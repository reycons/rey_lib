"""
Ollama provider implementation.

Ollama runs locally and exposes an OpenAI-compatible chat completions endpoint.
No API key is required.  The endpoint defaults to http://localhost:11434.

The ollama SDK is imported on demand.  Application code must never import
this module directly — use the provider registry instead.
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

__all__ = ["OllamaProvider"]

_DEFAULT_ENDPOINT = "http://localhost:11434"

_CAPABILITIES = ProviderCapabilities(
    supports_tools           = False,
    supports_images          = False,
    supports_json_mode       = True,
    supports_streaming       = True,
    supports_system_messages = True,
    max_context_tokens       = None,
)


class OllamaProvider(BaseProvider):
    """Ollama local LLM provider.

    Parameters
    ----------
    endpoint : str
        Base URL for the Ollama server.  Defaults to http://localhost:11434.
    """

    def __init__(self, endpoint: str = _DEFAULT_ENDPOINT) -> None:
        """Initialise the Ollama provider with a server endpoint."""
        self._endpoint = endpoint.rstrip("/")

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return Ollama capability declaration."""
        return _CAPABILITIES

    def run(
        self,
        messages:    list[Message],
        model:       str,
        max_tokens:  int   = 4000,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        """Call the Ollama chat API and return a normalised response.

        Uses the ollama Python package if installed; falls back to a direct
        HTTP request via urllib so the provider works without any extra deps.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array (system → user → assistant turns).
        model : str
            Ollama model name (e.g. 'llama3', 'mistral', 'phi3').
        max_tokens : int
            Maximum tokens to generate (passed as num_predict option).
        temperature : float
            Sampling temperature.

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
            return self._run_via_sdk(api_messages, model, max_tokens, temperature)
        except ImportError:
            return self._run_via_http(api_messages, model, max_tokens, temperature)

    def _run_via_sdk(
        self,
        messages:    list[dict[str, str]],
        model:       str,
        max_tokens:  int,
        temperature: float,
    ) -> ProviderResponse:
        """Call Ollama using the ollama Python SDK."""
        import ollama  # noqa: PLC0415

        try:
            response = ollama.chat(
                model    = model,
                messages = messages,
                options  = {
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            )
        except Exception as exc:
            raise ProviderFailure(f"Ollama SDK error: {exc}") from exc

        content    = response.message.content.strip()
        usage      = getattr(response, "usage", None)
        tokens_in  = getattr(usage, "prompt_tokens",     0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0

        raw: dict[str, Any] = {
            "model":   model,
            "message": {"role": "assistant", "content": content},
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
        messages:    list[dict[str, str]],
        model:       str,
        max_tokens:  int,
        temperature: float,
    ) -> ProviderResponse:
        """Call Ollama directly over HTTP using urllib (no SDK required)."""
        import json
        import urllib.error
        import urllib.request

        url     = f"{self._endpoint}/api/chat"
        payload = json.dumps({
            "model":    model,
            "messages": messages,
            "stream":   False,
            "options":  {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ProviderFailure(
                f"Ollama server unreachable at {self._endpoint}: {exc}"
            ) from exc
        except Exception as exc:
            raise ProviderFailure(f"Ollama HTTP error: {exc}") from exc

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
