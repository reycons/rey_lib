"""
Google Gemini provider implementation.

The google-genai SDK is imported on demand so it is only required when this
provider is actually used.  Application code must never import this module
directly — use the provider registry instead.

Gemini names its conversation roles 'user' and 'model', and carries the system
prompt outside the message array as a system instruction. Both are translated
here so callers keep using Rey's system/user/assistant messages unchanged.
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

__all__ = ["GeminiProvider"]

# The context window is the current 2.5-series figure. Gemini supports
# structured output through response_mime_type/response_schema, so JSON mode is
# declared true because the declaration describes the provider, not what Rey
# currently asks of it.
#
# Gap — reachability, not accuracy: BaseProvider.run() carries no parameter for
# a response format or schema, and runner._single_provider_call passes none, so
# nothing can request JSON mode through the contract today. Ollama's run() has
# an extra response_format parameter the contract does not declare, which is the
# same gap answered locally. Closing it properly is a provider-contract change
# for its own increment, deliberately not forced into this one.
_CAPABILITIES = ProviderCapabilities(
    supports_tools           = True,
    supports_images          = True,
    supports_json_mode       = True,
    supports_streaming       = True,
    supports_system_messages = True,
    max_context_tokens       = 1_048_576,
)

# Rey role → Gemini role. Gemini has no 'assistant'; a prior model turn is
# 'model'. System messages never appear here — they become a system
# instruction instead.
_ROLE_MAP = {"user": "user", "assistant": "model", "model": "model"}

# HTTP status codes that mean the same thing to Rey regardless of provider.
_TIMEOUT_STATUS = (408, 504)
_RATE_LIMIT_STATUS = 429


def _status_code(exc: Any) -> int:
    """Return an API error's HTTP status code, or 0 when it carries none."""
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return 0


class GeminiProvider(BaseProvider):
    """Google Gemini provider.

    Parameters
    ----------
    api_key : str
        Gemini API key.
    """

    def __init__(self, api_key: str) -> None:
        """Initialise the Gemini provider with an API key."""
        if not api_key:
            raise ProviderFailure(
                "GeminiProvider: api_key is required. "
                "Set GEMINI_API_KEY in your environment."
            )
        self._api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the Gemini capability declaration."""
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
        """Call the Gemini generate-content API and return a normalised response.

        When ``on_chunk`` is supplied the streaming API is used and each text
        delta is passed to it as it arrives; the accumulated text is still
        returned. When ``on_chunk`` is None the call is a single blocking
        request.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array. System messages are extracted and passed as
            the Gemini ``system_instruction``.
        model : str
            Gemini model identifier (e.g. 'gemini-2.5-flash').
        max_tokens : int
            Maximum tokens to generate.
        temperature : float
            Sampling temperature.
        on_chunk : Optional[Callable[[str], None]]
            Incremental-output callback, invoked with text deltas only.
        cancelled : Optional[Callable[[], bool]]
            Cooperative cancellation check.

        Returns
        -------
        ProviderResponse

        Raises
        ------
        ProviderFailure
            If the Gemini API returns an error.
        TimeoutFailure
            If the Gemini call times out.
        RateLimitFailure
            If the Gemini API reports a rate limit.
        CancellationFailure
            If the owning run is cancelled.
        """
        try:
            from google import genai  # noqa: PLC0415
            from google.genai import errors  # noqa: PLC0415
        except ImportError as exc:
            raise ProviderFailure(
                "google-genai package is not installed. Run: pip install google-genai"
            ) from exc

        system_parts = [m.content for m in messages if m.role == "system"]
        contents = [
            {
                "role": _ROLE_MAP.get(m.role, "user"),
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]

        # Plain dicts rather than SDK model objects: the SDK accepts both, and
        # this keeps the request shape readable and the module's SDK surface to
        # the client and its error types.
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature":       temperature,
        }
        if system_parts:
            config["system_instruction"] = "\n\n".join(system_parts)

        try:
            client = genai.Client(api_key=self._api_key)
            if cancelled is not None and cancelled():
                raise CancellationFailure("LLM execution cancelled.")
            if on_chunk is not None:
                return self._run_streaming(
                    client, contents, config, model, on_chunk, cancelled,
                )
            response = client.models.generate_content(
                model    = model,
                contents = contents,
                config   = config,
            )
        except errors.ClientError as exc:
            status = _status_code(exc)
            if status == _RATE_LIMIT_STATUS:
                raise RateLimitFailure(f"Gemini rate-limit {status}: {exc}") from exc
            if status in _TIMEOUT_STATUS:
                raise TimeoutFailure(f"Gemini timeout {status}: {exc}") from exc
            raise ProviderFailure(f"Gemini API error {status}: {exc}") from exc
        except errors.ServerError as exc:
            status = _status_code(exc)
            if status in _TIMEOUT_STATUS:
                raise TimeoutFailure(f"Gemini timeout {status}: {exc}") from exc
            raise ProviderFailure(f"Gemini API error {status}: {exc}") from exc
        except errors.APIError as exc:
            raise ProviderFailure(f"Gemini API error: {exc}") from exc

        tokens_in, tokens_out = _usage(response)
        resp_model = str(getattr(response, "model_version", "") or model)
        return ProviderResponse(
            content    = str(getattr(response, "text", "") or "").strip(),
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = resp_model,
            raw        = _raw(resp_model, tokens_in, tokens_out),
        )

    def _run_streaming(
        self,
        client:    Any,
        contents:  list[dict[str, Any]],
        config:    dict[str, Any],
        model:     str,
        on_chunk:  Callable[[str], None],
        cancelled: Optional[Callable[[], bool]],
    ) -> ProviderResponse:
        """Stream one Gemini response, forwarding text deltas as they arrive.

        Usage metadata arrives on the later chunks rather than the first, so the
        last reported counts win. The stream is closed on every exit path,
        including cancellation, so a cancelled run does not leave the response
        open.
        """
        content_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        resp_model = model

        stream = client.models.generate_content_stream(
            model    = model,
            contents = contents,
            config   = config,
        )
        try:
            for chunk in stream:
                if cancelled is not None and cancelled():
                    raise CancellationFailure("LLM execution cancelled.")
                text = str(getattr(chunk, "text", "") or "")
                if text:
                    content_parts.append(text)
                    on_chunk(text)
                chunk_in, chunk_out = _usage(chunk)
                if chunk_in or chunk_out:
                    tokens_in, tokens_out = chunk_in, chunk_out
                version = str(getattr(chunk, "model_version", "") or "")
                if version:
                    resp_model = version
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        return ProviderResponse(
            content    = "".join(content_parts).strip(),
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = resp_model,
            raw        = _raw(resp_model, tokens_in, tokens_out),
        )


def _usage(response: Any) -> tuple[int, int]:
    """Return (tokens_in, tokens_out) from a Gemini response or chunk.

    A response that reports no usage yields zeros, which is the same answer
    every other Rey provider gives when a provider omits usage.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    return int(tokens_in), int(tokens_out)


def _raw(model: str, tokens_in: int, tokens_out: int) -> dict[str, Any]:
    """Return the audit payload in the shape the other providers record."""
    return {
        "model": model,
        "usage": {
            "prompt_tokens":     tokens_in,
            "completion_tokens": tokens_out,
        },
    }
