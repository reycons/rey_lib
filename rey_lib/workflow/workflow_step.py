"""WorkflowStep -- one ApplicationOperation instantiated in a workflow.

The leaf of the workflow object model (backlog 950, slice 952). A step is bound
once, at construction, to an operation its application publishes and to the
implementation the application's approved catalog holds for it. It owns its
configured settings, already composed for this operation instance, and its own
execution: validating those settings against the operation's published
contract, recording STEP_START and STEP_END, invoking the implementation, and
reporting a failure through the existing error estate.

It does not compose configuration -- that is construction -- and it does not
decide whether it runs: dry-run eligibility, failure progression and
``always_run`` eligibility belong to the Workflow that holds it. ``apply_only``
and ``always_run`` are held here for the Workflow to read.

Built beside the recovered procedural engine (backlog 949), whose
``run_workflow`` dispatch is the behavioural reference; nothing here calls it.

Public API
----------
WorkflowStep    A bound, configured, executable operation instance.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.operation_contract import contract_review
from rey_lib.errors.error_utils import AppError, build_safe_error_payload
from rey_lib.logs import (
    AppLogger,
    bind_step,
    clear_step,
    log_step_end,
    log_step_failure,
    log_step_start,
    monotonic_ms,
)
from rey_lib.logs.run_log import RunLog

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
    outcome : StepOutcome | None
        The recorded outcome of the last execution; None before one.
    failure_record_id : Any
        The STEP_FAILURE record id of the last failed execution; None otherwise.
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
        self.outcome: Optional[StepOutcome] = None
        self.failure_record_id: Any = None

    def reset(self) -> None:
        """Clear the state of an earlier invocation; called at the start of every run."""
        self.outcome = None
        self.failure_record_id = None

    def settle(self, outcome: StepOutcome) -> StepOutcome:
        """Hold an outcome the Workflow decided without executing the step; return it.

        A step skipped (dry run, earlier failure) or refused before execution is
        settled here, so its state is never left over from an earlier run.
        """
        self.outcome = outcome
        self.failure_record_id = None
        return outcome

    def execute(
        self,
        ctx: Any,
        run_log: RunLog,
        run_ctx: RunContext,
        *,
        sequence: int,
        workflow_name: str,
    ) -> StepOutcome:
        """Validate, record, invoke and settle this step; return its outcome.

        The canonical execution boundary of an application operation: any
        exception the implementation raises is caught here, reported through
        AppError, recorded as STEP_FAILURE and STEP_END failed, and held as the
        step's failed outcome. It does not escape the step.

        Parameters
        ----------
        ctx : Any
            Resolved application context, the implementation's domain input.
        run_log : RunLog
            The run log this step writes its records through.
        run_ctx : RunContext
            The workflow's shared run context.
        sequence : int
            The step's sequence number in this run, assigned by the Workflow.
        workflow_name : str
            The name of the workflow running the step, for records and messages.

        Returns
        -------
        StepOutcome
            The recorded outcome, also held on :attr:`outcome`.

        Raises
        ------
        WorkflowError
            When the settings do not satisfy the operation's published contract.
            Raised before any step record is written, so the implementation never
            receives settings its contract refuses.
        """
        self.outcome = None
        self.failure_record_id = None
        refusal = self.contract_refusal(workflow_name)
        if refusal:
            raise WorkflowError(refusal)

        step_name = self.label or self.id
        bind_step(
            step_id=self.id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=workflow_name,
        )
        # One monotonic mark, read again at whichever STEP_END the step reaches.
        step_started = time.monotonic()
        try:
            # Workflow-step semantic boundary -> level 5, one per in-process step
            # dispatch (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
            run_log.set_nest_level("workflow_step")
            log_step_start(
                run_log, step_name, sequence, step_type=self.process_name, step_id=self.id
            )
            # Handler, lifecycle, file and result evidence sits beneath STEP_START.
            run_log.enter()
            try:
                try:
                    result = self.implementation(ctx, run_log, self.settings, run_ctx)
                except Exception as exc:  # noqa: BLE001 — canonical execution boundary
                    error_payload = build_safe_error_payload(
                        exc,
                        message=f"workflow '{workflow_name}' step '{self.id}' failed",
                        failed_step_id=self.id,
                        failed_step_name=step_name,
                        failed_step_sequence=sequence,
                    )
                    failure_message = str(error_payload["message"])
                    # Handled -- recorded, and settled as a failed outcome rather
                    # than raised -- so reported here (rule 79).
                    error = exc if isinstance(exc, AppError) else WorkflowError(
                        f"workflow '{workflow_name}' step '{self.id}' failed")
                    if error is not exc:
                        error.__cause__ = exc
                    error.report(
                        f"workflow '{workflow_name}' step '{self.id}'", source=__name__
                    )
                    self.failure_record_id = log_step_failure(run_log, **error_payload)
                    log_step_end(run_log, step_name, "failed", message=failure_message,
                                 duration_ms=monotonic_ms(step_started))
                    self.outcome = StepOutcome(
                        self.id, self.label, self.process_name, "failed",
                        error=failure_message,
                    )
                    return self.outcome

                status = (
                    str(getattr(result, "status", "ok")) if result is not None else "ok"
                )
                detail = str(getattr(result, "detail", "")) if result is not None else ""
                artifacts = (
                    list(getattr(result, "artifacts", []) or [])
                    if result is not None else []
                )
                log_step_end(run_log, step_name, status, message=detail,
                             duration_ms=monotonic_ms(step_started))
                self.outcome = StepOutcome(
                    self.id, self.label, self.process_name, status, detail, artifacts
                )
                if status == "failed":
                    failure_message = detail or f"workflow step '{self.id}' failed."
                    self.failure_record_id = log_step_failure(run_log,
                        failed_step_id=self.id,
                        failed_step_name=step_name,
                        message=failure_message,
                        error_type="WorkflowStepFailed",
                        error_message=failure_message,
                        sanitized_exception=failure_message,
                        failed_step_sequence=sequence,
                    )
                return self.outcome
            finally:
                # The depth entered beneath STEP_START is left again.
                run_log.exit()
        finally:
            clear_step()

    def contract_refusal(self, workflow_name: str) -> str:
        """Why this step's settings do not satisfy its operation's contract, or ''.

        The settings are reviewed whole against the operation's
        ``contract_definition`` (JSON Schema, backlog 939). Unsupported keys are
        tolerated with a warning naming them; a violation refuses. ``execute``
        calls this first; the Workflow calls it on the dry-run path, where it does
        not execute the step, so each step is reviewed once per run.
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
