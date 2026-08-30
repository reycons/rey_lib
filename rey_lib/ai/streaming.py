"""Canonical execution events.

A provider's stream stops at the provider adapter. What travels upward is this
vocabulary, so Console live output, a CLI and the Workbench observe one thing
and none of them learns a provider's event shape.

The old subsystem streamed through an ``on_chunk(str)`` callback, which can
express a text delta and nothing else -- no tool call, no structured delta, no
usage update, no artifact. That is why this is an event model rather than a
callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rey_lib.ai.results import AIArtifact, AIResult, AIUsage
from rey_lib.ai.tools import AIToolCall, AIToolResult

__all__ = ["AIEvent", "AIEventKind"]


class AIEventKind(str, Enum):
    """What kind of thing happened."""

    EXECUTION_STARTED = "execution_started"
    CONTENT_DELTA = "content_delta"
    STRUCTURED_DELTA = "structured_delta"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_RESULT_ACCEPTED = "tool_result_accepted"
    PROVIDER_CHANGED = "provider_changed"
    ARTIFACT_PRODUCED = "artifact_produced"
    USAGE_UPDATED = "usage_updated"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class AIEvent:
    """One canonical event.

    One type with a kind rather than nine classes: a consumer switches on the
    kind and reads the field that kind carries. Nine near-identical dataclasses
    would be the same information with more ceremony.
    """

    kind: AIEventKind
    execution_id: str = ""
    text: str = ""
    value: Any = None
    tool_call: AIToolCall | None = None
    tool_result: AIToolResult | None = None
    artifact: AIArtifact | None = None
    usage: AIUsage | None = None
    result: AIResult | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def started(execution_id: str) -> "AIEvent":
        return AIEvent(kind=AIEventKind.EXECUTION_STARTED, execution_id=execution_id)

    @staticmethod
    def content(execution_id: str, text: str) -> "AIEvent":
        return AIEvent(kind=AIEventKind.CONTENT_DELTA, execution_id=execution_id, text=text)

    @staticmethod
    def structured(execution_id: str, value: Any) -> "AIEvent":
        return AIEvent(
            kind=AIEventKind.STRUCTURED_DELTA, execution_id=execution_id, value=value,
        )

    @staticmethod
    def tool_requested(execution_id: str, call: AIToolCall) -> "AIEvent":
        return AIEvent(
            kind=AIEventKind.TOOL_CALL_REQUESTED, execution_id=execution_id, tool_call=call,
        )

    @staticmethod
    def tool_accepted(execution_id: str, result: AIToolResult) -> "AIEvent":
        return AIEvent(
            kind=AIEventKind.TOOL_RESULT_ACCEPTED, execution_id=execution_id, tool_result=result,
        )

    @staticmethod
    def artifact_produced(execution_id: str, artifact: AIArtifact) -> "AIEvent":
        return AIEvent(
            kind=AIEventKind.ARTIFACT_PRODUCED, execution_id=execution_id, artifact=artifact,
        )

    @staticmethod
    def provider_changed(
        *, from_provider: str, to_provider: str, reason: str = "",
    ) -> "AIEvent":
        """A fallback moved this execution to the next configured provider.

        Model-invisible -- the model sees nothing of it -- but never
        observer-invisible. Without this event a provider switch would be a
        silent substitution, which is the failure the fallback domain exists to
        make visible.
        """
        return AIEvent(
            kind=AIEventKind.PROVIDER_CHANGED,
            error=reason,
            metadata={"from": from_provider, "to": to_provider},
        )

    @staticmethod
    def usage_updated(execution_id: str, usage: AIUsage) -> "AIEvent":
        return AIEvent(kind=AIEventKind.USAGE_UPDATED, execution_id=execution_id, usage=usage)

    @staticmethod
    def completed(execution_id: str, result: AIResult) -> "AIEvent":
        return AIEvent(
            kind=AIEventKind.EXECUTION_COMPLETED, execution_id=execution_id, result=result,
        )

    @staticmethod
    def failed(execution_id: str, error: str) -> "AIEvent":
        return AIEvent(kind=AIEventKind.EXECUTION_FAILED, execution_id=execution_id, error=error)
