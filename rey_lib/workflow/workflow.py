"""Workflow -- the orchestrator of a fully hydrated workflow graph.

The root of the workflow object model (backlog 950). A Workflow holds its
governed workflow-level configuration, its
WorkflowProcess children, each grouping its WorkflowStep children, and the
Control it creates child Runs through. The workflow hydrator creates that whole
graph in one pass; by the time ``direct`` is called every object is fully hydrated,
and nothing here hydrates, merges, propagates or reconstructs configuration.

``direct(step, from_step, to_step)`` directs the steps (backlog 964). It creates
its own wrapper Run -- Workflow Run -> Step Runs -- which writes through the
Workflow's log, as each Step Run writes through the wrapper Run's. For
each hydrated step, in workflow-wide ordinal order, the Workflow decides whether
it is released (selection, dry run, earlier failure, ``always_run``) and sets
its indent level. A released step runs itself: it checks its own contract,
creates its own Step Run under the Workflow Run and executes through it. A step
a terminal run-time rule holds back gets a Step Run the Workflow creates,
finished NOT RUN. ``step_sequence`` is the step's ordinal. Running a step
changes the step's own state; it stays in the graph, so the Workflow is
reusable and a later ``direct`` releases its steps again. The Workflow is RUNNING
while ``direct`` executes, and refuses a second ``direct`` until it returns. It tells
its observers each time it or one of its steps changes. ``direct`` finishes and
returns the Workflow Run.

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

import threading

from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from rey_lib.config.applications import Application
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import resolve_config_paths
from rey_lib.config.config_utils import record_config_file_references
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.bootstrap import open_shared_control
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import (
    AppLogger,
    bind_run,
    clear_run,
    log_run_complete,
    log_run_start,
    log_step_failure,
)
from rey_lib.run.run import FAILED, SUCCEEDED, Run
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import RUNNING, _STEP_SUBJECT, WorkflowStep

__all__ = ["Workflow"]

_logger = AppLogger(__name__)

#: The subject type of a Workflow's own Run, as a workflow launch records it.
_WORKFLOW_SUBJECT = "workflow"

#: A Workflow's state when no ``direct`` is executing.
IDLE = "IDLE"


class Workflow:
    """A fully hydrated workflow: its governed state, processes and steps.

    Attributes
    ----------
    name : str
        The workflow's governed name.
    enabled : bool
        False refuses every run; it is reported as a refused run.
    config : dict | None
        The workflow's configuration as governed (control.workflow.config).
    control : Any
        The Control the child Runs are created through (``Run.start``).
    processes : tuple[WorkflowProcess, ...]
        The workflow's processes, as hydration gives them.
    steps : dict[str, WorkflowStep]
        The steps of every process, keyed by ``WorkflowStep.id``, in
        workflow-wide ordinal order.
    state : str
        ``RUNNING`` while ``direct`` executes, else ``IDLE``.
    released : WorkflowStep | None
        The step this Workflow last released -- the one running while it directs.
    """

    def __init__(
        self,
        *,
        name: str,
        enabled: bool,
        config: Optional[Mapping[str, Any]],
        processes: Sequence[WorkflowProcess],
        control: Any,
        runtime: Any = None,
        log: Any = None,
    ) -> None:
        """Hold the workflow's governed state and take ownership of its processes.

        Parameters
        ----------
        name, enabled
            The workflow's governed identity and switch.
        config : Mapping[str, Any] | None
            The workflow's governed configuration. Copied.
        processes : Sequence[WorkflowProcess]
            The workflow's processes, each already hydrated with its steps.
        control : Any
            The existing Control the child Runs are created through.
        runtime : Any
            The runtime this Workflow was hydrated from: the context every step
            executes in.
        log : RunLog | None
            The Workflow's log. Its wrapper Run writes through it, and each Step
            Run through the wrapper Run's.

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
        self.config: Optional[dict[str, Any]] = (
            None if config is None else deepcopy(dict(config))
        )
        self.control = control
        self.runtime = runtime
        self.log = log
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
        self.steps: dict[str, WorkflowStep] = {
            one.id: one for one in sorted(steps, key=lambda one: one.ordinal)
        }
        for process in self.processes:
            process.workflow = self
        self.state = IDLE
        #: The step this Workflow last released, None before it has released one.
        self.released: Optional[WorkflowStep] = None
        # Told when this Workflow or one of its steps changes.
        self._observers: list[Any] = []
        for one in self.steps.values():
            one.observe(self)
        # One direction at a time: held for the whole of ``direct``.
        self._running = threading.Lock()
        # One act at a time: an operation, and the snapshot answering it (the
        # same contract as Source, Transform and Target).
        self._acting = threading.RLock()

    @classmethod
    def hydrate(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        applications: Iterable[Application],
        approved: Mapping[str, Mapping[str, Callable[..., Any]]],
        control: Any,
        runtime: Any = None,
        log: Any = None,
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
            config=resolved["config"],
            processes=sorted(processes, key=lambda process: process.ordinal),
            control=control,
            runtime=runtime,
            log=log,
        )

    @classmethod
    def from_governed(
        cls,
        ctx: Any,
        workflow_name: Optional[str] = None,
        *,
        workflow_id: Any = None,
        log: Any = None,
    ) -> "Workflow":
        """One workflow of this installation, hydrated from its governed rows.

        Named by its name or by its control.workflow id -- exactly one. As an
        HTTP operation is established: the runtime's one Control reads the
        installation's rows, and :meth:`hydrate` builds the graph from them with
        the installation's Applications and the implementations each approves.
        By id, the installation-wide read supplies the rows and those of that
        workflow are kept. Nothing is executed or released, and no Run is created.
        The Workflow holds ``ctx`` as its runtime and ``log`` as its own log, so it
        is self-contained.

        Raises:
            ConfigError: If neither or both identities are given, the runtime has
                no control database, or the installation has no such workflow.
        """
        if (workflow_name is None) == (workflow_id is None):
            raise ConfigError("Workflow: name the workflow by its name or its id, exactly one.")
        named = f"'{workflow_name}'" if workflow_id is None else f"id {workflow_id}"
        control = open_shared_control(ctx).shared_control
        if control is None:
            raise ConfigError(
                f"Workflow: this runtime has no control database to read workflow "
                f"{named} from."
            )
        if workflow_id is None:
            rows = control.workflow(ctx.installation.name, workflow_name)
        else:
            rows = [
                row for row in control.workflow(ctx.installation.name)
                if str(row.get("workflow_id")) == str(workflow_id)
            ]
        if not rows:
            raise ConfigError(
                f"Workflow: installation '{ctx.installation.name}' has no workflow "
                f"{named}."
            )
        return cls.hydrate(
            rows,
            applications=ctx.applications,
            approved={
                application.name: application.approved_implementations
                for application in ctx.applications
            },
            control=control,
            runtime=ctx,
            log=log,
        )

    # -- its snapshot --------------------------------------------------------

    def act(self) -> Any:
        """The lock one act is taken under: an operation, and the snapshot answering it."""
        return self._acting

    # -- its observers -------------------------------------------------------

    def observe(self, observer: Any) -> None:
        """Be told, through ``observer.changed(workflow)``, each time this Workflow changes."""
        self._observers.append(observer)

    def step_changed(self, step: WorkflowStep) -> None:
        """One of this Workflow's steps changed its state: so has this Workflow."""
        del step
        self._changed()

    def _become(self, state: str) -> None:
        """Take ``state``, and tell the observers."""
        self.state = state
        self._changed()

    def _changed(self) -> None:
        for observer in list(self._observers):
            observer.changed(self)

    def projection(self, ctx: Any) -> dict[str, Any]:
        """What this Workflow shows: one snapshot of the hydrated graph.

        The hierarchy as it is held (#965): Workflow -> WorkflowProcess (one
        configured Application) -> WorkflowStep. Read from the objects already
        built -- nothing is re-read, re-resolved or rehydrated -- and built from
        copies, so the snapshot is never a path into its state.

        Args:
            ctx: The runtime context. Unused: nothing is read.
        """
        with self._acting:
            return {
                "name": self.name,
                "enabled": self.enabled,
                "state": self.state,
                # The step this Workflow released, and the Run that step created.
                "released": None if self.released is None else {
                    "step": self.released.id,
                    "run_id": (self.released.current_run.run_id
                               if self.released.current_run is not None else None),
                },
                "processes": [
                    {
                        "ordinal": process.ordinal,
                        "name": process.name,
                        "application": process.application.name,
                        "application_icon": process.application.icon,
                        "steps": [
                            {
                                "ordinal": step.ordinal,
                                "id": step.id,
                                "operation": step.operation.name,
                                "apply_only": step.apply_only,
                                "always_run": step.always_run,
                                "state": step.state,
                                "run_id": (step.current_run.run_id
                                           if step.current_run is not None else None),
                            }
                            for step in process.steps
                        ],
                    }
                    for process in self.processes
                ],
            }

    def direct(
        self,
        step: Optional[str] = None,
        from_step: Optional[str] = None,
        to_step: Optional[str] = None,
        *,
        apply: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[Run]:
        """Direct the eligible steps: all, one, or an inclusive range of them.

        The Workflow decides eligibility and releases each eligible step in
        order; it executes none. A released WorkflowStep runs itself through its
        own Run. The Workflow creates its own wrapper Run, which writes through
        the Workflow's log; each Step Run writes through the wrapper Run's. Every
        step executes in this Workflow's ``runtime``.

        Parameters
        ----------
        apply : bool
            False for dry-run: a step with ``apply_only`` is not released (NOT RUN).
        step : str | None
            Run exactly the one step matching this identifier, or every step of
            the process it names.
        from_step, to_step : str | None
            Run the inclusive ordered range between them. A process name starts
            at its first step, or ends at its last.
        metadata : dict | None
            Run metadata seeded into the Workflow Run's ``metadata``.

        Identifiers resolve against each step's ``id``, ``label`` or
        ``process_name``; no match or more than one fails closed.

        Returns
        -------
        Run | None
            The Workflow Run, finished; None for a disabled Workflow, which is
            recorded as refused and creates no Run.

        Raises
        ------
        AppError
            When the Workflow is already running, on an invalid or unresolvable
            selection, or for a step whose settings do not satisfy its contract
            (recorded as run evidence first).
        """
        if not self._running.acquire(blocking=False):
            raise AppError(f"workflow '{self.name}' is already running.")
        try:
            self._become(RUNNING)
            return self._direct(self.runtime, apply=apply, step=step,
                             from_step=from_step, to_step=to_step, metadata=metadata)
        finally:
            try:
                self._become(IDLE)
            finally:
                self._running.release()

    def _direct(
        self,
        ctx: Any,
        *,
        apply: bool,
        step: Optional[str],
        from_step: Optional[str],
        to_step: Optional[str],
        metadata: Optional[dict[str, Any]],
    ) -> Optional[Run]:
        """The release itself, while this Workflow is RUNNING (see :meth:`direct`)."""
        name = self.name
        # Being switched off is a governed outcome, reported as a refused run.
        if self.enabled is False:
            return self._refused(ctx, apply=apply)

        steps = list(self.steps.values())
        selected = self._select(steps, step=step, from_step=from_step, to_step=to_step)

        # The Workflow's own wrapper Run (backlog 964): Workflow Run -> Step Runs.
        # It writes through the Workflow's log, and is the execution context the
        # Step Runs work in.
        workflow_run = Run.start(
            self.control,
            subject_type=_WORKFLOW_SUBJECT,
            subject_id=name,
            subject_name=name,
        )
        workflow_run.log = self.log
        run_log = workflow_run.log
        workflow_run.apply = apply
        workflow_run.metadata.update(metadata or {})
        workflow_run.data["ctx"] = ctx

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

        # The first failure, captured once: the reason ordinary steps stopped.
        # A later always_run failure records itself without replacing it.
        first_failure: dict[str, Any] | None = None
        for index, one in enumerate(steps):
            if index not in selected:
                continue
            # The step's indent level: workflow-step semantic level 5.
            run_log.set_nest_level("workflow_step")
            # A terminal run-time rule holds the step back: an earlier failure
            # (unless always_run), or apply_only in a dry run. It is never
            # released; the Workflow records it NOT RUN under its own Run.
            if (first_failure is not None and not one.always_run) or (
                not apply and one.apply_only
            ):
                _logger.info("workflow '%s' step '%s' not released.", name, one.id)
                self._child_run(workflow_run, one).not_released(
                    step=one, sequence=one.ordinal, workflow_name=name,
                )
                continue

            self.released = one
            # Released: the step checks its own contract, creates its own Run and
            # executes through it. A refusal raises before any Step Run exists.
            try:
                child = one.run(control=self.control, parent_run=workflow_run, ctx=ctx,
                                workflow_name=name)
            except AppError as refusal:
                raise self._pre_execution_failure(
                    workflow_run, one, message=str(refusal),
                ) from refusal
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
        _finalize(workflow_run, SUCCEEDED if first_failure is None else FAILED)
        run_log.enter_phase("close")
        clear_run()
        return workflow_run

    # -- child Runs -----------------------------------------------------------

    def _child_run(self, parent_run: Run, one: WorkflowStep) -> Run:
        """Create the Run of a step that is not released, under the Workflow Run."""
        return Run.start(
            self.control,
            subject_type=_STEP_SUBJECT,
            subject_id=one.id,
            subject_name=one.label,
            app_name=one.process.application.name if one.process is not None else "",
            settings=one.settings,
            parent=parent_run,
        )

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
        selector, every step runs. An identifier naming one of this Workflow's
        processes stands for that process's steps: ``step`` runs all of them,
        ``from_step`` starts at its first, ``to_step`` ends at its last.
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
            first, last = self._resolve_span(steps, step)
            return set(range(first, last + 1))

        start = self._resolve_span(steps, from_step)[0] if from_step is not None else 0
        end = self._resolve_span(steps, to_step)[1] if to_step is not None else total - 1
        if start > end:
            raise AppError(
                f"workflow '{name}': from_step '{from_step}' resolves after to_step "
                f"'{to_step}' (empty range)."
            )
        return set(range(start, end + 1))

    def _resolve_span(self, steps: list[WorkflowStep], identifier: str) -> tuple[int, int]:
        """The first and last step index ``identifier`` stands for.

        One of this Workflow's processes, by name: its first and last step in
        release order. Otherwise the one step it matches.
        """
        for process in self.processes:
            if process.name == identifier and process.steps:
                indices = [index for index, one in enumerate(steps) if one.process is process]
                return min(indices), max(indices)
        index = self._resolve_index(steps, identifier)
        return index, index

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

    def _refused(self, ctx: Any, *, apply: bool) -> None:
        """Record a disabled workflow as a refused run, through the Workflow's log.

        A RUN_START and a terminal RUN_COMPLETE; no Workflow Run is created and no
        step is released. The run log is finalized by the app's common shutdown.
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
        run_log = self.log
        run_log.set_nest_level("workflow")
        log_run_start(run_log, workflow=name, apply=apply)
        bind_run(run_log)
        _logger.warning("%s", message)
        log_run_complete(run_log, "refused", message=message)
        clear_run()

    def _pre_execution_failure(
        self,
        workflow_run: Run,
        one: WorkflowStep,
        *,
        message: str,
    ) -> AppError:
        """Record a released step's contract refusal as run evidence, then hand it back.

        Written through the same records an in-step failure uses; the Workflow Run
        is finished FAILED and unbound, and the caller raises the returned error.
        Not reported here: it propagates, and the AppError boundary reports it once.
        """
        # Run-level evidence: written at the workflow's level, which the Workflow resets.
        run_log = workflow_run.log
        run_log.set_nest_level("workflow")
        failure_id = log_step_failure(run_log,
            failed_step_id=one.id,
            failed_step_name=one.label or one.id,
            message=message,
            error_type="WorkflowError",
            error_message=message,
            sanitized_exception=message,
            failed_step_sequence=one.ordinal,
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
        _finalize(workflow_run, FAILED)
        clear_run()
        return AppError(message)


def _finalize(workflow_run: Run, status: str) -> None:
    """Finish the Workflow Run, ending the Workflow's own chain.

    The run log is not finalized here: that belongs to the app's common shutdown.
    """
    workflow_run.finish(status)
