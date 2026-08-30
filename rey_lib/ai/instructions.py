"""What the model is told to do, as a domain value.

Four kinds, which is the domain rather than the Console's vocabulary:

    NONE        no instruction at all
    RAW         instruction text supplied directly
    CONTRACT    a configured contract, addressed by its own identity
    STRUCTURED  a contract that also requires a validated output shape

The old Console carried ``__none__``, ``__text_prompt__`` and
``__text_prompt_only__`` as magic strings in a selector, and every consumer that
touched them had to know what they meant. Two of the three described the *input*
a caller sends rather than the instruction itself -- Text Prompt and Text Prompt
Only ran identically and differed only in whether Data accompanied them. That is
a property of the request, not of the instruction, so it does not survive here:
an instruction says what to do, and what is sent is ``AIInput``'s business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["AIInstruction", "AIInstructionKind"]


class AIInstructionKind(str, Enum):
    """What kind of instruction this is."""

    NONE = "none"
    RAW = "raw"
    CONTRACT = "contract"
    STRUCTURED = "structured"


@dataclass(frozen=True)
class AIInstruction:
    """One instruction, however it is expressed.

    Attributes
    ----------
    id:
        What addresses it. ``""`` for the absence of an instruction.
    kind:
        Which of the four this is.
    name:
        What a reader sees.
    text:
        The instruction body, for a raw instruction or a resolved contract.
    reference:
        Where a contract's body comes from, for one that is loaded rather than
        carried. Resolved by the contract mechanism, never read by the root.
    schema:
        The output shape a structured instruction requires.
    metadata:
        Presentation-safe facts. Carried, never read here.
    """

    id: str = ""
    kind: AIInstructionKind = AIInstructionKind.NONE
    name: str = ""
    text: str = ""
    reference: str = ""
    schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.id or "No instruction"

    def requires_structured_output(self) -> bool:
        """Whether satisfying this instruction means validating a shape."""
        return self.kind is AIInstructionKind.STRUCTURED and self.schema is not None


#: The absence of an instruction, as a value rather than a magic string.
NONE = AIInstruction()
