"""The tool continuation seam.

    AI owns the tool invocation protocol.
    The application owns what a tool does.

``ToolRunner`` is the application's side: given the calls a model asked for, it
answers with results. Nothing in this subsystem executes business behaviour.

``ToolLoop`` is the Rey-owned seam for the *continuation* itself -- accept
results, append turns, ask again, and stop. It is a named protocol rather than a
loop inlined into the coordinator so that an implementation can be substituted
behind it without the coordinator learning anything about the substitute. That
is the whole reason it exists as a seam:

    ExecutionCoordinator -> ToolLoop (Rey-owned) -> some implementation

and never ``ExecutionCoordinator -> some framework's agent architecture``.

The continuation rule, which any implementation must honour:

    Construct the next provider turn from the **original resolved execution
    configuration** plus accumulated call/result history. Do not re-resolve
    application settings or capabilities.

A second model round is not a second chance for configuration to change
underneath an execution. What may change is history; what may not is the
decision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Protocol

from rey_lib.ai.errors import AIToolError
from rey_lib.ai.policies import AIExecutionPolicy
from rey_lib.ai.providers.base import ProviderReply
from rey_lib.ai.requests import ResolvedAIRequest
from rey_lib.ai.state import ExecutionState
from rey_lib.ai.streaming import AIEvent
from rey_lib.ai.tools import AIToolCall, AIToolResult

__all__ = ["CanonicalToolLoop", "TakeTurn", "ToolLoop", "ToolRunner"]


class ToolRunner(Protocol):
    """What the application supplies: how a requested tool is carried out."""

    def __call__(self, call: AIToolCall) -> AIToolResult:
        """Carry out one call and answer with its result."""


class TakeTurn(Protocol):
    """How the loop asks for one more provider turn.

    Supplied by the coordinator, so the loop drives continuation without owning
    provider transport, retry, replay safety or fallback -- those belong to the
    turn executor and stay there.
    """

    def __call__(
        self, request: ResolvedAIRequest, state: ExecutionState,
    ) -> ProviderReply:
        """Take one more turn against the already-resolved configuration."""


class ToolLoop(ABC):
    """Drives continuation until the model stops asking for tools."""

    @abstractmethod
    def run(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        reply: ProviderReply,
        *,
        take_turn: TakeTurn,
        runner: ToolRunner,
        emit: Callable[[AIEvent], None],
    ) -> ProviderReply:
        """Continue from a reply that asked for tools, to one that does not.

        Args:
            request: The resolved configuration. Never re-resolved.
            state: The accumulation, which the loop extends.
            reply: The reply that requested tool calls.
            take_turn: How to ask the provider again.
            runner: The application's tool execution.
            emit: Canonical event sink.

        Returns:
            The first reply that requested no tool calls.
        """


class CanonicalToolLoop(ToolLoop):
    """The estate's continuation: accept, append, re-ask, bounded.

    Two budgets apply and neither spends the other:

        ExecutionBudget.max_turns      every provider turn, including these
        ToolCorrectionPolicy          turns taken because a tool *failed*

    A tool that answers normally costs a turn and no correction. A tool that
    fails costs a turn *and* a correction, because asking the model to proceed
    after a failure is a correction whether or not it also produced results.
    """

    def __init__(self, *, policy: AIExecutionPolicy) -> None:
        self._policy = policy

    def run(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        reply: ProviderReply,
        *,
        take_turn: TakeTurn,
        runner: ToolRunner,
        emit: Callable[[AIEvent], None],
    ) -> ProviderReply:
        """Continue until the model answers without asking for a tool."""
        current = reply

        while current.tool_calls:
            state.requested_tools(current.tool_calls)
            for call in current.tool_calls:
                emit(AIEvent.tool_requested(state.execution_id, call))

            results = self._carry_out(current.tool_calls, runner)
            state.accepted_tool_results(results)
            for result in results:
                emit(AIEvent.tool_accepted(state.execution_id, result))

            if any(result.failed for result in results):
                taken = state.corrected_tool()
                if self._policy.tool_correction.exhausted(taken - 1):
                    raise AIToolError(
                        "A tool call failed and the tool-correction budget of "
                        f"{self._policy.tool_correction.max_corrections} is spent."
                    )

            if self._policy.budget.exhausted(state.turns_taken):
                raise AIToolError(
                    "The model asked for another tool and the execution budget "
                    f"of {self._policy.budget.max_turns} turns is spent."
                )

            current = take_turn(request, state)

        return current

    @staticmethod
    def _carry_out(
        calls: tuple[AIToolCall, ...], runner: ToolRunner,
    ) -> tuple[AIToolResult, ...]:
        """Every requested call, carried out, with failures stated not raised.

        A tool that raises is turned into a failed result rather than ending the
        execution: the model asked for it, so the model is told what happened
        and decides. An exception escaping here would make one tool's problem
        the whole run's.
        """
        results: list[AIToolResult] = []
        for call in calls:
            try:
                results.append(runner(call))
            except Exception as exc:  # noqa: BLE001 -- a tool must not end a run
                results.append(
                    AIToolResult(
                        call_id=call.id,
                        failed=True,
                        message=f"Tool '{call.name}' failed: {exc}",
                    ),
                )
        return tuple(results)
