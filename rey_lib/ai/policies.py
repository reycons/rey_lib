"""The control domains of one execution, kept apart.

Five domains and one predicate. They are separate because they mean different
things, not merely because they count separately:

    ExecutionBudget             how many provider turns an execution may take
    TransportRetryPolicy        repeating an equivalent provider operation
    FallbackPolicy              moving to the next provider/model in sequence
    ToolCorrectionPolicy        asking the model again after a tool problem
    ValidationCorrectionPolicy  asking the model again after invalid output
    ReplaySafety                whether repeating an operation is *legal*

Two axes cut across them, and conflating either is how a budget silently spends
another's:

    model-visible   a correction extends conversation history; the model sees
                    what went wrong and answers again. Transport retry and
                    fallback do not -- they repeat or relocate the same turn.
    observable      all of them may be reported through canonical events and
                    evidence. Model-invisible is not observer-invisible: a
                    fallback that no one can see is a silent provider switch.

``ReplaySafety`` is deliberately not a budget. It counts nothing and has no
exhaustion. It answers whether a repeat is permitted at all, and a transport
retry may proceed only when::

    policy permits retry  AND  replay safety permits replay

That distinction exists because an operation is only "equivalent" if repeating
it is invisible outside. A provider that has begun emitting a response has
already been observed, and a stateful request has already moved something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from rey_lib.ai.errors import AIError, AIOutputError, AIProviderError

__all__ = [
    "AIExecutionPolicy",
    "DEFAULT_EXECUTION_POLICY",
    "ExecutionBudget",
    "FallbackPolicy",
    "NO_RETRY",
    "ReplayClassification",
    "ReplayFacts",
    "ReplaySafety",
    "ToolCorrectionPolicy",
    "TransportRetryPolicy",
    "ValidationCorrectionPolicy",
]


@dataclass(frozen=True)
class ExecutionBudget:
    """How many provider turns one execution may take.

    Spent by turns, including continuation turns after tool results. Not spent
    by transport retries, which repeat a turn rather than taking a new one.
    """

    max_turns: int = 8

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError(
                f"ExecutionBudget.max_turns must be >= 1, got {self.max_turns}."
            )

    def exhausted(self, turns_taken: int) -> bool:
        """Whether another turn would exceed the budget."""
        return turns_taken >= self.max_turns


class ReplayClassification(str, Enum):
    """What a provider says about repeating an operation."""

    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReplayFacts:
    """What is known about one failed operation, for the replay question.

    Reported by the adapter, which is the only thing that knows. ``UNKNOWN`` is
    the honest default and is treated as unsafe: a provider that has not said
    whether a repeat is safe has not said that it is.
    """

    response_started: bool = False
    classification: ReplayClassification = ReplayClassification.UNKNOWN
    stateful: bool = False


@dataclass(frozen=True)
class ReplaySafety:
    """Whether repeating a provider operation is legal. Counts nothing.

    ``approve_unsafe_replay`` is separate from any retry decision on purpose:
    deciding to retry never by itself authorises replaying work that may
    already have happened. It is set only where repeating provider-side effects
    is acceptable to the caller.
    """

    approve_unsafe_replay: bool = False

    def permits(self, facts: ReplayFacts) -> bool:
        """Whether this failed operation may be repeated at all."""
        if self.approve_unsafe_replay:
            return True
        if facts.response_started:
            return False
        if facts.stateful:
            return False
        return facts.classification is ReplayClassification.SAFE

    def refusal(self, facts: ReplayFacts) -> str:
        """Why a replay was refused, for evidence. Empty when it was permitted."""
        if self.permits(facts):
            return ""
        if facts.response_started:
            return "the provider had already begun emitting a response"
        if facts.stateful:
            return "the request carried provider-side conversation state"
        if facts.classification is ReplayClassification.UNSAFE:
            return "the provider classified the operation as unsafe to replay"
        return "the provider did not say the operation was safe to replay"


@dataclass(frozen=True)
class TransportRetryPolicy:
    """Repeating an equivalent provider operation.

    Model-invisible: it alters no conversation history and consumes no turn or
    correction budget. ``AIOutputError`` is refused as retryable at
    construction, and the reasoning is unchanged from the original policy --
    output that does not satisfy its shape is a failed execution, not a slow
    one, and repeating the same call asks the same question and pays twice.
    Feeding the failure back to the model is a *correction*, which is
    ``ValidationCorrectionPolicy`` and a different budget entirely.
    """

    attempts: int = 3
    backoff_seconds: float = 0.0
    backoff_factor: float = 2.0
    retry_on: tuple[type[AIError], ...] = field(default=(AIProviderError,))

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(
                f"TransportRetryPolicy.attempts must be >= 1, got {self.attempts}."
            )
        if self.backoff_seconds < 0:
            raise ValueError("TransportRetryPolicy.backoff_seconds must not be negative.")
        if AIOutputError in self.retry_on:
            raise ValueError(
                "AIOutputError must not be transport-retryable. Output that does "
                "not satisfy its shape is a failed execution, not a transient "
                "one; feeding it back to the model is a validation correction."
            )

    def retryable(self, failure: BaseException) -> bool:
        """Whether this failure is the kind worth repeating."""
        return isinstance(failure, self.retry_on)

    def delay_before(self, attempt: int) -> float:
        """How long to wait before ``attempt``, which is 1-based.

        Zero before the first attempt, and zero throughout when no backoff is
        configured, so a caller that wants none pays nothing for the mechanism.
        """
        if attempt <= 1 or self.backoff_seconds <= 0:
            return 0.0
        return self.backoff_seconds * (self.backoff_factor ** (attempt - 2))


@dataclass(frozen=True)
class FallbackPolicy:
    """Moving to the next configured provider/model after one is exhausted.

    Names providers already configured for this runtime, in order. Selecting
    from it is execution's, and resolving what those names mean happened at
    resolution -- a fallback never re-reads application configuration, because
    that would let an execution change what it is halfway through.

    Model-invisible, but not observer-invisible: a transition is reported
    through canonical events so a switch is never silent.
    """

    sequence: tuple[str, ...] = ()

    def after(self, used: tuple[str, ...]) -> str:
        """The next provider to try, or empty when the sequence is spent."""
        for name in self.sequence:
            if name not in used:
                return name
        return ""

    def exhausted(self, used: tuple[str, ...]) -> bool:
        """Whether every configured fallback has been tried."""
        return not self.after(used)


@dataclass(frozen=True)
class ToolCorrectionPolicy:
    """Asking the model again after a tool call could not be carried out.

    Model-visible: the correction becomes a turn the model sees. It spends this
    budget *and* a turn from ``ExecutionBudget``, because a correction is a real
    provider turn -- unlike a transport retry, which is neither.
    """

    max_corrections: int = 2

    def __post_init__(self) -> None:
        if self.max_corrections < 0:
            raise ValueError(
                "ToolCorrectionPolicy.max_corrections must be >= 0, got "
                f"{self.max_corrections}."
            )

    def exhausted(self, taken: int) -> bool:
        """Whether another tool correction would exceed the budget."""
        return taken >= self.max_corrections


@dataclass(frozen=True)
class ValidationCorrectionPolicy:
    """Asking the model again after its output did not satisfy the contract.

    Model-visible, and it extends history with the validation failure so the
    model is told what was wrong rather than asked the same question again.
    Distinct from the ``AIOutputError`` transport prohibition, which forbids
    repeating the identical call -- a different mechanism with a different
    budget.

    Defaults to zero. Correcting output is a deliberate choice, and a runtime
    that has not asked for it keeps the original refusal behaviour.
    """

    max_corrections: int = 0

    def __post_init__(self) -> None:
        if self.max_corrections < 0:
            raise ValueError(
                "ValidationCorrectionPolicy.max_corrections must be >= 0, got "
                f"{self.max_corrections}."
            )

    def exhausted(self, taken: int) -> bool:
        """Whether another validation correction would exceed the budget."""
        return taken >= self.max_corrections


@dataclass(frozen=True)
class AIExecutionPolicy:
    """Every control domain for one execution, in one value.

    Held together so an execution is handed one policy rather than five
    arguments, and so a runtime states its whole control posture in a place a
    reader can see at once. The domains themselves stay independent: nothing
    here lets one spend another's budget.
    """

    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    transport: TransportRetryPolicy = field(default_factory=TransportRetryPolicy)
    replay: ReplaySafety = field(default_factory=ReplaySafety)
    fallback: FallbackPolicy = field(default_factory=FallbackPolicy)
    tool_correction: ToolCorrectionPolicy = field(default_factory=ToolCorrectionPolicy)
    validation_correction: ValidationCorrectionPolicy = field(
        default_factory=ValidationCorrectionPolicy,
    )


#: What applies when a runtime names no policy.
DEFAULT_EXECUTION_POLICY = AIExecutionPolicy()

#: One attempt, one turn, no fallback and no corrections, for a caller that
#: would rather fail than wait.
NO_RETRY = AIExecutionPolicy(
    budget=ExecutionBudget(max_turns=1),
    transport=TransportRetryPolicy(attempts=1),
)
