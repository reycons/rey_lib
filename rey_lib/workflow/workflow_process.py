"""WorkflowProcess -- one Application instantiated in a workflow.

The middle of the workflow object model (backlog 950). A process is a
configured instance of an existing Application: it holds that Application, its
own application configuration as governed for this instance, and its ordered
WorkflowStep children -- each a configured instance of one of that
Application's operations. Two processes may instantiate the same Application
with different configuration.

**It is the Application execution boundary.** ``run_step`` runs one of its own
steps' operations as part of this configured Application instance: it reviews
the step's settings against the operation's contract, records STEP_START and
STEP_END, invokes the step's approved implementation, settles a raised or
returned failure through the existing error estate, and settles the step's
outcome. The Workflow decides which steps run and in which order, and calls
this; it never invokes an implementation itself.

It owns population state, not catalog discovery. The workflow hydrator
materialises the graph from the governed rows and the approved implementation
catalog its caller injects -- as ManifestSource takes its reader -- and each
WorkflowStep arrives already bound to its ApplicationOperation and approved
implementation.

Public API
----------
WorkflowProcess    A configured Application instance and its ordered steps.
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from rey_lib.config.applications import Application
from rey_lib.errors.error_utils import AppError, build_safe_error_payload
from rey_lib.logs import (
    bind_step,
    clear_step,
    log_step_end,
    log_step_failure,
    log_step_start,
    monotonic_ms,
)
from rey_lib.logs.run_log import RunLog
from rey_lib.workflow.workflow_step import RunContext, StepOutcome, WorkflowError, WorkflowStep

if TYPE_CHECKING:
    from rey_lib.workflow.workflow import Workflow

__all__ = ["WorkflowProcess"]


class WorkflowProcess:
    """One Application instantiated in a workflow, with its configuration and steps.

    Attributes
    ----------
    name : str
        The process's governed name.
    ordinal : int
        The process's position in its workflow.
    application : Application
        The existing Application this process instantiates.
    config : dict | None
        This instance's application configuration as governed; None where the
        governed value is null, kept distinct from an empty mapping.
    steps : tuple[WorkflowStep, ...]
        The process's steps, in the order hydration gives them.
    workflow : Workflow | None
        The Workflow this process belongs to; set by that Workflow when it is
        constructed, None before.
    """

    def __init__(
        self,
        *,
        name: str,
        ordinal: int,
        application: Application,
        config: Optional[Mapping[str, Any]],
        steps: Sequence[WorkflowStep],
    ) -> None:
        """Hold the Application instance, its configuration and its steps.

        Parameters
        ----------
        name, ordinal
            The process's governed identity and position.
        application : Application
            The Application this process instantiates.
        config : Mapping[str, Any] | None
            The process's application configuration as governed. Copied, so
            no other process or caller shares it.
        steps : Sequence[WorkflowStep]
            The process's steps, each already bound by hydration. Each is made
            this process's own.

        Raises
        ------
        WorkflowError
            When a step instantiates an operation this process's Application
            does not publish, or already belongs to another process.
        """
        if not name:
            raise WorkflowError("a workflow process is missing required 'name'.")
        self.name = name
        self.ordinal = ordinal
        self.application = application
        self.config: Optional[dict[str, Any]] = (
            None if config is None else deepcopy(dict(config))
        )
        # By identity: a step is bound to the operation object its Application
        # published, so an equal operation another Application publishes is not it.
        published = application.workflow_operations
        for step in steps:
            if not any(step.operation is one for one in published):
                raise WorkflowError(
                    f"process '{name}': step '{step.id}' instantiates operation "
                    f"'{step.operation.name}', which application "
                    f"'{application.name}' does not publish."
                )
            if step.process is not None:
                raise WorkflowError(
                    f"process '{name}': step '{step.id}' already belongs to process "
                    f"'{step.process.name}'."
                )
        self.steps: tuple[WorkflowStep, ...] = tuple(steps)
        for step in self.steps:
            step.process = self
        self.workflow: Optional["Workflow"] = None

    def run_step(
        self,
        step: WorkflowStep,
        ctx: Any,
        run_log: RunLog,
        run_ctx: RunContext,
        *,
        sequence: int,
        workflow_name: str,
    ) -> StepOutcome:
        """Run one of this process's steps' operations; settle and return its outcome.

        The canonical execution boundary of an application operation: any
        exception the implementation raises is caught here, reported through
        AppError, recorded as STEP_FAILURE and STEP_END failed, and settled as
        the step's failed outcome. It does not escape.

        Parameters
        ----------
        step : WorkflowStep
            The step to run; it must be one of this process's own.
        ctx : Any
            Resolved application context, the implementation's domain input.
        run_log : RunLog
            The run log the step's records are written through.
        run_ctx : RunContext
            The workflow's shared run context.
        sequence : int
            The step's sequence number in this run, assigned by the Workflow.
        workflow_name : str
            The name of the workflow running the step, for records and messages.

        Returns
        -------
        StepOutcome
            The recorded outcome, also settled on the step.

        Raises
        ------
        WorkflowError
            When the step is not this process's own, or its settings do not
            satisfy its operation's contract. Raised before any step record is
            written, so the implementation never receives refused settings.
        """
        if step.process is not self:
            raise WorkflowError(
                f"process '{self.name}': step '{step.id}' is not one of its steps."
            )
        step.reset()
        refusal = step.contract_refusal(workflow_name)
        if refusal:
            raise WorkflowError(refusal)

        step_name = step.label or step.id
        bind_step(
            step_id=step.id,
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
                run_log, step_name, sequence, step_type=step.process_name, step_id=step.id
            )
            # Handler, lifecycle, file and result evidence sits beneath STEP_START.
            run_log.enter()
            try:
                try:
                    result = step.implementation(ctx, run_log, step.settings, run_ctx)
                except Exception as exc:  # noqa: BLE001 — canonical execution boundary
                    error_payload = build_safe_error_payload(
                        exc,
                        message=f"workflow '{workflow_name}' step '{step.id}' failed",
                        failed_step_id=step.id,
                        failed_step_name=step_name,
                        failed_step_sequence=sequence,
                    )
                    failure_message = str(error_payload["message"])
                    # Handled -- recorded, and settled as a failed outcome rather
                    # than raised -- so reported here (rule 79).
                    error = exc if isinstance(exc, AppError) else WorkflowError(
                        f"workflow '{workflow_name}' step '{step.id}' failed")
                    if error is not exc:
                        error.__cause__ = exc
                    error.report(
                        f"workflow '{workflow_name}' step '{step.id}'", source=__name__
                    )
                    failure_id = log_step_failure(run_log, **error_payload)
                    log_step_end(run_log, step_name, "failed", message=failure_message,
                                 duration_ms=monotonic_ms(step_started))
                    return step.settle(
                        StepOutcome(step.id, step.label, step.process_name, "failed",
                                    error=failure_message),
                        failure_record_id=failure_id,
                    )

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
                outcome = StepOutcome(
                    step.id, step.label, step.process_name, status, detail, artifacts
                )
                failure_id = None
                if status == "failed":
                    failure_message = detail or f"workflow step '{step.id}' failed."
                    failure_id = log_step_failure(run_log,
                        failed_step_id=step.id,
                        failed_step_name=step_name,
                        message=failure_message,
                        error_type="WorkflowStepFailed",
                        error_message=failure_message,
                        sanitized_exception=failure_message,
                        failed_step_sequence=sequence,
                    )
                return step.settle(outcome, failure_record_id=failure_id)
            finally:
                # The depth entered beneath STEP_START is left again.
                run_log.exit()
        finally:
            clear_step()
