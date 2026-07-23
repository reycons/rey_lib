"""
Ollama provider implementation.

Ollama runs locally and exposes an OpenAI-compatible chat completions endpoint.
No API key is required.  The endpoint defaults to http://localhost:11434.

The ollama SDK is imported on demand.  Application code must never import
this module directly — use the provider registry instead.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from rey_lib.llm.exceptions import CancellationFailure, ProviderFailure
from rey_lib.llm.providers.base import (
    BaseProvider,
    Message,
    ProviderCapabilities,
    ProviderResponse,
)

__all__ = ["OllamaProvider"]

# Exposed as constants so callers can reference them without magic strings.
DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_TIMEOUT  = 300

# Code-level capability defaults. Profiles may override any of these through
# provider options (see OllamaProvider.from_options) so capabilities are a
# configuration concern, not a value buried in code.
_DEFAULT_CAPABILITIES = ProviderCapabilities(
    supports_tools           = False,
    supports_images          = False,
    supports_json_mode       = True,
    supports_streaming       = True,
    supports_system_messages = True,
    max_context_tokens       = None,
)


def _close_response(response: Any) -> None:
    """Close one provider response when its transport exposes a close operation."""
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — cleanup must preserve the terminal result
            pass


def _close_response_on_cancel(
    response: Any,
    cancelled: Optional[Callable[[], bool]],
) -> threading.Event:
    """Close a blocking response promptly when its owning run is cancelled."""
    stop = threading.Event()
    if cancelled is None:
        return stop

    def _watch() -> None:
        while not stop.wait(0.05):
            if cancelled():
                _close_response(response)
                return

    threading.Thread(target=_watch, daemon=True).start()
    return stop


class OllamaProvider(BaseProvider):
    """Ollama local LLM provider.

    Parameters
    ----------
    endpoint : str
        Base URL for the Ollama server.  Defaults to http://localhost:11434.
    """

    def __init__(
        self,
        endpoint:     str                          = DEFAULT_ENDPOINT,
        timeout:      int                          = DEFAULT_TIMEOUT,
        capabilities: ProviderCapabilities | None  = None,
    ) -> None:
        """Initialise the Ollama provider with endpoint, timeout, and capabilities.

        Parameters
        ----------
        endpoint : str
            Base URL for the Ollama server.
        timeout : int
            HTTP timeout in seconds for generation requests.
        capabilities : ProviderCapabilities, optional
            Capability declaration. Defaults to the provider's code-level
            defaults when configuration does not supply one.
        """
        self._endpoint     = endpoint.rstrip("/")
        self._timeout      = timeout
        self._capabilities = capabilities or _DEFAULT_CAPABILITIES

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "OllamaProvider":
        """Build an OllamaProvider from a provider-options mapping.

        Recognised keys: ``endpoint``, ``timeout_seconds`` (or ``timeout``),
        and the ``supports_*`` / ``max_context_tokens`` capability flags. Any
        missing key falls back to the provider default. Options are typically
        sourced from the selected LLM profile.

        Parameters
        ----------
        options : dict[str, Any]
            Provider options from configuration.

        Returns
        -------
        OllamaProvider
        """
        endpoint = str(options.get("endpoint") or DEFAULT_ENDPOINT)
        timeout  = int(
            options.get("timeout_seconds") or options.get("timeout") or DEFAULT_TIMEOUT
        )

        defaults     = _DEFAULT_CAPABILITIES
        capabilities = ProviderCapabilities(
            supports_tools           = bool(options.get("supports_tools",           defaults.supports_tools)),
            supports_images          = bool(options.get("supports_images",          defaults.supports_images)),
            supports_json_mode       = bool(options.get("supports_json_mode",       defaults.supports_json_mode)),
            supports_streaming       = bool(options.get("supports_streaming",       defaults.supports_streaming)),
            supports_system_messages = bool(options.get("supports_system_messages", defaults.supports_system_messages)),
            max_context_tokens       = options.get("max_context_tokens",            defaults.max_context_tokens),
        )
        return cls(endpoint=endpoint, timeout=timeout, capabilities=capabilities)

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the configured Ollama capability declaration."""
        return self._capabilities

    def health_check(self) -> None:
        """Verify that the configured Ollama server is reachable.

        The provider never starts Ollama itself — lifecycle is a local/service
        concern. Callers should run this before analysis to fail fast with an
        actionable message when the endpoint is down.

        Raises
        ------
        ProviderFailure
            If the Ollama endpoint cannot be reached or returns an error status.
        """
        import urllib.error    # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        url = f"{self._endpoint}/api/tags"

        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status >= 400:
                    raise ProviderFailure(
                        f"Ollama health check failed at {self._endpoint}: "
                        f"HTTP {resp.status}"
                    )
        except urllib.error.URLError as exc:
            raise ProviderFailure(
                f"Ollama is configured but unreachable at {self._endpoint}. "
                "Start Ollama before running this Rey Apps pipeline."
            ) from exc
        except OSError as exc:
            raise ProviderFailure(
                f"Ollama health check failed at {self._endpoint}: {exc}"
            ) from exc

    def run(
        self,
        messages:        list[Message],
        model:           str,
        max_tokens:      int                          = 4000,
        temperature:     float                        = 0.0,
        response_format: str | dict[str, Any] | None  = None,
        on_chunk:        Optional[Callable[[str], None]] = None,
        cancelled:       Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Call the Ollama chat API and return a normalised response.

        Uses the ollama Python package if installed; falls back to a direct
        HTTP request via urllib so the provider works without any extra deps.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array (system → user → assistant turns).
        model : str
            Ollama model name (e.g. 'qwen2.5-coder:32b', 'llama3').
        max_tokens : int
            Maximum tokens to generate (passed as num_predict option).
        temperature : float
            Sampling temperature.
        response_format : str | dict | None
            Structured-output request, passed through as Ollama's ``format``
            field. Use 'json' for JSON mode or a JSON Schema dict where the
            model supports it. None disables structured output.

        Returns
        -------
        ProviderResponse

        Raises
        ------
        ProviderFailure
            If the Ollama server is unreachable or returns an error.
        """
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        try:
            return self._run_via_sdk(
                api_messages, model, max_tokens, temperature, response_format,
                on_chunk, cancelled,
            )
        except ImportError:
            return self._run_via_http(
                api_messages, model, max_tokens, temperature, response_format,
                on_chunk, cancelled,
            )

    def _run_via_sdk(
        self,
        messages:        list[dict[str, str]],
        model:           str,
        max_tokens:      int,
        temperature:     float,
        response_format: str | dict[str, Any] | None = None,
        on_chunk:        Optional[Callable[[str], None]] = None,
        cancelled:       Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Call Ollama using the ollama Python SDK against the configured endpoint."""
        import ollama  # noqa: PLC0415

        client = ollama.Client(host=self._endpoint)

        payload: dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   on_chunk is not None,
            "options":  {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        if response_format is not None:
            payload["format"] = response_format

        if cancelled is not None and cancelled():
            raise CancellationFailure("LLM execution cancelled.")

        try:
            response = client.chat(**payload)
        except ollama.ResponseError as exc:
            raise ProviderFailure(f"Ollama response error: {exc}") from exc
        except OSError as exc:
            raise ProviderFailure(f"Ollama connection error: {exc}") from exc

        if on_chunk is not None:
            content_parts: list[str] = []
            final_response: Any = None
            watcher_stop = _close_response_on_cancel(response, cancelled)
            try:
                try:
                    for chunk in response:
                        if cancelled is not None and cancelled():
                            raise CancellationFailure("LLM execution cancelled.")
                        final_response = chunk
                        message = getattr(chunk, "message", None)
                        piece = str(getattr(message, "content", "") or "")
                        thinking = str(getattr(message, "thinking", "") or "")
                        if thinking:
                            on_chunk(thinking)
                        if piece:
                            content_parts.append(piece)
                            on_chunk(piece)
                    if cancelled is not None and cancelled():
                        raise CancellationFailure("LLM execution cancelled.")
                except CancellationFailure:
                    raise
                except Exception as exc:
                    if cancelled is not None and cancelled():
                        raise CancellationFailure("LLM execution cancelled.") from exc
                    raise
            finally:
                watcher_stop.set()
                _close_response(response)
            content = "".join(content_parts).strip()
            tokens_in = int(getattr(final_response, "prompt_eval_count", 0) or 0)
            tokens_out = int(getattr(final_response, "eval_count", 0) or 0)
        else:
            try:
                content = response.message.content.strip()
                tokens_in = int(getattr(response, "prompt_eval_count", 0) or 0)
                tokens_out = int(getattr(response, "eval_count", 0) or 0)
            finally:
                _close_response(response)

        raw: dict[str, Any] = {
            "model":             model,
            "message":           {"role": "assistant", "content": content},
            "prompt_eval_count": tokens_in,
            "eval_count":        tokens_out,
        }

        return ProviderResponse(
            content    = content,
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = model,
            raw        = raw,
        )

    def _run_via_http(
        self,
        messages:        list[dict[str, str]],
        model:           str,
        max_tokens:      int,
        temperature:     float,
        response_format: str | dict[str, Any] | None = None,
        on_chunk:        Optional[Callable[[str], None]] = None,
        cancelled:       Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Call Ollama directly over HTTP using urllib (no SDK required)."""
        import json
        import urllib.error
        import urllib.request

        url = f"{self._endpoint}/api/chat"
        payload_dict: dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   on_chunk is not None,
            "options":  {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        if response_format is not None:
            payload_dict["format"] = response_format
        payload = json.dumps(payload_dict).encode("utf-8")

        req = urllib.request.Request(
            url,
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )

        if cancelled is not None and cancelled():
            raise CancellationFailure("LLM execution cancelled.")

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                watcher_stop = _close_response_on_cancel(resp, cancelled)
                try:
                    if on_chunk is None:
                        data = json.loads(resp.read().decode("utf-8"))
                    else:
                        content_parts: list[str] = []
                        data = {}
                        for raw_line in resp:
                            if cancelled is not None and cancelled():
                                raise CancellationFailure("LLM execution cancelled.")
                            line = raw_line.decode("utf-8").strip()
                            if not line:
                                continue
                            data = json.loads(line)
                            message = data.get("message") or {}
                            thinking = str(message.get("thinking") or "")
                            piece = str(message.get("content") or "")
                            if thinking:
                                on_chunk(thinking)
                            if piece:
                                content_parts.append(piece)
                                on_chunk(piece)
                        if cancelled is not None and cancelled():
                            raise CancellationFailure("LLM execution cancelled.")
                        data = dict(data)
                        data["message"] = {
                            "role": "assistant",
                            "content": "".join(content_parts),
                        }
                finally:
                    watcher_stop.set()
                    _close_response(resp)
        except urllib.error.URLError as exc:
            raise ProviderFailure(
                f"Ollama server unreachable at {self._endpoint}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderFailure(f"Ollama returned invalid JSON: {exc}") from exc
        except OSError as exc:
            if cancelled is not None and cancelled():
                raise CancellationFailure("LLM execution cancelled.") from exc
            raise ProviderFailure(f"Ollama HTTP error: {exc}") from exc
        except Exception as exc:
            if cancelled is not None and cancelled():
                raise CancellationFailure("LLM execution cancelled.") from exc
            raise

        message    = data.get("message", {})
        content    = (message.get("content") or "").strip()
        tokens_in  = data.get("prompt_eval_count",  0)
        tokens_out = data.get("eval_count",          0)

        raw: dict[str, Any] = {
            "model":              model,
            "message":            message,
            "prompt_eval_count":  tokens_in,
            "eval_count":         tokens_out,
        }

        return ProviderResponse(
            content    = content,
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            model      = model,
            raw        = raw,
        )
