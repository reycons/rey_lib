"""WorkflowStep -- one ApplicationOperation instantiated in a workflow.

The leaf of the workflow object model (backlog 950). A step is bound once, at
construction, to an operation its application publishes and to the
implementation the application's approved catalog holds for it. It owns that
operation, its resolved settings (already composed for this operation instance),
its declarations (``apply_only``, ``always_run``), its review against the
operation's published contract, and its per-run state.

It does not run itself. Running its operation belongs to its owning
WorkflowProcess -- the instantiated Application -- through ``run_step``; whether
it runs (dry-run eligibility, failure progression, ``always_run``) belongs to the
Workflow. It does not compose configuration: that is hydration.

Built beside the recovered procedural engine (backlog 949); nothing here imports
it.

Public API
----------
WorkflowStep    A bound, configured operation instance.
StepOutcome     One step's recorded outcome.
RunContext      Shared, mutable context passed to every step handler.
WorkflowError   Raised for workflow-level problems.
ProcessHandler  An approved implementation's signature.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.operation_contract import contract_review
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger

if TYPE_CHECKING:
    from rey_lib.workflow.workflow_process import WorkflowProcess

__all__ = ["ProcessHandler", "RunContext", "StepOutcome", "WorkflowError", "WorkflowStep"]

_logger = AppLogger(__name__)


class WorkflowError(AppError):
    """Raised for workflow-level problems (e.g. an unknown step name)."""


@dataclass
class RunContext:
    """Shared, mutable context passed to every step handler.

    Attributes
    ----------
    apply : bool
        False for dry-run (apply_only steps are skipped); True to run them.
    data : dict
        Cross-step values (handlers read/write, e.g. commit hashes, paths).
    metadata : dict
        Recorded run metadata (the engine records status + step results here).
    """

    apply:    bool            = False
    data:     dict[str, Any]  = field(default_factory=dict)
    metadata: dict[str, Any]  = field(default_factory=dict)

# A process handler: (ctx, run_log, effective_config, run_context) -> result-or-None.
# The result may be any object exposing ``status``/``detail``; None means "ok".
#
# The run log is passed rather than reached through ctx or the run context: a
# handler that writes records is a run-log writer, and a writer takes its owner.
ProcessHandler = Callable[[Any, Any, dict[str, Any], RunContext], Any]


@dataclass
class StepOutcome:
    """One workflow step's recorded outcome."""

    id: str
    label: str
    process: str
    status: str                       # "ok" | "skipped" | "failed"
    detail: str = ""
    artifacts: list[Any] = field(default_factory=list)
    error: Optional[str] = None


class WorkflowStep:
    """One ApplicationOperation instantiated in a workflow, with its own settings.

    Attributes
    ----------
    id : str
        The step's authored identity.
    process_name : str
        The authored process the step was declared under.
    label : str
        The step's human label; the step name in run records.
    ordinal : int
        The step's position in its workflow's execution order.
    operation : ApplicationOperation
        The published operation this step instantiates.
    implementation : ProcessHandler
        The approved implementation bound to that operation.
    settings : dict
        This instance's configured settings, already composed.
    apply_only : bool
        Held for the Workflow: the step is not run in a dry run.
    always_run : bool
        Held for the Workflow: the step stays eligible after an earlier failure.
    process : WorkflowProcess | None
        The WorkflowProcess that owns this step; set by that process when it is
        constructed, None before.
    outcome : StepOutcome | None
        This run's outcome; None until the step is settled in a run.
    failure_record_id : Any
        This run's STEP_FAILURE record id when it failed in execution; else None.
    """

    def __init__(
        self,
        *,
        id: str,
        process_name: str,
        label: str,
        ordinal: int,
        application: Application,
        operation_name: str,
        approved: Mapping[str, ProcessHandler],
        settings: Mapping[str, Any],
        apply_only: bool = False,
        always_run: bool = False,
    ) -> None:
        """Bind the step to its published operation and approved implementation.

        Parameters
        ----------
        id, process_name, label, ordinal
            The step's authored identity and position.
        application : Application
            The application the step's process instantiates.
        operation_name : str
            The name of the operation, as the application publishes it.
        approved : Mapping[str, ProcessHandler]
            The application's approved implementation catalog.
        settings : Mapping[str, Any]
            The step's composed settings for this operation instance.
        apply_only, always_run : bool
            Engine flags, held for the Workflow.

        Raises
        ------
        WorkflowError
            When the application does not publish the operation, or publishes
            it without approving an implementation for it.
        """
        if not id:
            raise WorkflowError("a workflow step is missing required 'id'.")
        self.id = id
        self.process_name = process_name
        self.label = label or id
        self.ordinal = ordinal
        self.operation = _published_operation(application, operation_name, id)
        self.implementation = _approved_implementation(
            application, approved, operation_name, id
        )
        # Deep-copied: a handler mutating a nested setting must not reach the
        # mapping hydration passed in, or any other holder of it.
        self.settings: dict[str, Any] = deepcopy(dict(settings))
        self.apply_only = apply_only
        self.always_run = always_run
        self.process: Optional["WorkflowProcess"] = None
        self.outcome: Optional[StepOutcome] = None
        self.failure_record_id: Any = None

    def reset(self) -> None:
        """Clear the state of an earlier invocation; called at the start of every run."""
        self.outcome = None
        self.failure_record_id = None

    def settle(self, outcome: StepOutcome, *, failure_record_id: Any = None) -> StepOutcome:
        """Hold this run's outcome, and its STEP_FAILURE record id if any; return it.

        Every outcome is settled here: the one its process records when running
        the operation, and one the Workflow decides without running it (dry run,
        earlier failure, refused before execution), so no state is left over
        from an earlier run.
        """
        self.outcome = outcome
        self.failure_record_id = failure_record_id
        return outcome

    def contract_refusal(self, workflow_name: str) -> str:
        """Why this step's settings do not satisfy its operation's contract, or ''.

        The settings are reviewed whole against the operation's
        ``contract_definition`` (JSON Schema, backlog 939). Unsupported keys are
        tolerated with a warning naming them; a violation refuses. The owning
        process calls this before running the operation; the Workflow calls it
        on the dry-run path, where the operation is not run, so each step is
        reviewed once per run.
        """
        unsupported, violations = contract_review(
            self.operation.contract_definition or {},
            to_plain_data(self.settings),
        )
        if unsupported:
            _logger.warning(
                "workflow '%s': process '%s' carries settings the contract of "
                "implementation '%s' does not support; they are ignored: %s",
                workflow_name, self.process_name, self.operation.name,
                "; ".join(unsupported),
            )
        if not violations:
            return ""
        return (
            f"workflow '{workflow_name}': process '{self.process_name}' settings do not "
            "satisfy the "
            f"contract of implementation '{self.operation.name}': "
            + "; ".join(violations)
        )


def _published_operation(
    application: Application, operation_name: str, step_id: str
) -> ApplicationOperation:
    """The operation the application publishes under that name, or a refusal.

    Read from the application's own published operations; an operation another
    application publishes is not found here, so it is refused the same way.
    """
    for operation in application.workflow_operations:
        if operation.name == operation_name:
            return operation
    raise WorkflowError(
        f"step '{step_id}' names operation '{operation_name}', which application "
        f"'{application.name}' does not publish."
    )


def _approved_implementation(
    application: Application,
    approved: Mapping[str, ProcessHandler],
    operation_name: str,
    step_id: str,
) -> ProcessHandler:
    """The implementation the application approves for the operation, or a refusal.

    Published but not approved is a gap in the application's own answer, never
    something to resolve by importing a name.
    """
    implementation = approved.get(operation_name)
    if implementation is None:
        raise WorkflowError(
            f"step '{step_id}': operation '{operation_name}' is published by "
            f"'{application.name}' but is not in its approved implementation catalog."
        )
    return implementation
