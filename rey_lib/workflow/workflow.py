"""Workflow -- the orchestrator of a fully hydrated workflow graph.

The root of the workflow object model (backlog 950). A Workflow holds its
governed workflow-level state -- ``tokens`` and ``config`` -- its
WorkflowProcess children, each grouping its WorkflowStep children, and the
Control it creates child Runs through. The workflow hydrator creates that whole
graph in one pass; by the time ``run`` is called every object is fully hydrated,
and nothing here hydrates, merges, propagates or reconstructs configuration.

``run(parent_run, ...)`` owns orchestration. Every hydrated step is waiting; for
each, in workflow-wide ordinal order, the Workflow decides whether it can run
(selection, dry run, earlier failure, ``always_run``), checks its contract
before releasing it, sets its indent level, assigns its ``step_sequence`` and
creates one child Run for it under the parent Run -- a skipped step gets one
too. The child Run executes the step's bound operation, or records it skipped,
and finishes its own lifecycle. A completed step is removed from the
Workflow's and its process's active steps: steps are single-use, so a second
``run`` once they are consumed is refused. ``run`` returns the parent Run.

The existing logging is consumed exactly as it is.

Built beside the recovered procedural engine (backlog 949), whose
``run_workflow`` is the behavioural reference; nothing here imports it.

Hydration (backlog 963) follows the HTTP operations': each canonical object
hydrates itself from its control.workflow_flat_vw rows. Hydration builds the
graph and nothing else -- no step is released or executed and no Run is
created.

Public API
----------
Workflow       The orchestrator.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from rey_lib.config.applications import Application
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import resolve_config_paths
from rey_lib.config.config_utils import record_config_file_references
from rey_lib.config.inventory import to_plain_data
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import (
    AppLogger,
    bind_run,
    clear_run,
    finalize_run_log,
    log_run_complete,
    log_run_start,
    log_step_failure,
)
from rey_lib.logs.run_log import RunLog
from rey_lib.run.run import FAILED, Run
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep

__all__ = ["Workflow"]

_logger = AppLogger(__name__)

#: The subject type of a step's child Run.
_STEP_SUBJECT = "step"


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
    control : Any
        The Control the child Runs are created through (``Run.start``).
    processes : tuple[WorkflowProcess, ...]
        The workflow's processes, as hydration gives them.
    steps : tuple[WorkflowStep, ...]
        The active steps of every process, in workflow-wide ordinal order; a
        completed step is removed.
    """

    def __init__(
        self,
        *,
        name: str,
        enabled: bool,
        tokens: Optional[Mapping[str, Any]],
        config: Optional[Mapping[str, Any]],
        processes: Sequence[WorkflowProcess],
        control: Any,
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
        control : Any
            The existing Control the child Runs are created through.

        Raises
        ------
        AppError
            When the name is missing, a process already belongs to another
            workflow, or two steps share a workflow-wide ordinal.
        """
        if not name:
            raise AppError("a workflow is missing required 'name'.")
        self.name = name
        self.enabled = enabled
        self.tokens: Optional[dict[str, Any]] = (
            None if tokens is None else deepcopy(dict(tokens))
        )
        self.config: Optional[dict[str, Any]] = (
            None if config is None else deepcopy(dict(config))
        )
        self.control = control
        for process in processes:
            if process.workflow is not None:
                raise AppError(
                    f"workflow '{name}': process '{process.name}' already belongs to "
                    f"workflow '{process.workflow.name}'."
                )
        self.processes: tuple[WorkflowProcess, ...] = tuple(processes)
        steps = [step for process in self.processes for step in process.steps]
        seen: dict[int, str] = {}
        for step in steps:
            if step.ordinal in seen:
                raise AppError(
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

    @classmethod
    def hydrate(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        applications: Iterable[Application],
        approved: Mapping[str, Mapping[str, Callable[..., Any]]],
        control: Any,
    ) -> "Workflow":
        """The workflow its governed rows state, fully hydrated (backlog 963).

        ``rows`` are this workflow's rows of control.workflow_flat_vw, one per
        step. Their configuration is resolved once, through the context build's
        own resolution (``config_paths.resolve_config_paths``) against the
        workflow's declared ``paths``; run-time placeholders survive as they do
        there. Each process is then hydrated with its Application and that
        Application's approved implementation catalog. Nothing is executed or
        released, and no Run is created.

        Raises:
            AppError: When the rows are empty, the workflow is disabled, a
                process names an Application that is not present, or any
                object of the graph is refused.
        """
        if not rows:
            raise AppError("a workflow was hydrated from no rows.")
        first = rows[0]
        name = str(first.get("workflow_name") or "")
        # As HttpOperation.hydrate refuses a disabled job: a disabled workflow
        # is never offered as a hydrated runnable workflow.
        if not first.get("workflow_enabled"):
            raise AppError(f"workflow '{name}' is disabled.")
        config = first.get("workflow_config")
        tree = Namespace({
            "config": config,
            "rows": [
                {"process_config": row.get("process_config"),
                 "step_settings": row.get("step_settings")}
                for row in rows
            ],
        })
        resolve_config_paths(tree, (config or {}).get("paths") or [])
        resolved = to_plain_data(tree)
        rows = [
            {**row, **resolved_row} for row, resolved_row in zip(rows, resolved["rows"])
        ]

        grouped: dict[Any, list[Mapping[str, Any]]] = {}
        for row in rows:
            if row.get("workflow_process_id") is not None:
                grouped.setdefault(row["workflow_process_id"], []).append(row)
        by_name = {application.name: application for application in applications}
        processes = []
        for process_rows in grouped.values():
            application_name = str(process_rows[0].get("application_name") or "")
            application = by_name.get(application_name)
            if application is None:
                raise AppError(
                    f"workflow '{name}': process '{process_rows[0].get('process_name')}' "
                    f"instantiates application '{application_name}', which is not present."
                )
            processes.append(WorkflowProcess.hydrate(
                process_rows, application=application,
                approved=approved.get(application_name) or {},
            ))
        return cls(
            name=name,
            enabled=bool(first.get("workflow_enabled")),
            tokens=None,
            config=resolved["config"],
            processes=sorted(processes, key=lambda process: process.ordinal),
            control=control,
        )

    def run(
        self,
        parent_run: Run,
        ctx: Any,
        run_log: RunLog,
        *,
        apply: bool = True,
        step: Optional[str] = None,
        from_step: Optional[str] = None,
        to_step: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Run:
        """Run the workflow's steps, or a single step or inclusive range of them.

        Parameters
        ----------
        parent_run : Run
            The run the steps' child Runs are created under, and that is returned.
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
            Run metadata seeded into the parent Run's ``metadata``.

        Identifiers resolve against each step's ``id``, ``label`` or
        ``process_name``; no match or more than one fails closed.

        Returns
        -------
        Run
            The parent Run.

        Raises
        ------
        AppError
            When no steps remain (the workflow was already run), on an invalid
            or unresolvable selection, or for a step whose settings do not
            satisfy its contract (recorded as run evidence first).
        """
        name = self.name
        # Steps are single-use: once consumed there is nothing to run.
        if not self.steps:
            raise AppError(
                f"workflow '{name}' has no steps left to run; a Workflow is single-use "
                "and its steps were consumed by an earlier run."
            )
        # Being switched off is a governed outcome, reported as a refused run.
        if self.enabled is False:
            return self._refused(parent_run, ctx, run_log, apply=apply)

        steps = list(self.steps)
        selected = self._select(steps, step=step, from_step=from_step, to_step=to_step)

        # The parent Run is the execution context its children work in.
        parent_run.apply = apply
        parent_run.metadata.update(metadata or {})
        parent_run.data["ctx"] = ctx

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
        for index, one in enumerate(steps):
            if index not in selected:
                continue
            # A prior failure makes ordinary steps ineligible; always_run steps
            # keep their declared position.
            if first_failure is not None and not one.always_run:
                _logger.info(
                    "workflow '%s' step '%s' skipped: an earlier step failed.",
                    name, one.id,
                )
                self._child_run(parent_run, one).skip("earlier step failed")
                self._consume(one)
                continue

            # The contract is part of the release decision: a step whose settings
            # do not satisfy it is never released and gets no child Run.
            refusal = one.contract_refusal(name)
            if refusal:
                raise self._pre_execution_failure(
                    ctx, run_log, one, sequence=sequence + 1, message=refusal,
                )

            sequence += 1
            # The step's indent level: workflow-step semantic level 5.
            run_log.set_nest_level("workflow_step")
            child = self._child_run(parent_run, one)
            if not apply and one.apply_only:
                child.skip("dry-run", step=one, run_log=run_log, sequence=sequence,
                           workflow_name=name)
                _logger.info("workflow '%s' step '%s' skipped (dry-run).", name, one.id)
            else:
                child.execute(one, ctx, run_log, sequence=sequence, workflow_name=name)
            self._consume(one)
            if child.status == FAILED and first_failure is None:
                failure_message = (
                    child.error or child.detail or f"workflow step '{one.id}' failed."
                )
                first_failure = {
                    "message": failure_message,
                    "failure_record_id": child.failure_record_id,
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
        return parent_run

    # -- child Runs and consumption -------------------------------------------

    def _child_run(self, parent_run: Run, one: WorkflowStep) -> Run:
        """Create the child Run of one released step, under the parent Run."""
        return Run.start(
            self.control,
            subject_type=_STEP_SUBJECT,
            subject_id=one.id,
            subject_name=one.label,
            app_name=one.process.application.name if one.process is not None else "",
            settings=one.settings,
            parent=parent_run,
        )

    def _consume(self, one: WorkflowStep) -> None:
        """Remove a completed step from the Workflow's and its process's active steps."""
        self.steps = tuple(each for each in self.steps if each is not one)
        process = one.process
        if process is not None:
            process.steps = tuple(each for each in process.steps if each is not one)

    # -- selection ------------------------------------------------------------

    def _select(
        self,
        steps: list[WorkflowStep],
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
        total = len(steps)
        if step is not None and (from_step is not None or to_step is not None):
            raise AppError(
                f"workflow '{name}': 'step' cannot be combined with 'from_step'/'to_step'."
            )
        if step is None and from_step is None and to_step is None:
            return set(range(total))

        if step is not None:
            return {self._resolve_index(steps, step)}

        start = self._resolve_index(steps, from_step) if from_step is not None else 0
        end = self._resolve_index(steps, to_step) if to_step is not None else total - 1
        if start > end:
            raise AppError(
                f"workflow '{name}': from_step '{from_step}' resolves after to_step "
                f"'{to_step}' (empty range)."
            )
        return set(range(start, end + 1))

    def _resolve_index(self, steps: list[WorkflowStep], identifier: str) -> int:
        """Return the one step index matching ``identifier`` by id, label or process.

        Fails closed on no match and on an ambiguous match. No fuzzy matching.
        """
        matches = [
            index
            for index, one in enumerate(steps)
            if identifier in (one.id, one.label, one.process_name)
        ]
        if not matches:
            raise AppError(
                f"workflow '{self.name}': no step matches identifier '{identifier}'."
            )
        if len(matches) > 1:
            raise AppError(
                f"workflow '{self.name}': step identifier '{identifier}' is ambiguous "
                f"(matches {len(matches)} steps)."
            )
        return matches[0]

    # -- run-level evidence ---------------------------------------------------

    def _refused(self, parent_run: Run, ctx: Any, run_log: RunLog, *, apply: bool) -> Run:
        """Record a disabled workflow as a refused run and return the parent Run.

        A RUN_START, a terminal RUN_COMPLETE and a finalized run; no step is
        released.
        """
        name = self.name
        message = (
            f"workflow '{name}' is disabled and was not executed. Set "
            f"'enabled: true' on the workflow definition to run it."
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
        return parent_run

    def _pre_execution_failure(
        self,
        ctx: Any,
        run_log: RunLog,
        one: WorkflowStep,
        *,
        sequence: int,
        message: str,
    ) -> AppError:
        """Record a step refused before release as run evidence, then hand it back.

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
        return AppError(message)


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
