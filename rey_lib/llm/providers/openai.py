"""
OpenAI (GPT) provider implementation.

The openai SDK is imported on demand so it is only required when this
provider is actually used.  Application code must never import this module
directly — use the provider registry instead.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from rey_lib.llm.exceptions import (
    CancellationFailure,
    ProviderFailure,
    RateLimitFailure,
    TimeoutFailure,
)
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
        on_chunk:    Optional[Callable[[str], None]] = None,
        cancelled:   Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Call the OpenAI Chat Completions API and return a normalised response.

        When ``on_chunk`` is supplied the Chat Completions streaming API is used
        (with usage included) and each content delta is passed to it as it
        arrives; the accumulated response is still returned. When ``on_chunk`` is
        None the call is a single blocking request (unchanged behaviour).

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
            client = openai.OpenAI(api_key=self._api_key)
            if cancelled is not None and cancelled():
                raise CancellationFailure("LLM execution cancelled.")
            if on_chunk is not None:
                # Streaming: emit each content delta as it arrives, accumulate the
                # full content, and read usage from the final (include_usage) chunk.
                content_parts: list[str] = []
                usage = None
                resp_id = ""
                resp_model = model
                stream = client.chat.completions.create(
                    model          = model,
                    max_tokens     = max_tokens,
                    temperature    = temperature,
                    messages       = api_messages,
                    stream         = True,
                    stream_options = {"include_usage": True},
                )
                try:
                    for chunk in stream:
                        if cancelled is not None and cancelled():
                            raise CancellationFailure("LLM execution cancelled.")
                        if chunk.choices and chunk.choices[0].delta.content:
                            piece = chunk.choices[0].delta.content
                            content_parts.append(piece)
                            on_chunk(piece)
                        if getattr(chunk, "usage", None) is not None:
                            usage = chunk.usage
                        if getattr(chunk, "id", ""):
                            resp_id = chunk.id
                        if getattr(chunk, "model", ""):
                            resp_model = chunk.model
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                stream_tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
                stream_tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
                return ProviderResponse(
                    content    = "".join(content_parts).strip(),
                    tokens_in  = stream_tokens_in,
                    tokens_out = stream_tokens_out,
                    model      = resp_model,
                    raw        = {
                        "id":    resp_id,
                        "model": resp_model,
                        "usage": {
                            "prompt_tokens":     stream_tokens_in,
                            "completion_tokens": stream_tokens_out,
                        },
                    },
                )
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
