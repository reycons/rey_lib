"""The tool protocol, and the line it draws.

    AI owns the tool invocation protocol.
    The application owns what a tool does.

So a declaration says what a tool is called, what it takes and what it is for; a
call says the model asked for it; a result says what came back. Nothing here
executes anything, and no business behaviour lives in this subsystem.

The old provider base advertised ``supports_tools`` and its response envelope
carried no tool call, so the capability was declared and unimplemented. That gap
is why this is first-class here rather than added later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["AITool", "AIToolCall", "AIToolResult"]


@dataclass(frozen=True)
class AITool:
    """One tool an application is offering for this request."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIToolCall:
    """The model asked for a tool, with these arguments."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIToolResult:
    """What the application's tool answered.

    ``failed`` is stated rather than inferred from an empty value: a tool that
    legitimately returns nothing is not a failure, and the two must not look
    alike to the execution that continues from here.
    """

    call_id: str
    value: Any = None
    failed: bool = False
    message: str = ""
