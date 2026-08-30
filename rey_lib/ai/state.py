"""What one execution has accumulated so far.

The other half of the resolution boundary:

    ResolvedAIRequest  immutable configuration -- what was decided
    ExecutionState     mutable accumulation    -- what has happened since

Kept apart deliberately. Appending turns to the resolved request would make the
decision and the history one object, and a continuation would then be unable to
say whether a value was chosen or accrued. Everything here changes during an
execution; nothing in ``ResolvedAIRequest`` does.

This is also what makes tool continuation expressible. Before it existed the
accumulated turns, attempts and counts were local variables inside one
generator, so there was nowhere for a second provider turn to continue *from*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rey_lib.ai.content import AIMessage
from rey_lib.ai.results import AIAttempt, AIUsage
from rey_lib.ai.tools import AIToolCall, AIToolResult

__all__ = ["ExecutionState"]


@dataclass
class ExecutionState:
    """The mutable accumulation of one execution.

    Attributes
    ----------
    execution_id:
        What this execution is called, for the whole of its life.
    turns:
        Canonical turns accrued *after* the resolved request's own input: the
        assistant turns that asked for tools, and the turns that answered them.
        A continuation sends the request's input followed by these.
    tool_calls / tool_results:
        The exchange, kept first-class alongside the turns so a result can say
        what it answered without a reader parsing the history back.
    attempts:
        Every provider attempt, failed or not. Transport-level, so a correction
        turn does not appear here.
    turns_taken:
        Provider turns spent, against ``ExecutionBudget``.
    tool_corrections / validation_corrections:
        Model-visible correction counts, each against its own policy. Separate
        from ``attempts`` because they mean different things and must not spend
        each other's budget.
    """

    execution_id: str
    turns: list[AIMessage] = field(default_factory=list)
    tool_calls: list[AIToolCall] = field(default_factory=list)
    tool_results: list[AIToolResult] = field(default_factory=list)
    attempts: list[AIAttempt] = field(default_factory=list)
    usage: AIUsage = field(default_factory=AIUsage)
    turns_taken: int = 0
    tool_corrections: int = 0
    validation_corrections: int = 0
    cancelled: bool = False

    # -- provider turns ----------------------------------------------------

    def began_turn(self) -> int:
        """Count a provider turn about to be taken, and answer which it is."""
        self.turns_taken += 1
        return self.turns_taken

    def record_attempt(self, attempt: AIAttempt) -> None:
        """Record one provider attempt, successful or failed."""
        self.attempts.append(attempt)

    def add_usage(self, usage: AIUsage) -> None:
        """Accumulate what a turn consumed.

        Summed rather than replaced: a continuation's cost is the whole
        execution's, and a caller reading the last turn's usage would understate
        a multi-turn run.
        """
        self.usage = AIUsage(
            input_tokens=self.usage.input_tokens + usage.input_tokens,
            output_tokens=self.usage.output_tokens + usage.output_tokens,
        )

    # -- the tool exchange -------------------------------------------------

    def requested_tools(self, calls: tuple[AIToolCall, ...]) -> None:
        """Record that the assistant asked for these calls, and keep the turn."""
        if not calls:
            return
        self.tool_calls.extend(calls)
        self.turns.append(AIMessage.asked_for_tools(calls))

    def accepted_tool_results(self, results: tuple[AIToolResult, ...]) -> None:
        """Record answers to those calls, and keep a turn for each."""
        for result in results:
            self.tool_results.append(result)
            self.turns.append(
                AIMessage.tool_answer(result.call_id, _answer_value(result)),
            )

    # -- corrections -------------------------------------------------------

    def corrected_tool(self) -> int:
        """Count one model-visible tool correction, and answer the new count."""
        self.tool_corrections += 1
        return self.tool_corrections

    def corrected_validation(self) -> int:
        """Count one model-visible validation correction, and answer the count."""
        self.validation_corrections += 1
        return self.validation_corrections

    def correction(self, message: AIMessage) -> None:
        """Keep a correction turn in the history the model will see."""
        self.turns.append(message)

    # -- what a continuation sends ----------------------------------------

    def accumulated(self) -> tuple[AIMessage, ...]:
        """The turns accrued so far, in order."""
        return tuple(self.turns)

    def has_history(self) -> bool:
        """Whether anything has accrued that a continuation would carry."""
        return bool(self.turns)


def _answer_value(result: AIToolResult) -> object:
    """What a tool's answer contributes to the turn the model sees.

    A failure is stated rather than sent as an empty value, because a tool that
    legitimately returned nothing and one that failed must not look alike to the
    model deciding what to do next.
    """
    if result.failed:
        return {"error": result.message or "The tool call failed."}
    return result.value
