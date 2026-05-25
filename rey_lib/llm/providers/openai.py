"""
OpenAI (GPT) provider implementation.

The openai SDK is imported on demand so it is only required when this
provider is actually used.  Application code must never import this module
directly — use the provider registry instead.
"""

from __future__ import annotations

from typing import Any

from rey_lib.llm.exceptions import ProviderFailure, RateLimitFailure, TimeoutFailure
from rey_lib.llm.providers.base import (
    BaseProvider,
    Message,
    ProviderCapabilities,
    ProviderResponse,
)

__all__ = ["OpenAIProvider"]

_CAPABILITIES = ProviderCapabilities(
    supports_tools           = True,
    supports_images          = True,
    supports_json_mode       = True,
    supports_streaming       = True,
    supports_system_messages = True,
    max_context_tokens       = 128_000,
)


class OpenAIProvider(BaseProvider):
    """OpenAI GPT provider.

    Parameters
    ----------
    api_key : str
        OpenAI API key.
    """

    def __init__(self, api_key: str) -> None:
        """Initialise the OpenAI provider with an API key."""
        if not api_key:
            raise ProviderFailure(
                "OpenAIProvider: api_key is required. "
                "Set OPENAI_API_KEY in your environment."
            )
        self._api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return OpenAI capability declaration."""
        return _CAPABILITIES

    def run(
        self,
        messages:    list[Message],
        model:       str,
        max_tokens:  int   = 4000,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        """Call the OpenAI Chat Completions API and return a normalised response.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array including system, user, and assistant turns.
        model : str
            OpenAI model identifier (e.g. 'gpt-4o').
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
            If the OpenAI API returns an error.
        """
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:
            raise ProviderFailure(
                "openai package is not installed. Run: pip install openai"
            ) from exc

        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        try:
            client   = openai.OpenAI(api_key=self._api_key)
            response = client.chat.completions.create(
                model       = model,
                max_tokens  = max_tokens,
                temperature = temperature,
                messages    = api_messages,
            )
        except openai.APITimeoutError as exc:
            raise TimeoutFailure(f"OpenAI timeout: {exc}") from exc
        except openai.RateLimitError as exc:
            raise RateLimitFailure(
                f"OpenAI rate-limit {exc.status_code}: {exc.message}"
            ) from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 408:
                raise TimeoutFailure(
                    f"OpenAI timeout {exc.status_code}: {exc.message}"
                ) from exc
            if exc.status_code == 429:
                raise RateLimitFailure(
                    f"OpenAI rate-limit {exc.status_code}: {exc.message}"
                ) from exc
            raise ProviderFailure(
                f"OpenAI API error {exc.status_code}: {exc.message}"
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderFailure(f"OpenAI connection error: {exc}") from exc
        except openai.OpenAIError as exc:
            raise ProviderFailure(f"OpenAI API error: {exc}") from exc

        content    = response.choices[0].message.content.strip()
        usage      = response.usage
        tokens_in  = getattr(usage, "prompt_tokens",     0)
        tokens_out = getattr(usage, "completion_tokens", 0)

        raw: dict[str, Any] = {
            "id":    response.id,
            "model": response.model,
            "usage": {
                "prompt_tokens":     tokens_in,
                "completion_tokens": tokens_out,
            },
        }

        return ProviderResponse(
            content    = content,
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = response.model,
            raw        = raw,
        )
