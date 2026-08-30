"""What came back, in terms an application can use without knowing a provider.

Every value here is immutable and presentation-neutral. Nothing names a viewer,
a panel or a region: how a result is shown is the application's, and a result
that carried a rendering instruction would make the domain answer a question it
cannot see.

``AIExecutionInfo`` is the reason a result stays intelligible later. It records
what actually ran -- profile, provider, model, instruction, timings, attempts --
so changing a default tomorrow cannot make yesterday's result ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rey_lib.ai.tools import AIToolCall, AIToolResult

__all__ = [
    "AIArtifact",
    "AIEvidence",
    "AIExecutionInfo",
    "AIFinishReason",
    "AIOutput",
    "AIOutputForm",
    "AIResult",
    "AIUsage",
]


class AIOutputForm(str, Enum):
    """What form the output actually came back in."""

    TEXT = "text"
    STRUCTURED = "structured"
    TOOL_CALLS = "tool_calls"


class AIFinishReason(str, Enum):
    """Why execution stopped."""

    COMPLETED = "completed"
    TOOL_CALLS = "tool_calls"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class AIOutput:
    """The output, in the form it actually has.

    A structured result keeps its value. It is not stringified to simplify the
    API -- an application that wanted the object would have to parse the text
    back, which is the round trip this exists to prevent.

    Already normalized by the time it is here: any envelope resolution
    introduced has been removed, so ``value`` is the caller's own payload and
    ``text`` is the caller's own text. ``media_type`` carries the representation
    the contract asked for, so a Markdown contract answers Markdown.
    """

    form: AIOutputForm = AIOutputForm.TEXT
    text: str = ""
    value: Any = None
    media_type: str = ""

    @staticmethod
    def of_text(value: str, media_type: str = "") -> "AIOutput":
        return AIOutput(form=AIOutputForm.TEXT, text=value, media_type=media_type)

    @staticmethod
    def of_value(value: Any, text: str = "", media_type: str = "") -> "AIOutput":
        return AIOutput(
            form=AIOutputForm.STRUCTURED, text=text, value=value, media_type=media_type,
        )


@dataclass(frozen=True)
class AIUsage:
    """What the execution consumed, where the provider reports it.

    Zero means "not reported", as the old envelope also meant. No accounting
    guarantee is invented on top of a provider that does not give one.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AIArtifact:
    """A durable reference to something the execution produced.

    **A reference, never persistence.** AI produces a value; the estate's
    run-artifact owner persists and names it. That ownership is not this
    subsystem's and is not migrating here: ``rey_lib.files.file_utils`` owns the
    universal ``<artifact_name>.<run_timestamp>.<extension>`` convention, and
    the old LLM subsystem followed it as a caller "like every other Rey
    run-created artifact" -- it never owned it either.

    So the conversion boundary is::

        AI            produces the value and this reference
        run-artifact  names and persists it under the estate convention
        owner

    The converter is the **caller's** to supply at attachment: something holding
    both a run and this reference turns one into the other. Nothing in
    ``rey_lib.ai`` writes a file, and a future version that did would be taking
    ownership the estate already assigned elsewhere.

    ``uri`` is empty until that owner has persisted it -- an artifact that has
    been produced but not yet stored is a real state, and it says so rather than
    carrying a path that does not exist yet.
    """

    id: str
    uri: str = ""
    media_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIEvidence:
    """Provenance, kept so audit and logging do not each invent a form.

    ``provider_raw`` is the provider's own payload, retained for replay. It
    never reaches an application as a provider object -- it is data here, and
    nothing in the subsystem reads it.
    """

    payload_id: str = ""
    prompt_digest: str = ""
    provider_raw: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIAttempt:
    """One try, and what became of it."""

    number: int
    failed: bool = False
    error: str = ""


@dataclass(frozen=True)
class AIExecutionInfo:
    """What actually ran."""

    execution_id: str
    profile_id: str = ""
    provider: str = ""
    model: str = ""
    instruction_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    attempts: tuple[AIAttempt, ...] = ()
    finish_reason: AIFinishReason = AIFinishReason.COMPLETED

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


@dataclass(frozen=True)
class AIResult:
    """One completed execution, whole.

    **The public invariant: successful output is already normalized.** By the
    time a result exists, execution structure has been removed and the caller
    reads its own content directly. A caller never unwraps, and never reaches
    something like ``result.output.value["result"]["content"]``.

    ``owns != implements``: the invariant is this object's, the work is the
    output normalizer's, which runs where the resolved contract and the raw
    provider output are both still in hand. This frozen value does not inspect
    itself.

    Usage, artifacts, tool exchange and execution evidence stay *beside* the
    payload rather than inside it, so nothing an application reads as content
    is really machinery.
    """

    output: AIOutput
    execution: AIExecutionInfo
    usage: AIUsage = field(default_factory=AIUsage)
    tool_calls: tuple[AIToolCall, ...] = ()
    tool_results: tuple[AIToolResult, ...] = ()
    artifacts: tuple[AIArtifact, ...] = ()
    evidence: AIEvidence = field(default_factory=AIEvidence)

    @property
    def finish_reason(self) -> AIFinishReason:
        return self.execution.finish_reason

    @property
    def text(self) -> str:
        """The canonical textual payload, where the output has one.

        Never a stringified value, and never an execution wrapper: for a
        contract whose representation is Markdown text, this is the Markdown.
        """
        return self.output.text

    @property
    def value(self) -> Any:
        """The caller's own structured payload, already unwrapped."""
        return self.output.value

    @property
    def media_type(self) -> str:
        """The representation the output contract asked for, if it named one."""
        return self.output.media_type
