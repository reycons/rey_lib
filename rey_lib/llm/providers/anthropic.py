"""
Anthropic (Claude) provider implementation.

The anthropic SDK is imported on demand so it is only required when this
provider is actually used.  Application code must never import this module
directly — use the provider registry instead.
"""

from __future__ import annotations

from typing import Any

from rey_lib.llm.exceptions import ProviderFailure
from rey_lib.llm.providers.base import (
    BaseProvider,
    Message,
    ProviderCapabilities,
    ProviderResponse,
)

__all__ = ["AnthropicProvider"]

_CAPABILITIES = ProviderCapabilities(
    supports_tools           = True,
    supports_images          = True,
    supports_json_mode       = False,
    supports_streaming       = True,
    supports_system_messages = True,
    max_context_tokens       = 200_000,
)


class AnthropicProvider(BaseProvider):
    """Anthropic Claude provider.

    Parameters
    ----------
    api_key : str
        Anthropic API key.
    """

    def __init__(self, api_key: str) -> None:
        """Initialise the Anthropic provider with an API key."""
        if not api_key:
            raise ProviderFailure(
                "AnthropicProvider: api_key is required. "
                "Set ANTHROPIC_API_KEY in your environment."
            )
        self._api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return Anthropic capability declaration."""
        return _CAPABILITIES

    def run(
        self,
        messages:    list[Message],
        model:       str,
        max_tokens:  int   = 4000,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        """Call the Anthropic Messages API and return a normalised response.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array.  System messages are extracted and passed
            via the Anthropic ``system`` parameter.
        model : str
            Anthropic model identifier (e.g. 'claude-sonnet-4-6').
        max_tokens : int
            Maximum tokens to generate.
        temperature : float
            Sampling temperature.

        Returns
        -------
        ProviderResponse

        Raises
        ------
        ProviderFailure
            If the Anthropic API returns an error.
        """
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise ProviderFailure(
                "anthropic package is not installed. Run: pip install anthropic"
            ) from exc

        system_parts = [m.content for m in messages if m.role == "system"]
        user_messages = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role != "system"
        ]

        kwargs: dict[str, Any] = dict(
            model       = model,
            max_tokens  = max_tokens,
            temperature = temperature,
            messages    = user_messages,
        )
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        try:
            client   = anthropic.Anthropic(api_key=self._api_key)
            response = client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise ProviderFailure(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderFailure(f"Anthropic connection error: {exc}") from exc
        except anthropic.APIError as exc:
            raise ProviderFailure(f"Anthropic API error: {exc}") from exc

        content    = response.content[0].text.strip()
        tokens_in  = getattr(response.usage, "input_tokens",  0)
        tokens_out = getattr(response.usage, "output_tokens", 0)

        raw: dict[str, Any] = {
            "id":         response.id,
            "model":      response.model,
            "stop_reason": response.stop_reason,
            "usage":      {"input_tokens": tokens_in, "output_tokens": tokens_out},
        }

        return ProviderResponse(
            content    = content,
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = response.model,
            raw        = raw,
        )
