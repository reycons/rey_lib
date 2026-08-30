"""The Gemini adapter.

Ported from ``rey_lib.llm.providers.gemini`` as a functional inventory. Same four
changes as the other API adapters: no held credential, no SDK retry, canonical
messages in, replay facts out.

The old module's own note is kept because it is a real distinction:
``supports_json_mode`` described *that adapter*, not the vendor. Gemini supports
structured output; this adapter does not implement it, so it does not advertise
it. Capability is what the adapter can do, not what the vendor could.
"""

from __future__ import annotations

from typing import Any, Callable

from rey_lib.ai.capabilities import AICapability, AICapabilitySet
from rey_lib.ai.content import AIContentKind, AIMessage, AIRole
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import AICancelled, AIProviderError
from rey_lib.ai.policies import ReplayClassification, ReplayFacts
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.providers.configuration import ConfiguredProvider
from rey_lib.ai.results import AIUsage

__all__ = ["GeminiProvider"]

_CAPABILITY = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STREAMING,
    AICapability.VISION,
)

#: Canonical role to the two Gemini accepts.
_ROLE = {AIRole.USER: "user", AIRole.ASSISTANT: "model", AIRole.TOOL: "user"}


class GeminiProvider(AIProvider):
    """Gemini, behind the canonical provider boundary."""

    def __init__(
        self,
        configured: ConfiguredProvider,
        credentials: CredentialResolver,
    ) -> None:
        self._configured = configured
        self._credentials = credentials
        self._emitted = False

    @property
    def name(self) -> str:
        return self._configured.provider or "gemini"

    def capability_for(self, model: str) -> AICapabilitySet:  # noqa: ARG002
        return _CAPABILITY

    def replay_facts(self, failure: BaseException) -> ReplayFacts:  # noqa: ARG002
        """Safe only when nothing was emitted."""
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
        try:
            from google import genai  # noqa: PLC0415 -- optional until this runs
            from google.genai import errors  # noqa: PLC0415
        except ImportError as exc:
            raise AIProviderError(
                "The google-genai package is not installed. "
                "Run: pip install google-genai",
                cause=exc,
            ) from exc

        self._emitted = False
        system, contents = _translated(call.messages)

        config: dict[str, Any] = {}
        if call.max_tokens is not None:
            config["max_output_tokens"] = call.max_tokens
        if call.temperature is not None:
            config["temperature"] = call.temperature
        if system:
            config["system_instruction"] = system

        # Resolved here, used immediately, never stored on this object.
        api_key = self._credentials(self._configured.credential_ref)

        try:
            client = genai.Client(api_key=api_key)
            if cancelled is not None and cancelled():
                raise AICancelled("The caller withdrew this AI execution.")

            if on_text is not None:
                return self._streamed(
                    client, contents, config, call.model, on_text, cancelled,
                )
            response = client.models.generate_content(
                model=call.model, contents=contents, config=config,
            )
        except AICancelled:
            raise
        except errors.APIError as exc:
            raise AIProviderError(f"Gemini API error: {exc}", cause=exc) from exc
        except Exception as exc:  # noqa: BLE001 -- a provider's own error never leaves
            raise AIProviderError(f"Gemini failed: {exc}", cause=exc) from exc

        tokens_in, tokens_out = _usage(response)
        model = str(getattr(response, "model_version", "") or call.model)
        return ProviderReply(
            text=str(getattr(response, "text", "") or "").strip(),
            usage=AIUsage(input_tokens=tokens_in, output_tokens=tokens_out),
            model=model,
            raw={"model": model},
        )

    def _streamed(
        self,
        client: Any,
        contents: list[dict[str, Any]],
        config: dict[str, Any],
        model: str,
        on_text: Callable[[str], None],
        cancelled: Callable[[], bool] | None,
    ) -> ProviderReply:
        """One streamed call. Usage arrives on later chunks, so the last wins."""
        parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        answered = model

        stream = client.models.generate_content_stream(
            model=model, contents=contents, config=config,
        )
        try:
            for chunk in stream:
                if cancelled is not None and cancelled():
                    raise AICancelled("The caller withdrew this AI execution.")
                piece = str(getattr(chunk, "text", "") or "")
                if piece:
                    parts.append(piece)
                    self._emitted = True
                    on_text(piece)
                chunk_in, chunk_out = _usage(chunk)
                if chunk_in or chunk_out:
                    tokens_in, tokens_out = chunk_in, chunk_out
                if getattr(chunk, "model_version", ""):
                    answered = str(chunk.model_version)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        return ProviderReply(
            text="".join(parts).strip(),
            usage=AIUsage(input_tokens=tokens_in, output_tokens=tokens_out),
            model=answered,
            raw={"model": answered},
        )


def _translated(messages: tuple[AIMessage, ...]) -> tuple[str, list[dict[str, Any]]]:
    """Canonical turns as Gemini takes them, with system extracted."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        if message.role is AIRole.SYSTEM:
            system_parts.extend(
                str(part.value) for part in message.content
                if part.kind is AIContentKind.TEXT
            )
            continue
        parts = [block for part in message.content
                 for block in (_part(part),) if block is not None]
        if parts:
            contents.append({"role": _ROLE.get(message.role, "user"), "parts": parts})

    return "\n\n".join(system_parts), contents


def _part(part: Any) -> dict[str, Any] | None:
    """One canonical content part as a Gemini part."""
    if part.kind is AIContentKind.TEXT:
        return {"text": str(part.value)}
    if part.kind is AIContentKind.STRUCTURED:
        import json  # noqa: PLC0415

        return {"text": json.dumps(part.value, ensure_ascii=False, sort_keys=True)}
    if part.kind is AIContentKind.IMAGE:
        return {
            "file_data": {
                "file_uri": str(part.value),
                "mime_type": part.media_type or "image/png",
            },
        }
    return None


def _usage(response: Any) -> tuple[int, int]:
    """Prompt and candidate token counts, where the response reports them."""
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return 0, 0
    return (
        int(getattr(metadata, "prompt_token_count", 0) or 0),
        int(getattr(metadata, "candidates_token_count", 0) or 0),
    )
