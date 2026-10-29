"""What kind of instruction a governed AI task carries.

The kinds ``ai.ai_instruction.kind`` states:

    NONE        no instruction at all
    RAW         instruction text supplied directly
    CONTRACT    a governed contract, whose body the task carries
    STRUCTURED  a contract that also requires a validated output shape
"""

from __future__ import annotations

from enum import Enum

__all__ = ["AIInstructionKind"]


class AIInstructionKind(str, Enum):
    """What kind of instruction this is."""

    NONE = "none"
    RAW = "raw"
    CONTRACT = "contract"
    STRUCTURED = "structured"
