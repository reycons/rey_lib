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

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from rey_lib.ai.errors import AIConfigurationError, AIToolError
from rey_lib.errors.error_utils import AppError
from rey_lib.logs.app_logger import AppLogger

logger = AppLogger(__name__)

__all__ = ["AITool", "AIToolCall", "AIToolResult", "AIToolSet"]

#: The operation a runtime binds to a capability_key: it receives the validated
#: arguments and answers by calling the canonical owner. Never implemented here.
Operation = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class AITool:
    """One AI-callable capability (backlog 1023).

    A governed declaration: what the model is told, the arguments it takes,
    whether the same arguments may run more than once in one execution
    (``replay_safe``), and ``capability_key`` -- the binding identity a runtime
    maps to the operation that reaches the canonical owner. It translates a call
    and owns no domain behaviour.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    replay_safe: bool = False
    capability_key: str = ""

    def validated(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments, checked against this tool's schema.

        Pydantic AI does not validate arguments for a schema-declared tool, so
        the tool does: a call the schema refuses never reaches its owner.

        Raises:
            AIToolError: naming the first place the arguments break the schema.
        """
        import jsonschema  # noqa: PLC0415 -- used only where a call is validated

        given = dict(arguments)
        errors = sorted(
            jsonschema.Draft7Validator(self.input_schema or {}).iter_errors(given),
            key=lambda error: list(error.path),
        )
        if errors:
            where = "/".join(str(part) for part in errors[0].path) or "(root)"
            raise AIToolError(
                f"Tool '{self.name}' was called with arguments that break its "
                f"schema at {where}: {errors[0].message}"
            )
        return given

    def replay_key(self, arguments: Mapping[str, Any]) -> str:
        """This tool and these arguments, normalized: one call's identity."""
        return json.dumps(
            {"tool": self.name, "arguments": dict(arguments)}, sort_keys=True, default=str,
        )

    def call(self, arguments: Mapping[str, Any], operation: Operation) -> Any:
        """Validate, then hand the call to the operation that reaches the owner."""
        validated = self.validated(arguments)
        logger.debug("AI tool %s calling capability %s", self.name, self.capability_key)
        return operation(validated)


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


class AIToolSet:
    """The tools offered to one execution, bound to what carries them out.

    Built at execution from a task's governed tool facts and the runtime's
    ``capability_key -> operation`` mapping: the database supplies the facts and
    never the behaviour. One per execution, so the replay record is that
    execution's and nothing persists.

    It owns the set and coordinates every call: a tool's arguments are validated
    by the tool, a non-replay-safe tool runs at most once per identical
    arguments -- a repeat receives the first result -- and the set hands Pydantic
    AI its tools.

    Raises:
        AIConfigurationError: on construction, when two tools share a name or a
            tool's capability_key is bound to nothing in this runtime.
    """

    def __init__(self, tools: Sequence[AITool], operations: Mapping[str, Operation]) -> None:
        self._tools: dict[str, AITool] = {}
        self._operations: dict[str, Operation] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise AIConfigurationError(f"Two AI tools are offered as '{tool.name}'.")
            operation = operations.get(tool.capability_key) if tool.capability_key else None
            if operation is None:
                raise AIConfigurationError(
                    f"AI tool '{tool.name}' names capability "
                    f"'{tool.capability_key or '(none)'}', which this runtime does not bind."
                )
            self._tools[tool.name] = tool
            self._operations[tool.name] = operation
        self._carried: dict[str, Any] = {}

    def tools(self) -> tuple[AITool, ...]:
        """The tools offered, in the order configuration gave them."""
        return tuple(self._tools.values())

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Carry out one call, honouring the tool's replay safety.

        Raises:
            AIToolError: when no such tool is offered, or its arguments break
                its schema.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise AIToolError(f"No AI tool is offered as '{name}'.")
        validated = tool.validated(arguments)
        key = tool.replay_key(validated)
        if not tool.replay_safe and key in self._carried:
            logger.debug("AI tool %s not run again for the same arguments", name)
            return self._carried[key]
        value = tool.call(validated, self._operations[name])
        if not tool.replay_safe:
            self._carried[key] = value
        return value

    def pydantic_tools(self) -> list[Any]:
        """The tools as Pydantic AI carries them.

        A schema the tool validates itself, ``sequential`` for a tool that is
        not replay-safe so concurrency cannot defeat its once-only guarantee,
        and Rey's outcomes in Pydantic AI's terms: arguments that break the
        schema ask the model to correct them (``ModelRetry``); a refusal by the
        canonical owner is that call's failure, which the model may adapt to
        (``ToolFailed``).
        """
        from pydantic_ai import ModelRetry, Tool  # noqa: PLC0415 -- AI's dependency only
        from pydantic_ai.exceptions import ToolFailed  # noqa: PLC0415

        def carried(name: str) -> Callable[..., Any]:
            def run(**arguments: Any) -> Any:
                try:
                    return self.invoke(name, arguments)
                except AIToolError as invalid:
                    raise ModelRetry(str(invalid)) from invalid
                except AppError as refused:
                    raise ToolFailed(str(refused)) from refused
            return run

        return [
            Tool.from_schema(
                carried(tool.name),
                name=tool.name,
                description=tool.description,
                json_schema=tool.input_schema or {"type": "object", "properties": {}},
                sequential=not tool.replay_safe,
            )
            for tool in self._tools.values()
        ]
