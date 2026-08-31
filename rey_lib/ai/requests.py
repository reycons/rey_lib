"""What a caller asked for, and what was actually decided.

Two values, kept apart on purpose:

    AIRequest          the caller's intent, immutable, possibly incomplete
    ResolvedAIRequest  what the runtime decided to execute, immutable, complete

Resolution never mutates the request. That is what makes an already-resolved
execution immune to a settings change made while it is in flight, and what lets
a result say what actually ran rather than what is currently selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from rey_lib.ai.content import AIInput
from rey_lib.ai.instructions import AIInstruction
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.tools import AITool, AIToolCall, AIToolResult

__all__ = ["AIOutputSpec", "AIOutputKind", "AIRequest", "AIRequestOptions", "ResolvedAIRequest"]


class AIOutputKind(str, Enum):
    """What shape the caller needs back."""

    TEXT = "text"
    JSON = "json"
    SCHEMA = "schema"
    CONTRACT = "contract"


@dataclass(frozen=True)
class AIOutputSpec:
    """The caller's output requirement.

    First-class, because structured output added afterwards to a text-shaped API
    is how structured results end up stringified. ``CONTRACT`` defers the shape
    to the resolved instruction, which is the only one that knows it.

    ``media_type`` states the *representation* of the payload, separately from
    its structure. Empty means unstated.
    """

    kind: AIOutputKind = AIOutputKind.TEXT
    schema: dict[str, Any] | None = None
    media_type: str = ""

    @staticmethod
    def text(media_type: str = "") -> "AIOutputSpec":
        return AIOutputSpec(media_type=media_type)

    @staticmethod
    def markdown() -> "AIOutputSpec":
        """Text whose canonical consumer representation is Markdown.

        Deliberately **not** a new ``AIOutputKind``. TEXT versus STRUCTURED is a
        structural distinction; Markdown versus plain text is a representation
        one, and putting both in one enum would conflate serialization structure
        with presentation format. The estate already expresses "same structure,
        different content format" as a media type -- ``AIArtifact.media_type``
        -- and this reuses that vocabulary.
        """
        return AIOutputSpec(media_type="text/markdown")

    @staticmethod
    def json() -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.JSON)

    @staticmethod
    def schema_of(schema: dict[str, Any]) -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.SCHEMA, schema=schema)

    @staticmethod
    def from_contract() -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.CONTRACT)

    def is_structured(self) -> bool:
        return self.kind is not AIOutputKind.TEXT


@dataclass(frozen=True)
class AIRequestOptions:
    """Execution options with cross-provider meaning.

    Not a bag of provider parameters: an option that only one provider
    understands belongs in that adapter, reached through the profile's own
    options, not in the canonical request.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


@dataclass(frozen=True)
class AIRequest:
    """One caller's intent.

    ``profile_id`` and ``instruction_id`` are *overrides*. Absent, the runtime's
    settings apply; present, they win -- so a governed operation never silently
    depends on whatever an operator last selected.

    ``task`` names what the AI is being asked to do, and is how a caller stops
    naming a profile at all: the runtime resolves the settings configured for
    that purpose. Optional, and empty means the defaults -- so a caller that
    knows nothing about tasks behaves exactly as it did.

    The precedence, for every task-aware setting:

        this request's override  ->  the task's override  ->  the default
    """

    input: AIInput
    task: str = ""
    profile_id: str = ""
    instruction_id: str = ""
    instruction: AIInstruction | None = None
    output: AIOutputSpec = field(default_factory=AIOutputSpec)
    tools: tuple[AITool, ...] = ()
    options: AIRequestOptions = field(default_factory=AIRequestOptions)
    context: dict[str, Any] = field(default_factory=dict)
    cancelled: Callable[[], bool] | None = None
    tool_runner: Callable[[AIToolCall], AIToolResult] | None = None

    @staticmethod
    def prompt(value: str, **fields: Any) -> "AIRequest":
        """The simple case, without making it the canonical shape."""
        return AIRequest(input=AIInput.prompt(value), **fields)


@dataclass(frozen=True)
class ResolvedAIRequest:
    """What the runtime decided to execute.

    A fact, not an intent. It names the profile and instruction objects
    themselves rather than their ids, because by this point the choice is made
    and re-resolving it downstream would be a second decision.

    ``envelope_key`` records an envelope that **resolution itself introduced**
    around the caller's value -- and nothing else. Normalization removes exactly
    this key and never inspects the caller's own schema, which is what stops a
    legitimate payload that happens to use ``result`` or ``content`` from being
    silently unwrapped. Empty means resolution introduced no envelope, and
    normalization therefore removes nothing.
    """

    input: AIInput
    profile: AIProfile
    instruction: AIInstruction
    output: AIOutputSpec
    tools: tuple[AITool, ...] = ()
    options: AIRequestOptions = field(default_factory=AIRequestOptions)
    context: dict[str, Any] = field(default_factory=dict)
    cancelled: Callable[[], bool] | None = None
    tool_runner: Callable[[AIToolCall], AIToolResult] | None = None
    session_id: str = ""
    envelope_key: str = ""

    @property
    def schema(self) -> dict[str, Any] | None:
        """The shape the output must satisfy, from wherever it was stated.

        The request's own schema wins; a contract-defined output falls back to
        the instruction's. Asked once, here, so execution does not re-derive it.
        """
        if self.output.schema is not None:
            return self.output.schema
        if self.output.kind is AIOutputKind.CONTRACT:
            return self.instruction.schema
        return None
