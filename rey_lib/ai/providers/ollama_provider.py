"""The Ollama adapter.

Ported from ``rey_lib.llm.providers.ollama`` as a functional inventory. It is the
one adapter whose shape already matched ``ConfiguredProvider``: it took an
endpoint and a timeout rather than a credential, because a local server needs
none.

Two things do not carry across:

- **the capability override.** The old adapter let configuration set
  ``supports_tools`` / ``supports_images`` per option. Under this model the
  adapter is the sole authority on what it can do, so configuration cannot claim
  a capability the code does not implement -- which is exactly the defect the
  three-layer capability model exists to prevent. An installation may still
  *narrow* what it permits, through ``AIProfile.policy``.
- **the health check.** It reached the server at construction. Bootstrap must not
  depend on a local model server being up, so reachability is discovered when a
  call is made, as a provider failure like any other.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from rey_lib.ai.capabilities import AICapability, AICapabilitySet
from rey_lib.ai.content import AIContentKind, AIMessage
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import AICancelled, AIProviderError
from rey_lib.ai.policies import ReplayClassification, ReplayFacts
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.providers.configuration import ConfiguredProvider
from rey_lib.ai.results import AIUsage

__all__ = ["OllamaProvider"]

#: Where a local Ollama server is, when configuration names none.
DEFAULT_ENDPOINT = "http://localhost:11434"

#: What this adapter implements. Text and streaming only: the old adapter
#: declared tools and images false, and nothing here implements either.
_CAPABILITY = AICapabilitySet.of(AICapability.TEXT, AICapability.STREAMING)


class OllamaProvider(AIProvider):
    """A local Ollama server, behind the canonical provider boundary."""

    def __init__(
        self,
        configured: ConfiguredProvider,
        credentials: CredentialResolver | None = None,  # noqa: ARG002
    ) -> None:
        """Built like every other adapter, though it needs no credential.

        The resolver is accepted and ignored so one factory signature builds any
        provider. An adapter that needed a different construction shape would
        make the construction seam branch on which provider it was building.
        """
        self._configured = configured
        self._endpoint = (configured.endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self._timeout = configured.timeout_seconds or 120.0
        self._emitted = False

    @property
    def name(self) -> str:
        return self._configured.provider or "ollama"

    def capability_for(self, model: str) -> AICapabilitySet:  # noqa: ARG002
        return _CAPABILITY

    def replay_facts(self, failure: BaseException) -> ReplayFacts:  # noqa: ARG002
        """Safe only when nothing was emitted.

        A local server holds no conversation state, so a call that produced
        nothing is genuinely equivalent to repeat.
        """
        if self._emitted:
            return ReplayFacts(
                response_started=True,
                classification=ReplayClassification.UNSAFE,
            )
        return ReplayFacts(classification=ReplayClassification.SAFE)

    def invoke(
        self,
        call: ProviderCall,
        *,
        cancelled: Callable[[], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> ProviderReply:
        """Perform one call and answer in the subsystem's terms."""
        self._emitted = False

        options: dict[str, Any] = {}
        if call.temperature is not None:
            options["temperature"] = call.temperature
        if call.max_tokens is not None:
            options["num_predict"] = call.max_tokens

        payload: dict[str, Any] = {
            "model": call.model,
            "messages": _translated(call.messages),
            "stream": on_text is not None,
        }
        if options:
            payload["options"] = options
        if call.json_output:
            payload["format"] = "json"

        request = urllib.request.Request(
            f"{self._endpoint}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        if cancelled is not None and cancelled():
            raise AICancelled("The caller withdrew this AI execution.")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if on_text is not None:
                    return self._streamed(response, call.model, on_text, cancelled)
                body = json.loads(response.read().decode("utf-8"))
        except AICancelled:
            raise
        except urllib.error.URLError as exc:
            raise AIProviderError(
                f"Ollama is unreachable at {self._endpoint}: {exc}", cause=exc,
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- a provider's own error never leaves
            raise AIProviderError(f"Ollama failed: {exc}", cause=exc) from exc

        return _reply_from(body, call.model)

    def _streamed(
        self,
        response: Any,
        model: str,
        on_text: Callable[[str], None],
        cancelled: Callable[[], bool] | None,
    ) -> ProviderReply:
        """One streamed call. Ollama sends one JSON object per line."""
        parts: list[str] = []
        final: dict[str, Any] = {}

        for raw in response:
            if cancelled is not None and cancelled():
                raise AICancelled("The caller withdrew this AI execution.")
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            piece = str((chunk.get("message") or {}).get("content") or "")
            if piece:
                parts.append(piece)
                self._emitted = True
                on_text(piece)
            if chunk.get("done"):
                final = chunk

        return ProviderReply(
            text="".join(parts).strip(),
            usage=_usage(final),
            model=str(final.get("model") or model),
            raw={"model": final.get("model") or model},
        )


def _translated(messages: tuple[AIMessage, ...]) -> list[dict[str, str]]:
    """Canonical turns as the Ollama chat API takes them.

    Text only, because that is what this adapter declares. A non-text part
    cannot arrive: the capability check refuses the request before a provider is
    reached.
    """
    turns: list[dict[str, str]] = []
    for message in messages:
        rendered: list[str] = []
        for part in message.content:
            if part.kind is AIContentKind.TEXT:
                rendered.append(str(part.value))
            elif part.kind is AIContentKind.STRUCTURED:
                rendered.append(
                    json.dumps(part.value, ensure_ascii=False, sort_keys=True),
                )
        if rendered:
            turns.append({
                "role": message.role.value,
                "content": "\n\n".join(rendered),
            })
    return turns


def _usage(body: dict[str, Any]) -> AIUsage:
    """What the server reported, where it reported anything."""
    return AIUsage(
        input_tokens=int(body.get("prompt_eval_count") or 0),
        output_tokens=int(body.get("eval_count") or 0),
    )


def _reply_from(body: dict[str, Any], model: str) -> ProviderReply:
    """The server's answer, in the subsystem's terms."""
    return ProviderReply(
        text=str((body.get("message") or {}).get("content") or "").strip(),
        usage=_usage(body),
        model=str(body.get("model") or model),
        raw={"model": body.get("model") or model},
    )
