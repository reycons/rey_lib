"""Workflow -- the orchestrator of a fully hydrated workflow graph.

The root of the workflow object model (backlog 950, slice 956). A Workflow holds
its governed workflow-level state -- ``tokens`` and ``config`` -- and its
WorkflowProcess children, each holding its WorkflowStep children. The workflow
hydrator creates that whole graph in one pass from the governed database facts
(control.workflow_flat_vw) and the approved implementation catalog its caller
injects; hydration owns population, not implementation, as ManifestSource does.
By the time ``run`` is called every object is fully hydrated: nothing here
hydrates, merges, propagates or reconstructs configuration.

``run`` owns workflow execution: selection, ordering, dispatch, dry-run
boundaries, failure progression, ``always_run`` eligibility, first-failure
retention, run-level evidence, finalization and the aggregate WorkflowRun. Each
eligible step is run by its owning WorkflowProcess (WorkflowProcess.run_step),
the instantiated Application; the Workflow never invokes an implementation.

Built beside the recovered procedural engine (backlog 949), whose
``run_workflow`` is the behavioural reference; nothing here imports it.

Public API
----------
WorkflowRun    Ordered step outcomes plus overall status.
Workflow       The orchestrator.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from rey_lib.config.config_utils import record_config_file_references
from rey_lib.logs import (
    AppLogger,
    bind_run,
    bind_step,
    clear_run,
    clear_step,
    finalize_run_log,
    log_run_complete,
    log_run_start,
    log_step_end,
    log_step_failure,
    log_step_start,
    monotonic_ms,
)
from rey_lib.logs.run_log import RunLog
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import RunContext, StepOutcome, WorkflowError, WorkflowStep

__all__ = ["Workflow", "WorkflowRun"]

_logger = AppLogger(__name__)


@dataclass
class WorkflowRun:
    """The result of coordinating a workflow's steps."""

    name: str
    status: str                       # "success" | "failed"
    outcomes: list[StepOutcome] = field(default_factory=list)
    context: Optional[RunContext] = None  # final run context (metadata + data)


class Workflow:
    """A fully hydrated workflow: its governed state, processes and steps.

    Attributes
    ----------
    name : str
        The workflow's governed name.
    enabled : bool
        False refuses every run; it is reported as a refused run.
    tokens : dict | None
        The workflow's tokens as governed; None kept distinct from empty.
    config : dict | None
        The workflow's configuration as governed (control.workflow.config).
    processes : tuple[WorkflowProcess, ...]
        The workflow's processes, as hydration gives them.
    steps : tuple[WorkflowStep, ...]
        Every step of every process, in workflow-wide ordinal order.
    """

    def __init__(
        self,
        *,
        name: str,
        enabled: bool,
        tokens: Optional[Mapping[str, Any]],
        config: Optional[Mapping[str, Any]],
        processes: Sequence[WorkflowProcess],
    ) -> None:
        """Hold the workflow's governed state and take ownership of its processes.

        Parameters
        ----------
        name, enabled
            The workflow's governed identity and switch.
        tokens, config : Mapping[str, Any] | None
            The workflow's governed tokens and configuration. Copied.
        processes : Sequence[WorkflowProcess]
            The workflow's processes, each already hydrated with its steps.

        Raises
        ------
        WorkflowError
            When the name is missing, a process already belongs to another
            workflow, or two steps share a workflow-wide ordinal.
        """
        if not name:
            raise WorkflowError("a workflow is missing required 'name'.")
        self.name = name
        self.enabled = enabled
        self.tokens: Optional[dict[str, Any]] = (
            None if tokens is None else deepcopy(dict(tokens))
        )
        self.config: Optional[dict[str, Any]] = (
            None if config is None else deepcopy(dict(config))
        )
        for process in processes:
            if process.workflow is not None:
                raise WorkflowError(
                    f"workflow '{name}': process '{process.name}' already belongs to "
                    f"workflow '{process.workflow.name}'."
                )
        self.processes: tuple[WorkflowProcess, ...] = tuple(processes)
        steps = [step for process in self.processes for step in process.steps]
        seen: dict[int, str] = {}
        for step in steps:
            if step.ordinal in seen:
                raise WorkflowError(
                    f"workflow '{name}': steps '{seen[step.ordinal]}' and '{step.id}' "
                    f"share ordinal {step.ordinal}."
                )
            seen[step.ordinal] = step.id
        # The declared workflow-wide order: control.workflow_step.ordinal.
        self.steps: tuple[WorkflowStep, ...] = tuple(
            sorted(steps, key=lambda one: one.ordinal)
        )
        for process in self.processes:
            process.workflow = self

    def run(
        self,
        ctx: Any,
        run_log: RunLog,
        *,
        apply: bool = True,
        step: Optional[str] = None,
        from_step: Optional[str] = None,
        to_step: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> WorkflowRun:
        """Run the workflow's steps, or a single step or inclusive range of them.

        Parameters
        ----------
        ctx : Any
            Resolved application context, passed unchanged to every step.
        run_log : RunLog
            The run log the workflow and its steps write through.
        apply : bool
            False for dry-run: a step with ``apply_only`` is skipped.
        step : str | None
            Run exactly the one step matching this identifier.
        from_step, to_step : str | None
            Run the inclusive ordered range between them.
        metadata : dict | None
            Shared run metadata seeded into the run context.

        Identifiers resolve against each step's ``id``, ``label`` or
        ``process_name``; no match or more than one fails closed.

        Returns
        -------
        WorkflowRun
            Ordered outcomes of the selected steps and overall status.

        Raises
        ------
        WorkflowError
            On an invalid or unresolvable selection, or a step whose settings
            do not satisfy its contract (recorded as run evidence first).
        """
        name = self.name
        # Every invocation starts clean: no step reports an earlier run's state.
        for one in self.steps:
            one.reset()
        # Being switched off is a governed outcome, reported as a refused run.
        if self.enabled is False:
            return self._refused(ctx, run_log, apply=apply, metadata=metadata)

        selected = self._select(step=step, from_step=from_step, to_step=to_step)

        run_ctx = RunContext(apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx})
        run = WorkflowRun(name=name, status="success", context=run_ctx)

        try:
            ctx.workflow_name = name
        except (AttributeError, TypeError):
            # A bare object() ctx cannot accept dynamic attributes.
            pass
        # Workflow semantic base -> level 4 (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
        run_log.set_nest_level("workflow")
        log_run_start(run_log, workflow=name, apply=apply)
        # Bound so file_utils records file operations through it; cleared at every exit.
        bind_run(run_log)
        record_config_file_references(ctx, run_log)
        run_log.enter_phase("steps")

        sequence = 0
        # The first failure, captured once: the reason ordinary steps stopped.
        # A later always_run failure records itself without replacing it.
        first_failure: dict[str, Any] | None = None
        for index, one in enumerate(self.steps):
            if index not in selected:
                continue
            # A prior failure makes ordinary steps ineligible; always_run steps
            # keep their declared position.
            if first_failure is not None and not one.always_run:
                _logger.info(
                    "workflow '%s' step '%s' skipped: an earlier step failed.",
                    name, one.id,
                )
                run.outcomes.append(one.settle(StepOutcome(
                    one.id, one.label, one.process_name, "skipped", "earlier step failed",
                )))
                continue

            if not apply and one.apply_only:
                # Reviewed even though it is not executed, so a dry run refuses
                # what a real run would.
                refusal = one.contract_refusal(name)
                if refusal:
                    raise self._pre_execution_failure(
                        ctx, run_log, run, one, sequence=sequence + 1, message=refusal,
                    )
                sequence += 1
                self._record_dry_run_skip(run_log, run, one, sequence)
                continue

            try:
                # Through the step's own process: the instantiated Application
                # runs its operation. The Workflow never invokes an implementation.
                outcome = one.process.run_step(
                    one, ctx, run_log, run_ctx, sequence=sequence + 1, workflow_name=name,
                )
            except WorkflowError as refusal:
                # The step's settings do not satisfy its contract; nothing was
                # recorded for the step and its implementation was not called.
                raise self._pre_execution_failure(
                    ctx, run_log, run, one, sequence=sequence + 1, message=str(refusal),
                ) from refusal
            sequence += 1
            run.outcomes.append(outcome)
            if outcome.status == "failed":
                run.status = "failed"
                if first_failure is None:
                    failure_message = (
                        outcome.error or outcome.detail
                        or f"workflow step '{one.id}' failed."
                    )
                    first_failure = {
                        "message": failure_message,
                        "failure_record_id": one.failure_record_id,
                        "failed_step_id": one.id,
                        "failed_step_name": one.label,
                        "failure_message": failure_message,
                    }

        # One completion, after the whole declared order has been walked.
        run_log.set_nest_level("workflow")
        if first_failure is None:
            log_run_complete(run_log, "success")
        else:
            log_run_complete(run_log, "failed", **first_failure)
        _finalize(ctx, run_log)
        run_log.enter_phase("close")
        clear_run()
        return run

    # -- selection ------------------------------------------------------------

    def _select(
        self,
        *,
        step: Optional[str],
        from_step: Optional[str],
        to_step: Optional[str],
    ) -> set[int]:
        """Resolve the set of step indices to execute (fail closed).

        ``step`` selects one step; ``from_step`` runs to the end; ``to_step`` runs
        from the start; both is the inclusive ordered range. ``step`` cannot
        combine with a range, and a reversed range fails closed. With no
        selector, every step runs.
        """
        name = self.name
        total = len(self.steps)
        if step is not None and (from_step is not None or to_step is not None):
            raise WorkflowError(
                f"workflow '{name}': 'step' cannot be combined with 'from_step'/'to_step'."
            )
        if step is None and from_step is None and to_step is None:
            return set(range(total))

        if step is not None:
            return {self._resolve_index(step)}

        start = self._resolve_index(from_step) if from_step is not None else 0
        end = self._resolve_index(to_step) if to_step is not None else total - 1
        if start > end:
            raise WorkflowError(
                f"workflow '{name}': from_step '{from_step}' resolves after to_step "
                f"'{to_step}' (empty range)."
            )
        return set(range(start, end + 1))

    def _resolve_index(self, identifier: str) -> int:
        """Return the one step index matching ``identifier`` by id, label or process.

        Fails closed on no match and on an ambiguous match. No fuzzy matching.
        """
        matches = [
            index
            for index, one in enumerate(self.steps)
            if identifier in (one.id, one.label, one.process_name)
        ]
        if not matches:
            raise WorkflowError(
                f"workflow '{self.name}': no step matches identifier '{identifier}'."
            )
        if len(matches) > 1:
            raise WorkflowError(
                f"workflow '{self.name}': step identifier '{identifier}' is ambiguous "
                f"(matches {len(matches)} steps)."
            )
        return matches[0]

    # -- run-level evidence ---------------------------------------------------

    def _refused(
        self,
        ctx: Any,
        run_log: RunLog,
        *,
        apply: bool,
        metadata: Optional[dict[str, Any]],
    ) -> WorkflowRun:
        """Record a disabled workflow as a refused run and return it.

        A RUN_START, a terminal RUN_COMPLETE and a finalized run; no step is
        selected or executed.
        """
        name = self.name
        message = (
            f"workflow '{name}' is disabled and was not executed. Set "
            f"'enabled: true' on the workflow definition to run it."
        )
        run = WorkflowRun(
            name=name,
            status="refused",
            context=RunContext(apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx}),
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
        _finalize(ctx, run_log)
        clear_run()
        return run

    def _pre_execution_failure(
        self,
        ctx: Any,
        run_log: RunLog,
        run: WorkflowRun,
        one: WorkflowStep,
        *,
        sequence: int,
        message: str,
    ) -> WorkflowError:
        """Record a step refused before execution as run evidence, then hand it back.

        Written through the same records an in-step failure uses; the run is
        finalized and unbound, and the caller raises the returned error. Not
        reported here: it propagates, and the AppError boundary reports it once.
        """
        failure_id = log_step_failure(run_log,
            failed_step_id=one.id,
            failed_step_name=one.label or one.id,
            message=message,
            error_type="WorkflowError",
            error_message=message,
            sanitized_exception=message,
            failed_step_sequence=sequence,
            process=one.process_name,
        )
        run.outcomes.append(one.settle(
            StepOutcome(one.id, one.label, one.process_name, "failed", error=message)
        ))
        run.status = "failed"
        log_run_complete(run_log,
            "failed",
            message=message,
            failure_record_id=failure_id,
            failed_step_id=one.id,
            failed_step_name=one.label or one.id,
            failure_message=message,
        )
        _finalize(ctx, run_log)
        clear_run()
        return WorkflowError(message)

    def _record_dry_run_skip(
        self, run_log: RunLog, run: WorkflowRun, one: WorkflowStep, sequence: int,
    ) -> None:
        """Record an apply_only step skipped by a dry run: STEP_START, STEP_END skipped."""
        step_name = one.label or one.id
        bind_step(
            step_id=one.id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=self.name,
        )
        step_started = time.monotonic()
        try:
            run_log.set_nest_level("workflow_step")
            log_step_start(
                run_log, step_name, sequence, step_type=one.process_name, step_id=one.id
            )
            run_log.enter()
            try:
                log_step_end(run_log, step_name, "skipped", message="dry-run",
                             duration_ms=monotonic_ms(step_started))
            finally:
                run_log.exit()
            run.outcomes.append(one.settle(
                StepOutcome(one.id, one.label, one.process_name, "skipped", "dry-run")
            ))
            _logger.info("workflow '%s' step '%s' skipped (dry-run).", self.name, one.id)
        finally:
            clear_step()


def _finalize(ctx: Any, run_log: RunLog) -> None:
    """Finalize the run log unless a parent pipeline owns finalization.

    A pipeline step's ctx carries pipeline run identity in ``ctx.runtime``; the
    pipeline then owns the single run-level finalization. With no JSONL file
    there is nothing to finalize from.
    """
    if _get(getattr(ctx, "runtime", None), "pipeline_run_id"):
        return
    if run_log.path() is None:
        return
    finalize_run_log(run_log, ai=getattr(ctx, "shared_ai", None))


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Return obj[key] / obj.key, or default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
