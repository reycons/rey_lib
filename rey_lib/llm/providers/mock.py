"""
Deterministic mock provider for testing.

The mock provider returns pre-configured responses without making any network
calls.  Tests must not require paid provider API calls.

Usage
-----
Single fixed response::

    provider = MockProvider(response='{"status": "ok"}')
    result = provider.run(messages, model="mock")

Rotating responses (cycles through the list)::

    provider = MockProvider(responses=['{"a": 1}', '{"b": 2}'])

Simulating failure::

    provider = MockProvider(raise_on_call=ProviderFailure("timeout"))
"""

from __future__ import annotations

from itertools import cycle
from typing import Any, Callable, Optional

from rey_lib.llm.exceptions import ProviderFailure
from rey_lib.llm.providers.base import (
    BaseProvider,
    Message,
    ProviderCapabilities,
    ProviderResponse,
)

__all__ = ["MockProvider"]

_CAPABILITIES = ProviderCapabilities(
    supports_tools           = True,
    supports_images          = True,
    supports_json_mode       = True,
    supports_streaming       = False,
    supports_system_messages = True,
    max_context_tokens       = 1_000_000,
)

_DEFAULT_RESPONSE = '{"mock": true}'


class MockProvider(BaseProvider):
    """Deterministic mock provider for unit and contract testing.

    Parameters
    ----------
    response : Optional[str]
        Fixed response string returned for every call.  Overrides
        ``responses`` when provided.
    responses : Optional[list[str]]
        Rotating list of responses.  Cycles from the beginning when
        exhausted.  Defaults to ['{"mock": true}'] when neither
        ``response`` nor ``responses`` is supplied.
    raise_on_call : Optional[Exception]
        If set, run() raises this exception instead of returning a response.
        Useful for simulating provider failures.
    tokens_in : int
        Reported input token count (default 10).
    tokens_out : int
        Reported output token count (default 10).
    """

    def __init__(
        self,
        response:      Optional[str]            = None,
        responses:     Optional[list[str]]      = None,
        raise_on_call: Optional[Exception]      = None,
        tokens_in:     int                      = 10,
        tokens_out:    int                      = 10,
    ) -> None:
        """Initialise the mock provider."""
        self._raise_on_call = raise_on_call
        self._tokens_in     = tokens_in
        self._tokens_out    = tokens_out

        if response is not None:
            self._responses: Any = cycle([response])
        elif responses is not None:
            if not responses:
                raise ValueError("MockProvider: responses list must not be empty.")
            self._responses = cycle(responses)
        else:
            self._responses = cycle([_DEFAULT_RESPONSE])

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return mock provider capability declaration."""
        return _CAPABILITIES

    def run(
        self,
        messages:    list[Message],
        model:       str,
        max_tokens:  int   = 4000,
        temperature: float = 0.0,
        on_chunk:    Optional[Callable[[str], None]] = None,
        cancelled:   Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Return the next pre-configured response without any network call.

        ``on_chunk`` is accepted for interface parity but ignored (the mock does
        not stream).

        Parameters
        ----------
        messages : list[Message]
            Ignored — mock does not process messages.
        model : str
            Stored in the response for audit consistency.
        max_tokens : int
            Ignored.
        temperature : float
            Ignored.

        Returns
        -------
        ProviderResponse

        Raises
        ------
        Exception
            Whatever was passed as raise_on_call, if set.
        """
        if self._raise_on_call is not None:
            raise self._raise_on_call

        content = next(self._responses)

        raw: dict[str, Any] = {
            "mock":       True,
            "model":      model,
            "usage":      {"input_tokens": self._tokens_in, "output_tokens": self._tokens_out},
        }

        return ProviderResponse(
            content    = content,
            tokens_in  = self._tokens_in,
            tokens_out = self._tokens_out,
            model      = model,
            raw        = raw,
        )
