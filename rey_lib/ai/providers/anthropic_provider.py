"""The Anthropic adapter.

Ported from ``rey_lib.llm.providers.anthropic`` as a functional inventory, not
copied. Four things changed, each because the new boundary requires it:

- **no credential is held.** The old adapter kept ``self._api_key`` for its
  lifetime. This one holds a reference and a resolver and reads the environment
  as the client is constructed, which is the estate's stated point of use.
- **no SDK retry.** ``max_retries=0`` is passed explicitly. Retry policy is owned
  above this boundary, and an SDK retrying underneath multiplies it invisibly.
- **canonical messages in.** The adapter receives ``AIMessage`` values and
  translates each content part itself, so an image arrives as an image.
- **replay facts out.** It reports whether emission had begun, so the execution
  owner can decide whether repeating the call is legal.
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
from rey_lib.ai.tools import AIToolCall

__all__ = ["AnthropicProvider"]

#: What this adapter implements. ``STRUCTURED_OUTPUT`` is absent because the old
#: adapter declared ``supports_json_mode = False`` and nothing here implements a
#: JSON mode; advertising it would let a structured request through to a provider
#: that will not honour it.
_CAPABILITY = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STREAMING,
    AICapability.TOOLS,
    AICapability.VISION,
)


class AnthropicProvider(AIProvider):
    """Anthropic, behind the canonical provider boundary."""

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
        """The configured provider this adapter *is*, by its stable identity.

        Not the provider type. Several configured providers share a type -- this
        estate configures six against ``ollama``, each with its own model -- so
        keying an adapter by its type would let one configuration replace
        another. ``ConfiguredProvider.provider`` remains the type, and is
        descriptive.
        """
        return self._configured.id or "anthropic"

    def capability_for(self, model: str) -> AICapabilitySet:  # noqa: ARG002
        """What this adapter can do. The same for every Anthropic model here."""
        return _CAPABILITY

    def replay_facts(self, failure: BaseException) -> ReplayFacts:  # noqa: ARG002
        """Whether the failed call may be repeated.

        Safe only when nothing was emitted. A streamed call that had begun
        producing text has already been observed downstream, so repeating it
        would duplicate output that a caller has seen.
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
        try:
            import anthropic  # noqa: PLC0415 -- optional until this provider runs
        except ImportError as exc:
            raise AIProviderError(
                "The anthropic package is not installed. Run: pip install anthropic",
                cause=exc,
            ) from exc

        self._emitted = False
        system, turns = _translated(call.messages)

        kwargs: dict[str, Any] = {
            "model": call.model,
            "max_tokens": call.max_tokens or 4000,
            "messages": turns,
        }
        if call.temperature is not None:
            kwargs["temperature"] = call.temperature
        if system:
            kwargs["system"] = system
        if call.tools:
            kwargs["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {"type": "object"},
                }
                for tool in call.tools
            ]
        if self._configured.timeout_seconds is not None:
            kwargs["timeout"] = self._configured.timeout_seconds

        client_options: dict[str, Any] = {
            # Resolved here, used immediately, never stored on this object.
            "api_key": self._credentials(self._configured.credential_ref),
            # Retry is owned above this boundary. See the base class contract.
            "max_retries": 0,
        }
        if self._configured.endpoint:
            client_options["base_url"] = self._configured.endpoint

        try:
            client = anthropic.Anthropic(**client_options)
            if cancelled is not None and cancelled():
                raise AICancelled("The caller withdrew this AI execution.")

            if on_text is not None:
                with client.messages.stream(**kwargs) as stream:
                    for delta in stream.text_stream:
                        if cancelled is not None and cancelled():
                            raise AICancelled("The caller withdrew this AI execution.")
                        self._emitted = True
                        on_text(delta)
                    response = stream.get_final_message()
            else:
                response = client.messages.create(**kwargs)
        except AICancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- a provider's own error never leaves
            raise _translated_failure(anthropic, exc) from exc

        return _reply_from(response)


def _translated(messages: tuple[AIMessage, ...]) -> tuple[str, list[dict[str, Any]]]:
    """Canonical turns as Anthropic takes them, with system extracted.

    Each content part is translated on its own. An image part becomes an image
    block rather than a description of one -- the defect the canonical message
    boundary exists to prevent.
    """
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        if message.role is AIRole.SYSTEM:
            system_parts.extend(
                str(part.value) for part in message.content
                if part.kind is AIContentKind.TEXT
            )
            continue

        if message.role is AIRole.TOOL:
            turns.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": _as_text(message),
                }],
            })
            continue

        blocks: list[dict[str, Any]] = [
            block for part in message.content
            for block in (_block(part),) if block is not None
        ]
        for requested in message.tool_calls:
            blocks.append({
                "type": "tool_use",
                "id": requested.id,
                "name": requested.name,
                "input": dict(requested.arguments),
            })
        if blocks:
            turns.append({"role": message.role.value, "content": blocks})

    return "\n\n".join(system_parts), turns


def _block(part: Any) -> dict[str, Any] | None:
    """One canonical content part as an Anthropic content block."""
    if part.kind is AIContentKind.TEXT:
        return {"type": "text", "text": str(part.value)}
    if part.kind is AIContentKind.STRUCTURED:
        import json  # noqa: PLC0415 -- only where a structured part appears

        return {
            "type": "text",
            "text": json.dumps(part.value, ensure_ascii=False, sort_keys=True),
        }
    if part.kind is AIContentKind.IMAGE:
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": str(part.value),
            },
        }
    if part.kind is AIContentKind.DOCUMENT:
        return {"type": "document", "source": {"type": "url", "url": str(part.value)}}
    return None


def _as_text(message: AIMessage) -> str:
    """A tool answer rendered as the text a tool_result block carries."""
    import json  # noqa: PLC0415

    rendered: list[str] = []
    for part in message.content:
        if part.kind is AIContentKind.TEXT:
            rendered.append(str(part.value))
        else:
            rendered.append(json.dumps(part.value, ensure_ascii=False, sort_keys=True))
    return "\n\n".join(rendered)


def _reply_from(response: Any) -> ProviderReply:
    """The provider's answer, in the subsystem's terms."""
    text_parts: list[str] = []
    tool_calls: list[AIToolCall] = []
    for block in getattr(response, "content", ()) or ():
        kind = getattr(block, "type", "")
        if kind == "text":
            text_parts.append(getattr(block, "text", ""))
        elif kind == "tool_use":
            tool_calls.append(
                AIToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", {}) or {}),
                ),
            )

    usage = getattr(response, "usage", None)
    return ProviderReply(
        text="".join(text_parts).strip(),
        tool_calls=tuple(tool_calls),
        usage=AIUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        ),
        model=str(getattr(response, "model", "")),
        raw={
            "id": getattr(response, "id", ""),
            "model": getattr(response, "model", ""),
            "stop_reason": getattr(response, "stop_reason", ""),
        },
    )


def _translated_failure(anthropic: Any, exc: Exception) -> AIProviderError:
    """One provider exception, in the estate's vocabulary.

    Every native class is folded into ``AIProviderError`` with the original kept
    as the cause. The old adapter distinguished rate-limit and timeout with their
    own types; the canonical hierarchy does not, because nothing above the
    boundary branched on them -- and inventing distinctions no caller reads is
    how a sixteen-class hierarchy happened the first time.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(exc, getattr(anthropic, "APITimeoutError", ())):
        return AIProviderError(f"Anthropic timed out: {exc}", cause=exc)
    if status is not None:
        return AIProviderError(
            f"Anthropic API error {status}: {getattr(exc, 'message', exc)}", cause=exc,
        )
    return AIProviderError(f"Anthropic failed: {exc}", cause=exc)
