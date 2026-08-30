"""The OpenAI adapter.

Ported from ``rey_lib.llm.providers.openai`` as a functional inventory. Same four
changes as the Anthropic adapter: no held credential, no SDK retry, canonical
messages in, replay facts out.

The nested-retry point is sharpest here. The OpenAI SDK retries by default, and
the old adapter configured nothing -- so a three-attempt Rey policy over that
default was already more network calls than an operator was told about.
``max_retries=0`` makes the Rey policy the only one.
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

__all__ = ["OpenAIProvider"]

#: ``STRUCTURED_OUTPUT`` is absent for the same reason as Anthropic: the old
#: adapter declared ``supports_json_mode = False`` and nothing here implements
#: one.
_CAPABILITY = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STREAMING,
    AICapability.TOOLS,
    AICapability.VISION,
)


class OpenAIProvider(AIProvider):
    """OpenAI, behind the canonical provider boundary."""

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
        return self._configured.provider or "openai"

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
            import openai  # noqa: PLC0415 -- optional until this provider runs
        except ImportError as exc:
            raise AIProviderError(
                "The openai package is not installed. Run: pip install openai",
                cause=exc,
            ) from exc

        self._emitted = False

        kwargs: dict[str, Any] = {
            "model": call.model,
            "messages": _translated(call.messages),
        }
        if call.max_tokens is not None:
            kwargs["max_tokens"] = call.max_tokens
        if call.temperature is not None:
            kwargs["temperature"] = call.temperature
        if call.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or {"type": "object"},
                    },
                }
                for tool in call.tools
            ]

        client_options: dict[str, Any] = {
            # Resolved here, used immediately, never stored on this object.
            "api_key": self._credentials(self._configured.credential_ref),
            # The SDK retries by default. Retry is owned above this boundary.
            "max_retries": 0,
        }
        if self._configured.endpoint:
            client_options["base_url"] = self._configured.endpoint
        if self._configured.timeout_seconds is not None:
            client_options["timeout"] = self._configured.timeout_seconds

        try:
            client = openai.OpenAI(**client_options)
            if cancelled is not None and cancelled():
                raise AICancelled("The caller withdrew this AI execution.")

            if on_text is not None:
                return self._streamed(client, kwargs, cancelled, on_text)
            response = client.chat.completions.create(**kwargs)
        except AICancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- a provider's own error never leaves
            raise _translated_failure(exc) from exc

        return _reply_from(response)

    def _streamed(
        self,
        client: Any,
        kwargs: dict[str, Any],
        cancelled: Callable[[], bool] | None,
        on_text: Callable[[str], None],
    ) -> ProviderReply:
        """One streamed call, reporting deltas as they arrive."""
        parts: list[str] = []
        usage: Any = None
        model = str(kwargs.get("model", ""))
        identity = ""

        stream = client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True},
        )
        try:
            for chunk in stream:
                if cancelled is not None and cancelled():
                    raise AICancelled("The caller withdrew this AI execution.")
                choices = getattr(chunk, "choices", None) or ()
                if choices and getattr(choices[0].delta, "content", None):
                    piece = choices[0].delta.content
                    parts.append(piece)
                    self._emitted = True
                    on_text(piece)
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if getattr(chunk, "id", ""):
                    identity = chunk.id
                if getattr(chunk, "model", ""):
                    model = chunk.model
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        return ProviderReply(
            text="".join(parts).strip(),
            usage=AIUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            model=model,
            raw={"id": identity, "model": model},
        )


def _translated(messages: tuple[AIMessage, ...]) -> list[dict[str, Any]]:
    """Canonical turns as the Chat Completions API takes them."""
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.role is AIRole.TOOL:
            turns.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": _as_text(message),
            })
            continue

        if message.tool_calls:
            import json  # noqa: PLC0415 -- only where a tool call appears

            turns.append({
                "role": "assistant",
                "content": _as_text(message) or None,
                "tool_calls": [
                    {
                        "id": requested.id,
                        "type": "function",
                        "function": {
                            "name": requested.name,
                            "arguments": json.dumps(requested.arguments),
                        },
                    }
                    for requested in message.tool_calls
                ],
            })
            continue

        parts = [block for part in message.content
                 for block in (_block(part),) if block is not None]
        if not parts:
            continue
        # A turn of plain text is sent as a string, which every model accepts;
        # the multipart form is used only where a non-text part is present.
        if all(block["type"] == "text" for block in parts):
            turns.append({
                "role": message.role.value,
                "content": "\n\n".join(block["text"] for block in parts),
            })
        else:
            turns.append({"role": message.role.value, "content": parts})
    return turns


def _block(part: Any) -> dict[str, Any] | None:
    """One canonical content part as a Chat Completions content block."""
    if part.kind is AIContentKind.TEXT:
        return {"type": "text", "text": str(part.value)}
    if part.kind is AIContentKind.STRUCTURED:
        import json  # noqa: PLC0415

        return {
            "type": "text",
            "text": json.dumps(part.value, ensure_ascii=False, sort_keys=True),
        }
    if part.kind is AIContentKind.IMAGE:
        return {"type": "image_url", "image_url": {"url": str(part.value)}}
    return None


def _as_text(message: AIMessage) -> str:
    """The text a turn carries, for the shapes that take a plain string."""
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
    import json  # noqa: PLC0415

    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    tool_calls: list[AIToolCall] = []
    for requested in getattr(message, "tool_calls", None) or ():
        function = getattr(requested, "function", None)
        raw_arguments = getattr(function, "arguments", "") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = {"_unparsed": raw_arguments}
        tool_calls.append(
            AIToolCall(
                id=str(getattr(requested, "id", "")),
                name=str(getattr(function, "name", "")),
                arguments=arguments if isinstance(arguments, dict) else {},
            ),
        )

    usage = getattr(response, "usage", None)
    return ProviderReply(
        text=(getattr(message, "content", "") or "").strip(),
        tool_calls=tuple(tool_calls),
        usage=AIUsage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        ),
        model=str(getattr(response, "model", "")),
        raw={"id": getattr(response, "id", ""), "model": getattr(response, "model", "")},
    )


def _translated_failure(exc: Exception) -> AIProviderError:
    """One provider exception, in the estate's vocabulary."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        return AIProviderError(
            f"OpenAI API error {status}: {getattr(exc, 'message', exc)}", cause=exc,
        )
    return AIProviderError(f"OpenAI failed: {exc}", cause=exc)
