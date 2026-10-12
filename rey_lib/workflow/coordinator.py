"""Shared workflow coordinator — stacks an app's internal function calls.

A workflow is a named, ordered series of internal function calls owned by one
app (SGC_rey_workflow_internal_function_call_model). This coordinator is the
shared mechanic every Rey app uses: it executes a canonical ``Workflow``
(rey_lib.workflow.model) -- the steps the workflow selects, in ordinal order,
each with the configuration its process gives it -- and dispatches each step to
the app-provided handler registry. Selection, defaults, overrides and tokens
are the workflow objects'; execution, logging and evidence are this module's.

It owns no app domain logic. It calls only handlers the app registered by
process name (never arbitrary Python from YAML), dispatches by ``process`` (never
from human ``label``), and performs no cross-app orchestration — that is
pipeline territory.

Public API
----------
StepOutcome     One step's recorded outcome (id, label, process, status, ...).
WorkflowRun     Ordered step outcomes plus overall status.
run_workflow    Stack a workflow's step function calls per its YAML config.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from rey_lib.config.config_utils import record_config_file_references
from rey_lib.config.inventory import to_plain_data
from rey_lib.errors.error_utils import AppError, build_safe_error_payload
from rey_lib.logs import (
    bind_step,
    bind_run,
    clear_step,
    clear_run,
    AppLogger,
    finalize_run_log,
    log_run_complete,
    log_run_start,
    log_step_failure,
    log_step_end,
    log_step_start,
    monotonic_ms,
)
from rey_lib.workflow.engine import RunContext, WorkflowError
from rey_lib.workflow.hydration import _get
from rey_lib.workflow.model import Workflow

__all__ = ["StepOutcome", "WorkflowRun", "run_workflow", "ProcessHandler"]

_logger = AppLogger(__name__)

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


@dataclass
class WorkflowRun:
    """The result of coordinating a workflow's steps."""

    name: str
    status: str                       # "success" | "failed"
    outcomes: list[StepOutcome] = field(default_factory=list)
    context: Optional[RunContext] = None  # final run context (metadata + data)


def _finalize_run(ctx: Any, run_log: Any) -> None:
    """Finalize the run log unless a parent pipeline owns finalization.

    A pipeline step's ctx carries pipeline run identity in ``ctx.runtime`` (stamped
    by pipeline_coordinator); when present, the pipeline owns the single run-level
    finalization, so this embedded workflow must not finalize the shared pipeline
    run. A standalone workflow run has no pipeline identity and finalizes as before.
    """
    if _get(getattr(ctx, "runtime", None), "pipeline_run_id"):
        return
    # Finalization reads the JSONL run log back; with no JSONL file
    # (logging.run_store writes none) there is nothing to finalize from.
    if run_log.path() is None:
        return
    finalize_run_log(run_log, ai=getattr(ctx, "shared_ai", None))


def _refused_disabled_workflow(
    ctx: Any, run_log,
    name: str,
    *,
    apply: bool,
    metadata: Optional[dict[str, Any]],
) -> "WorkflowRun":
    """Record a disabled workflow as a refused run and return it.

    The attempt is still evidence worth keeping — someone asked for this
    workflow and it did not run — so it goes through the normal run result
    path: a RUN_START, a terminal RUN_COMPLETE, and a finalized run. No step
    is resolved and no handler is consulted.
    """
    message = (
        f"workflow '{name}' is disabled and was not executed. Set "
        f"'enabled: true' on the workflow definition to run it."
    )
    run = WorkflowRun(
        name=name,
        status="refused",
        context=RunContext(
            apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx}
        ),
    )
    try:
        ctx.workflow_name = name
    except (AttributeError, TypeError):
        pass
    run_log.set_nest_level("workflow")
    log_run_start(run_log, workflow=name, apply=apply)
    bind_run(run_log)
    _logger.warning("%s", message)
    log_run_complete(run_log, "refused", message=message)
    _finalize_run(ctx, run_log)
    clear_run()
    return run


def _pre_execution_failure(
    ctx: Any, run_log,
    run: "WorkflowRun",
    *,
    step_id: str,
    label: str,
    process: str,
    sequence: int,
    message: str,
) -> "WorkflowError":
    """Record a resolution failure as normal run evidence, then hand it back.

    A workflow that fails between RUN_START and its first step would otherwise
    leave a run log with no outcome at all. The failure is written through the
    same records an in-step failure uses, the run is finalized and unbound, and
    the caller raises the returned error so the existing contract is unchanged.
    """
    failure_id = log_step_failure(run_log,
        failed_step_id=step_id,
        failed_step_name=label or step_id,
        message=message,
        error_type="WorkflowError",
        error_message=message,
        sanitized_exception=message,
        failed_step_sequence=sequence,
        process=process,
    )
    run.outcomes.append(
        StepOutcome(step_id, label, process, "failed", error=message)
    )
    run.status = "failed"
    # Propagated -- the caller raises the returned WorkflowError -- so not
    # reported here: the AppError boundary reports it once (rule 79).
    log_run_complete(run_log,
        "failed",
        message=message,
        failure_record_id=failure_id,
        failed_step_id=step_id,
        failed_step_name=label or step_id,
        failure_message=message,
    )
    _finalize_run(ctx, run_log)
    clear_run()
    return WorkflowError(message)


def run_workflow(
    ctx: Any, run_log,
    workflow: Workflow,
    registry: Mapping[str, ProcessHandler],
    *,
    application: str,
    apply: bool = True,
    only: Optional[str] = None,
    step: Optional[str] = None,
    from_step: Optional[str] = None,
    to_step: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> WorkflowRun:
    """Stack the workflow's step function calls per its YAML config.

    Optionally run a single step or an ordered inclusive range instead of the
    whole workflow. Selection resolves against the workflow's ordered step list
    and is deterministic and fail-closed (SGC_Rey_Lib_Shared_Workflow_Step_Execution).

    Parameters
    ----------
    ctx : Any
        Resolved application context (the handlers' domain input).
    workflow : Workflow
        The canonical workflow. Nothing else is accepted: a resolved
        configuration entry is hydrated with ``hydrate_workflow`` first.
    registry : Mapping[str, ProcessHandler]
        App-owned ``operation name -> handler``. YAML may only call these names.
    application : str
        The application whose registry this is. This coordinator executes only
        that application's processes; a process belonging to another is
        refused. The limitation is the coordinator's, not the workflow's.
    apply : bool
        False for dry-run; a step whose effective config sets ``apply_only``
        true is skipped. Dry-run is expressed by ``apply=False`` — this SGC adds
        no new dry-run semantics and applies the existing rules to the selected
        step set only.
    only : str | None
        Legacy single-step selector (by ``id``). Treated as ``step`` when set.
    step : str | None
        Run exactly the one step matching this identifier. Cannot be combined
        with ``from_step``/``to_step``.
    from_step : str | None
        Run from the matching step through the end of the workflow.
    to_step : str | None
        Run from the start of the workflow through the matching step. Combined
        with ``from_step`` this is the inclusive ordered range.
    metadata : dict | None
        Shared run metadata seeded into the run context.

    Step identifiers resolve against each step's ``id``, ``label``, or
    ``process``. An identifier matching no step, or more than one step, fails
    closed with a clear error. Only the selected/executed steps appear in the
    returned outcomes.

    Returns
    -------
    WorkflowRun
        Ordered step outcomes and overall status ("failed" stops the run).

    Raises
    ------
    WorkflowError
        On anything but a canonical Workflow, an unregistered
        handler, a process of another application, or an invalid/unresolvable
        step selection — always fail closed, never guess. A malformed definition
        is refused earlier, when it is hydrated (retired keys included).
    """
    if not isinstance(workflow, Workflow):
        raise WorkflowError(
            "run_workflow executes a canonical Workflow; hydrate the configuration "
            f"entry with hydrate_workflow (got {type(workflow).__name__})."
        )
    name = workflow.name
    # Being switched off is a governed outcome, not a fault: it is reported as
    # a run with a terminal status like any other result, so the caller exits
    # non-zero without a traceback. Absent means enabled; only an explicit
    # false refuses, so a definition that omits the key is unaffected. A
    # disabled workflow is hydrated as identity only, so it is never rejected
    # for some unrelated defect in steps it will not run.
    if not workflow.enabled:
        return _refused_disabled_workflow(ctx, run_log, name, apply=apply, metadata=metadata)
    tokens = workflow.resolved_tokens()
    # Read once, and refused here rather than at the first step: a context
    # without applications is a boundary failure, not a workflow defect.
    applications = _require_applications(ctx, name)

    # ``only`` is the legacy single-step-by-id parameter; treat it as ``step``.
    if only is not None and step is None:
        step = only
    selected = workflow.select(step=step, from_step=from_step, to_step=to_step)

    run_ctx = RunContext(apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx})
    run = WorkflowRun(name=name, status="success", context=run_ctx)

    # Append-only run logging (SGC_Rey_Workflow_Pipeline_Automatic_Control_Batch_Logging):
    # emit a RUN_START, per-step STEP_START/STEP_END, and a RUN_COMPLETE + deterministic
    # RUN_SUMMARY through the log_utils authority. Record emission is fail-safe and
    # never alters workflow behavior. run_id is established by the logging layer; all
    # records share it and RUN_START carries the workflow name.
    try:
        ctx.workflow_name = name
    except (AttributeError, TypeError):
        # Some tests intentionally pass bare object() as ctx; proceed without
        # mutating ctx when it cannot accept dynamic attributes.
        pass
    # Workflow semantic base -> level 4: a workflow executes inside an app, so it
    # sits below the app base (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
    run_log.set_nest_level("workflow")
    log_run_start(run_log, workflow=name, apply=apply)
    # Bind this run log so file_utils records file operations through it
    # (SGC_Rey_File_Utils_Ambient_Run_Log_File_Recording); cleared at every exit.
    bind_run(run_log)
    # Record the config files that fed the effective ctx from provenance
    # (SGC_Rey_Config_Utils_Run_Log_Config_File_Recording).
    record_config_file_references(ctx, run_log)

    # The steps begin here, so resolve ends here -- one timestamp closing
    # bootstrap-and-resolve and opening the work itself.
    run_log.enter_phase("steps")

    sequence = 0
    # The first failure, captured once. A failure stops ordinary steps from
    # running; it does not stop the walk, because a step may declare
    # always_run. What is kept is the *first* failure's identity: it is the
    # reason normal execution stopped, and a later always_run failure records
    # itself without replacing it.
    first_failure: dict[str, Any] | None = None
    for workflow_process, workflow_step in workflow.ordered_steps():
        if workflow_step.ordinal not in selected:
            continue
        step_id, label, process = (
            workflow_step.key, workflow_step.label, workflow_process.name
        )
        owner = workflow_process.application
        implementation = workflow_process.operation
        if not implementation:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': process '{process}' names no "
                    "'implementation'. A process is a declaration binding a "
                    "name to a published operation."
                ),
            )

        operation = _published_operation(applications, owner, implementation)
        if operation is None:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': process '{process}' names "
                    f"implementation '{implementation}', which application "
                    f"'{owner}' does not publish."
                ),
            )

        # The limitation is this coordinator's: it was handed one
        # application's registry and dispatches in-process, so it executes only
        # that application's processes. The workflow itself may hold processes
        # of several.
        if owner != application:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': process '{process}' belongs to "
                    f"'{owner}'; this coordinator was invoked for "
                    f"'{application}' and executes only that application's "
                    "processes."
                ),
            )

        handler = registry.get(implementation)
        if handler is None:
            # Published but not approved for invocation. The catalog is the
            # application's own answer and the publication is what an author
            # reads; a gap between them is the application's to close, never
            # something to resolve by importing a name.
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': implementation '{implementation}' is "
                    f"published by '{owner}' but is not in its approved "
                    "implementation catalog."
                ),
            )

        # The engine's own vocabulary is read from the process defaults with
        # the step's overrides applied, and is not passed down: apply_only says
        # how the engine treats a step, not configuration the application asked
        # for.
        apply_only = workflow_process.apply_only(workflow_step)
        # Eligible after an earlier step failed, and nothing more: the step keeps
        # its declared position, and every other reason it might not run still
        # applies.
        always_run = workflow_process.always_run(workflow_step)

        # A prior failure makes ordinary steps ineligible. always_run steps stay
        # eligible and keep their declared position -- nothing is reordered and
        # nothing is deferred to a final group.
        if first_failure is not None and not always_run:
            _logger.info(
                "workflow '%s' step '%s' skipped: an earlier step failed.",
                name, step_id,
            )
            run.outcomes.append(
                StepOutcome(step_id, label, process, "skipped", "earlier step failed")
            )
            continue
        effective = workflow_process.effective(workflow_step, tokens)

        # Validation precedes dispatch, so an implementation never receives
        # configuration its published contract did not declare.
        refusal = _contract_refusal(operation, effective, name, process)
        if refusal:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=refusal,
            )

        sequence += 1
        step_name = label or step_id
        bind_step(
            step_id=step_id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=name,
        )
        # One mark, read again at whichever STEP_END this step reaches.
        # Monotonic, because a duration must not move when the wall clock does.
        #
        # It fills duration_ms on STEP_END rather than introducing a field of
        # its own: the record already accepts it and the results summary
        # already projects it, so the only thing missing was a producer.
        step_started = time.monotonic()
        try:
            # Workflow-step semantic boundary -> level 5, one per sequential in-process
            # step dispatch (SGC_Rey_Log_Nest_Level_Hierarchy_Correction). The descent
            # from workflow (4) parents each step under the workflow record.
            run_log.set_nest_level("workflow_step")
            log_step_start(run_log, step_name, sequence, step_type=process, step_id=step_id
            )
            # STEP_START anchors the workflow-step scope. Handler, lifecycle, file,
            # and result evidence belongs one level beneath that durable record.
            run_log.enter()

            if not apply and apply_only:
                log_step_end(run_log, step_name, "skipped", message="dry-run",
                             duration_ms=monotonic_ms(step_started))
                run.outcomes.append(
                    StepOutcome(step_id, label, process, "skipped", "dry-run")
                )
                _logger.info("workflow '%s' step '%s' skipped (dry-run).", name, step_id)
                continue

            try:
                result = handler(ctx, run_log, effective, run_ctx)
            except Exception as exc:  # noqa: BLE001 — fail closed: record and stop
                error_payload = build_safe_error_payload(
                    exc,
                    message=f"workflow '{name}' step '{step_id}' failed",
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    failed_step_sequence=sequence,
                )
                failure_message = str(error_payload["message"])
                # Handled -- recorded, and the workflow returns a failed run
                # rather than raising -- so reported here (rule 79).
                error = exc if isinstance(exc, AppError) else WorkflowError(
                    f"workflow '{name}' step '{step_id}' failed")
                if error is not exc:
                    error.__cause__ = exc
                error.report(f"workflow '{name}' step '{step_id}'", source=__name__)
                failure_id = log_step_failure(run_log, **error_payload)
                log_step_end(run_log, step_name, "failed", message=failure_message,
                             duration_ms=monotonic_ms(step_started))
                run.outcomes.append(
                    StepOutcome(step_id, label, process, "failed", error=failure_message)
                )
                run.status = "failed"
                # Recorded once. A later always_run step that also fails writes
                # its own ERROR, STEP_FAILURE and STEP_END at its own step; what
                # it must not do is replace the reason normal execution stopped.
                if first_failure is None:
                    first_failure = {
                        "message": failure_message,
                        "failure_record_id": failure_id,
                        "failed_step_id": step_id,
                        "failed_step_name": step_name,
                        "failure_message": failure_message,
                    }
                continue

            status = str(getattr(result, "status", "ok")) if result is not None else "ok"
            detail = str(getattr(result, "detail", "")) if result is not None else ""
            artifacts = list(getattr(result, "artifacts", []) or []) if result is not None else []
            log_step_end(run_log, step_name, status, message=detail,
                         duration_ms=monotonic_ms(step_started))
            run.outcomes.append(StepOutcome(step_id, label, process, status, detail, artifacts))
            if status == "failed":
                run.status = "failed"
                failure_message = detail or f"workflow step '{step_id}' failed."
                failure_id = log_step_failure(run_log,
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    message=failure_message,
                    error_type="WorkflowStepFailed",
                    error_message=failure_message,
                    sanitized_exception=failure_message,
                    failed_step_sequence=sequence,
                )
                if first_failure is None:
                    first_failure = {
                        "message": failure_message,
                        "failure_record_id": failure_id,
                        "failed_step_id": step_id,
                        "failed_step_name": step_name,
                        "failure_message": failure_message,
                    }
        finally:
            clear_step()

    # One completion, after the whole declared order has been walked. A failed
    # run is not complete while always_run steps are still executing, so
    # RUN_COMPLETE and finalization wait for them -- which is also what puts
    # their records inside the RESULTS_SUMMARY and LLM_PACKAGE that follow.
    run_log.set_nest_level("workflow")
    if first_failure is None:
        log_run_complete(run_log, "success")
    else:
        # The first failure, not the last. A later always_run failure has
        # already recorded itself; the run still reports why normal execution
        # stopped.
        log_run_complete(run_log, "failed", **first_failure)
    # _finalize_run enters summarize / package / interpret from inside; what is left when it
    # returns is this function's own teardown. Nothing closes the timeline here
    # -- app_runtime's finally owns that, for every exit.
    _finalize_run(ctx, run_log)
    run_log.enter_phase("close")
    clear_run()
    return run


# ---------------------------------------------------------------------------
# Published contract
# ---------------------------------------------------------------------------

def _require_applications(ctx: Any, workflow: str) -> tuple[Any, ...]:
    """The canonical applications, or a refusal naming the invariant.

    **Any execution path that runs a workflow must have canonical
    ``ctx.applications`` available.** Workflow capability is resolved from the
    owning ``Application``; there is no fallback registry and nothing is
    recovered by inspecting Python.

    Absent is a boundary failure and says so here. Left to fall through it would
    surface as an application that publishes nothing, which reads as a
    configuration mistake in the workflow rather than a context that was never
    built by the configuration load.
    """
    applications = getattr(ctx, "applications", None)
    if applications is None:
        raise WorkflowError(
            f"workflow '{workflow}': ctx.applications is missing. A workflow "
            "resolves its published operations from the canonical "
            "applications, so a context without them was not built by the "
            "configuration load."
        )
    return tuple(applications)


def _published_operation(applications: tuple[Any, ...], owner: str, implementation: str) -> Any:
    """The operation an application publishes under that key, or None.

    Read from the canonical applications -- the objects the configuration load
    built from what each installed application published. The engine has no
    catalogue of its own and asks no application directly.
    """
    if not owner:
        return None
    for application in applications:
        if getattr(application, "name", None) != owner:
            continue
        for operation in getattr(application, "workflow_operations", ()) or ():
            if getattr(operation, "name", None) == implementation:
                return operation
        return None
    return None


def _contract_refusal(
    operation: Any,
    effective: Mapping[str, Any],
    workflow: str,
    process: str,
) -> str:
    """Why this configuration does not satisfy the published contract, or ''.

    Two rules, both fail-closed. Configuration the contract does not declare is
    refused rather than passed through, because an implementation that silently
    receives an unknown key is how a typo becomes a setting nobody applied. A
    declared parameter marked required must be present.

    What a value *means* is not checked here. Publication says a parameter
    exists, is required and which values are valid; the implementation still
    owns what a name resolves to.
    """
    declared = {
        str(getattr(parameter, "name", "")): parameter
        for parameter in getattr(operation, "parameters", ()) or ()
    }
    # Through the one namespace-to-plain-data conversion. A resolved
    # configuration nests Namespace objects rather than dicts, and a Namespace
    # is not a Mapping, so walking the structure directly would read every
    # nested block as a single leaf -- target instead of target.connection.
    supplied = set(_flattened(to_plain_data(effective)))

    # A declared name covers its own subtree. An application that publishes a
    # block says it takes that block; requiring every leaf would make the
    # contract a copy of one installation's current configuration, which is the
    # drift publication exists to remove.
    undeclared = sorted(
        name for name in supplied
        if not any(
            name == one or name.startswith(f"{one}.") for one in declared
        )
    )
    if undeclared:
        return (
            f"workflow '{workflow}': process '{process}' supplies "
            f"{', '.join(repr(one) for one in undeclared)}, which "
            f"implementation '{getattr(operation, 'name', '')}' does not "
            "declare."
        )

    missing = sorted(
        name for name, parameter in declared.items()
        if getattr(parameter, "required", False)
        and not any(
            one == name or one.startswith(f"{name}.") for one in supplied
        )
    )
    if missing:
        return (
            f"workflow '{workflow}': process '{process}' is missing required "
            f"{', '.join(repr(one) for one in missing)} for implementation "
            f"'{getattr(operation, 'name', '')}'."
        )
    return ""


def _flattened(config: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Every configured key, nested ones written as ``parent.child``.

    A published parameter names one setting. ``target.connection`` is the name
    of a setting whose value happens to sit inside a mapping, so a contract can
    declare it without the declaration having to be flat -- and an application
    that publishes ``target`` instead has declared the whole block.
    """
    names: list[str] = []
    for key, value in config.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            names.extend(_flattened(value, prefix=f"{name}."))
        else:
            names.append(name)
    return names
