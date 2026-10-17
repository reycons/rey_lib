"""One unit of work for AI: its governed facts, its input, and its own outcome.

``AITask`` is the canonical unit of work (backlog 1021, ai.redesign). It carries
everything one execution needs -- which engine, which contract and instruction,
which settings, what is sent and what comes back -- so that AI receives a task
whose choices are already settled and performs it.

Two kinds of state, deliberately held by one object:

    the facts       governed configuration, input and caller context. Fixed at
                    construction: hydration (backlog 1022) decides them, and a
                    different choice -- another engine, another instruction --
                    is a different task, built again, never an edit to this one.
    the lifecycle   status, answer, error and run id. This task's own, changed
                    only by its transitions: started, answered, failed,
                    cancelled, and naming the run that is executing it.

Immutable facts do not mean an immutable lifecycle. Every field is read-only to
callers; the transitions below are the only code that writes the lifecycle, so
AI executes a task and invokes its transitions, and owns none of its state.

It reads nothing, resolves nothing and executes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.instructions import AIInstructionKind
from rey_lib.ai.requests import AIOutputSpec

__all__ = ["AITask"]


@dataclass(frozen=True, eq=False, kw_only=True)
class AITask:
    """One unit of work, as hydration settled it.

    Attributes:
        id: What addresses this unit of work. Minted here.
        task_key, installation, label: Which governed task this is, and where.
        composed_first: Whether this task is composed before it runs. An
            interaction fact of the task, carried and not interpreted here.
        profile_key: Which governed engine was chosen, for evidence.
        model_identifier: The engine as Pydantic AI resolves it,
            ``<provider>:<model>``. Required: a task reaches one engine.
        base_url: Where that engine is reached; empty is the provider default.
        credential_ref: A reference to the engine's credential, never the value.
        timeout_seconds: How long one call may take; ``None`` is the default.
        access_allowed, access_default: Which representations the engine may
            receive, as its governed row states them.
        instruction_key, instruction_kind: The instruction this task follows.
        contract_key, contract_version, contract_body: The contract that
            instruction binds, and its text.
        output_schema: The shape the contract requires, when it states one.
        temperature, representation, connection: The settings that apply,
            already resolved from the group default and the task's override.
        input: What is sent.
        output: The output this unit of work requires.
        subject: What is being asked about, already resolved by the caller.
        subject_id, subject_kind, origin_id: How the caller addresses this
            unit of work's subject and where it began. Opaque: carried and
            preserved, never interpreted.
        status, answer, error, run_id: The lifecycle. Read-only to callers and
            changed only by this task's transitions.
    """

    IDLE: ClassVar[str] = "idle"
    RUNNING: ClassVar[str] = "running"
    COMPLETED: ClassVar[str] = "completed"
    FAILED: ClassVar[str] = "failed"
    CANCELLED: ClassVar[str] = "cancelled"

    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    task_key: str = ""
    installation: str = ""
    label: str = ""
    composed_first: bool = False

    profile_key: str = ""
    model_identifier: str
    base_url: str = ""
    credential_ref: str = ""
    timeout_seconds: float | None = None
    access_allowed: tuple[str, ...] = ()
    access_default: str = ""

    instruction_key: str = ""
    instruction_kind: AIInstructionKind = AIInstructionKind.NONE
    contract_key: str = ""
    contract_version: str = ""
    contract_body: str = ""
    output_schema: dict[str, Any] | None = None

    temperature: float | None = None
    representation: str = ""
    connection: str = ""

    input: AIInput = field(default_factory=AIInput)
    output: AIOutputSpec = field(default_factory=AIOutputSpec)

    subject: str = ""
    subject_id: str = ""
    subject_kind: str = ""
    origin_id: str = ""

    status: str = field(default=IDLE, init=False)
    answer: Any = field(default=None, init=False)
    error: str = field(default="", init=False)
    run_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        """Refuse a unit of work that names no engine."""
        if not str(self.model_identifier or "").strip():
            raise AIConfigurationError(
                f"AI task '{self.task_key or '(default)'}' names no engine."
            )

    # -- the lifecycle -----------------------------------------------------

    def started(self, run_id: str = "") -> None:
        """Running, with the previous outcome cleared.

        The outcome goes with the run that produced it: keeping it would let a
        reader see the last answer beside a running state as though the new one
        had already arrived.

        Args:
            run_id: How the execution now under way is addressed.
        """
        self._become(status=self.RUNNING, answer=None, error="", run_id=str(run_id or ""))

    def record_run(self, run_id: str) -> None:
        """Name the run executing this task, and change nothing else.

        Not ``started``, which also clears the outcome: a run can finish before
        whoever launched it has recorded its name, and resetting here would
        erase an answer that had already arrived.
        """
        self._become(run_id=str(run_id or ""))

    def answered(self, answer: Any) -> None:
        """Completed, carrying what came back."""
        self._become(status=self.COMPLETED, answer=answer, error="")

    def failed(self, message: str) -> None:
        """Failed, carrying why."""
        self._become(status=self.FAILED, answer=None, error=str(message))

    def cancelled(self) -> None:
        """Cancelled. Not a failure, and not an answer."""
        self._become(status=self.CANCELLED, answer=None, error="")

    def _become(self, **lifecycle: Any) -> None:
        """Write the lifecycle -- the only place it is written.

        The dataclass is frozen so that no caller can assign any field; the
        lifecycle is this object's own, so its transitions write it here. A
        governed fact is never written, even from inside.
        """
        facts = set(lifecycle) - {"status", "answer", "error", "run_id"}
        if facts:
            raise AttributeError(f"AITask facts are fixed: {', '.join(sorted(facts))}")
        for name, value in lifecycle.items():
            object.__setattr__(self, name, value)
