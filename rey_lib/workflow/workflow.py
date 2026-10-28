"""Workflow -- the orchestrator of a fully hydrated workflow graph.

The root of the workflow object model (backlog 950). A Workflow holds its
governed workflow-level configuration, its
WorkflowProcess children, each grouping its WorkflowStep children, and the
Control it creates child Runs through. The workflow hydrator creates that whole
graph in one pass; by the time ``direct`` is called every object is fully hydrated,
and nothing here hydrates, merges, propagates or reconstructs configuration.

``direct(step, from_step, to_step)`` directs the steps (backlog 964). There is
no Workflow Run: each Step Run is a child of the Application Run that created
this Workflow (``run_id``) and writes through the Workflow's log. For each
hydrated step, in workflow-wide ordinal order, the Workflow decides whether it
is released (selection, dry run, earlier failure, ``always_run``) and sets its
indent level, and hands the step this direction's execution values -- its
``apply``, and the working ``data`` and ``metadata`` its steps share. A released
step runs itself: it checks its own contract, creates its own Step Run and
executes through it. A step a terminal run-time rule holds back creates its own
Step Run, finished NOT RUN. ``step_sequence`` is the step's ordinal. Running a
step changes the step's own state; it stays in the graph, so the Workflow is
reusable and a later ``direct`` releases its steps again. The Workflow is RUNNING
while ``direct`` executes, and refuses a second ``direct`` until it returns. It tells
its observers each time it or one of its steps changes. ``direct`` returns the
direction's outcome.

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
from rey_lib.errors.error_utils import AppError, ConfigError, build_safe_error_payload
from rey_lib.logs import (
    AppLogger,
    bind_run,
    clear_run,
    log_run_complete,
    log_run_start,
    log_step_failure,
)
from rey_lib.run.run import FAILED, NOT_RUN, SKIPPED, SUCCEEDED
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import RUNNING, WorkflowStep

__all__ = ["Workflow"]

_logger = AppLogger(__name__)


#: A Workflow's state before its first ``direct``, and after a refused one.
IDLE = "IDLE"

#: A step's states that are the outcome of its Run: what it completed as.
_STEP_OUTCOMES = frozenset({SUCCEEDED, FAILED, SKIPPED, NOT_RUN})


class Workflow:
    """A fully hydrated workflow: its governed state, processes and steps.

    Attributes
    ----------
    workflow_id : int
        The workflow's governed identity: its control.workflow id (#1141).
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
        ``RUNNING`` while ``direct`` executes; then the direction's outcome,
        ``SUCCEEDED`` or ``FAILED``, until the next one. ``IDLE`` before the first
        direction and after a refused one.
    released : WorkflowStep | None
        The step this Workflow last released -- the one running while it directs.
    run_id : int | None
        The Application Run of the application that created this Workflow: its
        link to its live application execution. None where no application did.
    """

    def __init__(
        self,
        *,
        workflow_id: Any,
        name: str,
        enabled: bool,
        config: Optional[Mapping[str, Any]],
        processes: Sequence[WorkflowProcess],
        control: Any,
        runtime: Any = None,
        log: Any = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Hold the workflow's governed state and take ownership of its processes.

        Parameters
        ----------
        workflow_id
            The workflow's governed identity, its control.workflow id: an int or a
            string of ASCII digits 0-9, held as an int.
        name, enabled
            The workflow's governed name and switch.
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
            The Workflow's log: the creating application's run log, which each
            Step Run writes through.
        run_id : int | None
            The Application Run id of the application creating this Workflow,
            as that application passes it.

        Raises
        ------
        AppError
            When the governed id or the name is missing, a process already
            belongs to another workflow, or two steps share a workflow-wide
            ordinal.
        """
        governed = _governed_id(workflow_id)
        if governed is None:
            raise AppError(
                f"a workflow is missing its governed id (control.workflow id); "
                f"given {workflow_id!r}."
            )
        self.workflow_id = governed
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
        self.run_id = run_id
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
        #: The held process, and step, this Workflow is opened at (backlog 1126):
        #: owner state naming one of its own parts, None where it is opened whole.
        self.focused_process: Optional[WorkflowProcess] = None
        self.focused_step: Optional[WorkflowStep] = None
        # This direction's route and the Steps that have reported a transition.
        # Outcomes stay on the Steps; no previous direction is inferred from them.
        self._direction_steps: tuple[WorkflowStep, ...] = ()
        self._observed_steps: set[WorkflowStep] = set()
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
        run_id: Optional[int] = None,
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
        # One workflow's rows: its governed identity is theirs, and theirs alone.
        ids = {str(row.get("workflow_id")) for row in rows}
        if len(ids) != 1:
            raise AppError(
                f"workflow '{name}' was hydrated from rows of more than one workflow: "
                f"{', '.join(sorted(ids))}."
            )
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
            workflow_id=first.get("workflow_id"),
            name=name,
            enabled=bool(first.get("workflow_enabled")),
            config=resolved["config"],
            processes=sorted(processes, key=lambda process: process.ordinal),
            control=control,
            runtime=runtime,
            log=log,
            run_id=run_id,
        )

    @classmethod
    def from_governed(
        cls,
        ctx: Any,
        workflow_name: Optional[str] = None,
        *,
        workflow_id: Any = None,
        workflow_process_id: Any = None,
        workflow_step_id: Any = None,
        log: Any = None,
        run_id: Optional[int] = None,
    ) -> "Workflow":
        """One workflow of this installation, hydrated from its governed rows.

        Named by exactly one identity: its name, its control.workflow id, or the
        governed id of one of its processes or steps (backlog 1126). As an HTTP
        operation is established: the runtime's one Control reads the
        installation's rows, and :meth:`hydrate` builds the graph from them with
        the installation's Applications and the implementations each approves.
        By an id, the installation-wide read supplies the rows and those of that
        workflow are kept -- for a process or step id, the workflow those very rows
        place it in -- and a Workflow named by a process or step is focused on it.
        Nothing is executed or released, and no Run is created. The Workflow holds
        ``ctx`` as its runtime and ``log`` as its own log, so it is self-contained,
        and ``run_id`` -- the Application Run id the creating application passes --
        as its link to that live application execution.

        Raises:
            ConfigError: If no identity, or more than one, is given, the runtime
                has no control database, or the installation has no such workflow,
                process or step.
        """
        identities = {
            "name": workflow_name, "id": workflow_id,
            "process id": workflow_process_id, "step id": workflow_step_id,
        }
        given = [(kind, value) for kind, value in identities.items() if value is not None]
        if len(given) != 1:
            raise ConfigError(
                "Workflow: name the workflow by exactly one of its name, its id, or the "
                "id of one of its processes or steps."
            )
        kind, value = given[0]
        named = f"'{value}'" if kind == "name" else f"{kind} {value}"
        control = open_shared_control(ctx).shared_control
        if control is None:
            raise ConfigError(
                f"Workflow: this runtime has no control database to read workflow "
                f"{named} from."
            )
        if workflow_name is not None:
            rows = control.workflow(ctx.installation.name, workflow_name)
        else:
            column = {"id": "workflow_id", "process id": "workflow_process_id",
                      "step id": "workflow_step_id"}[kind]
            every = control.workflow(ctx.installation.name)
            owning = {str(row.get("workflow_id")) for row in every
                      if str(row.get(column)) == str(value)}
            rows = [row for row in every if str(row.get("workflow_id")) in owning]
        if not rows:
            raise ConfigError(
                f"Workflow: installation '{ctx.installation.name}' has no workflow "
                f"{named}."
            )
        workflow = cls.hydrate(
            rows,
            applications=ctx.applications,
            approved={
                application.name: application.approved_implementations
                for application in ctx.applications
            },
            control=control,
            runtime=ctx,
            log=log,
            run_id=run_id,
        )
        if workflow_process_id is not None or workflow_step_id is not None:
            workflow.focus(
                workflow_process_id=workflow_process_id, workflow_step_id=workflow_step_id,
            )
        return workflow

    def focus(self, *, workflow_process_id: Any = None, workflow_step_id: Any = None) -> None:
        """Open this Workflow at one of its own processes or steps (backlog 1126).

        Owner state naming a held part, never a new object: the process or step is
        found among this Workflow's own by its governed id. Focusing a step also
        focuses the process holding it.

        Raises:
            ConfigError: If not exactly one id is given, or this Workflow holds no
                process or step with it.
        """
        if (workflow_process_id is None) == (workflow_step_id is None):
            raise ConfigError(
                f"workflow '{self.name}': focus exactly one process or step."
            )
        with self._acting:
            if workflow_step_id is not None:
                step = next((one for one in self.steps.values()
                             if str(one.governed_id) == str(workflow_step_id)), None)
                if step is None:
                    raise ConfigError(
                        f"workflow '{self.name}' holds no step {workflow_step_id}."
                    )
                self.focused_step, self.focused_process = step, step.process
                return
            process = next((one for one in self.processes
                            if str(one.governed_id) == str(workflow_process_id)), None)
            if process is None:
                raise ConfigError(
                    f"workflow '{self.name}' holds no process {workflow_process_id}."
                )
            self.focused_process, self.focused_step = process, None

    # -- its snapshot --------------------------------------------------------

    def act(self) -> Any:
        """The lock one act is taken under: an operation, and the snapshot answering it."""
        return self._acting

    # -- its observers -------------------------------------------------------

    def observe(self, observer: Any) -> None:
        """Be told, through ``observer.changed(workflow)``, each time this Workflow changes."""
        self._observers.append(observer)

    def step_changed(self, step: WorkflowStep) -> None:
        """One of this Workflow's steps changed its state: so has this Workflow.

        A step that has completed is announced with its outcome.
        """
        if step in self._direction_steps:
            self._observed_steps.add(step)
        completed = step.state in _STEP_OUTCOMES
        self._changed(f"Step {step.id}: {step.state}" if completed else None)

    def _become(self, state: str) -> None:
        """Take ``state``, and tell the observers."""
        self.state = state
        self._changed(f"Workflow state: {state}")

    def _changed(self, notification: Optional[str] = None) -> None:
        """Tell every display observer this Workflow changed (backlog 1045).

        ``notification`` is the event, as this Workflow states it, when the change
        is one: the Workflow entering a state, or a step completing. None for a
        change that is not (a step starting, say). The observer is told both.

        The boundary where the Workflow calls external display behaviour: an
        observer cannot decide whether the domain execution succeeds. Each
        observer's failure is converted to its AppError, reported once (rule 79),
        and the next observer is still told; the Workflow, its Steps and their
        Runs continue by their own lifecycle. Nothing else is caught here.
        """
        for observer in list(self._observers):
            try:
                observer.changed(self, notification)
            except Exception as exc:  # noqa: BLE001 — canonical notification boundary
                error = exc if isinstance(exc, AppError) else AppError(
                    f"workflow '{self.name}': display observer failed")
                if error is not exc:
                    error.__cause__ = exc
                error.report(f"workflow '{self.name}' observer notification", source=__name__)

    def actions(self) -> dict[str, list[dict[str, Any]]]:
        """The existing display subjects and their exact direction commands."""
        def commands(name: Optional[str] = None) -> list[dict[str, Any]]:
            offered = [{"id": "workflow_run", "command": {"command": "direct"}}]
            if name is not None:
                offered.extend(
                    {"id": action, "command": {"command": "direct", selector: name}}
                    for action, selector in (
                        ("workflow_run_to", "to_step"),
                        ("workflow_run_from", "from_step"),
                        ("workflow_run_only", "step"),
                    )
                )
            return offered

        return {
            "workflow": commands(),
            **{f"process:{process.ordinal}": commands(process.name)
               for process in self.processes},
            **{f"step:{step.ordinal}": commands(step.id) for step in self.steps.values()},
        }

    def relationships(self) -> list[dict[str, str]]:
        """Display the held hierarchy and the same ordinal chain direct walks."""
        relationships = []
        for process in self.processes:
            parent = f"process:{process.ordinal}"
            relationships.append({"source": "workflow", "target": parent, "kind": "contains"})
            relationships.extend(
                {"source": parent, "target": f"step:{step.ordinal}", "kind": "contains"}
                for step in process.steps
            )
        steps = list(self.steps.values())
        adjacent = set(zip(steps, steps[1:]))
        relationships.extend(
            {"source": f"step:{before.ordinal}", "target": f"step:{after.ordinal}",
             "kind": "precedes"}
            for before, after in zip(steps, steps[1:])
        )
        # This direction's route: a selected pair that is not already adjacent
        # (1 -> 3 past an unselected 2) is joined by its own precedes (#1050).
        route = self._direction_steps
        relationships.extend(
            {"source": f"step:{before.ordinal}", "target": f"step:{after.ordinal}",
             "kind": "precedes"}
            for before, after in zip(route, route[1:])
            if (before, after) not in adjacent
        )
        selected = {f"step:{step.ordinal}": step for step in self._direction_steps}
        entry = self._direction_steps[0] if self._direction_steps else None
        entry_links = set()
        if entry is not None and entry.process is not None:
            parent = f"process:{entry.process.ordinal}"
            entry_links = {("workflow", parent), (parent, f"step:{entry.ordinal}")}
        for relationship in relationships:
            if relationship["kind"] != "precedes":
                if (relationship["source"], relationship["target"]) in entry_links:
                    relationship["state"] = self._path_state(entry)
                continue
            source, target = relationship["source"], relationship["target"]
            if source in selected and target in selected:
                before = self._path_state(selected[source])
                relationship["state"] = (before if before == "NOT RUN"
                                         else self._path_state(selected[target]))
        return relationships

    def _path_state(self, step: WorkflowStep) -> str:
        """A release-path endpoint's state in this direction, owned by its Step."""
        return step.state if step in self._observed_steps else "PENDING"

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
            # No presentation (#1215): the governed subject's, resolved by
            # Projection.for_subject in the host.
            return {
                "name": self.name,
                "enabled": self.enabled,
                "actions": self.actions(),
                "relationships": self.relationships(),
                "state": self.state,
                # The step this Workflow released, and the Run that step created.
                "released": None if self.released is None else {
                    "step": self.released.id,
                    "run_id": (self.released.current_run.run_id
                               if self.released.current_run is not None else None),
                },
                # The part this Workflow is opened at (backlog 1126), None when whole.
                "focus": None if self.focused_process is None else {
                    "process": self.focused_process.ordinal,
                    "step": None if self.focused_step is None else self.focused_step.id,
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

    def definition(self) -> dict[str, Any]:
        """What this Workflow is, and where its execution stands (#1199 S3).

        Its domain read, independent of how it is presented: identity, whether it
        is enabled, its state and the step it released, and each process's
        Application with its steps and their own states and Runs. No presentation
        (panel type, display commands, display relationships, focus) and nothing it
        was configured with (settings, contracts, config). Read from the objects
        already built, under one act, as copies -- nothing is re-read.
        """
        with self._acting:
            return {
                "workflow_id": self.workflow_id,
                "name": self.name,
                "enabled": self.enabled,
                "state": self.state,
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
    ) -> Optional[str]:
        """Direct the eligible steps: all, one, or an inclusive range of them.

        The Workflow decides eligibility and releases each eligible step in
        order; it executes none. A released WorkflowStep runs itself through its
        own Run, a child of the Application Run (``run_id``) that writes through
        the Workflow's log. The Workflow hands each step this direction's
        ``apply``, ``data`` and ``metadata``, created fresh for every direction.
        Every step executes in this Workflow's ``runtime``.

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
            Seeded into this direction's ``metadata``, which its Step Runs share.

        Identifiers resolve against each step's ``id``, ``label`` or
        ``process_name``; no match or more than one fails closed.

        Returns
        -------
        str | None
            The direction's outcome: ``SUCCEEDED``, or ``FAILED`` when a released
            step failed; None for a disabled Workflow, which is recorded as
            refused and releases nothing.

        Raises
        ------
        AppError
            When the Workflow is already running, on an invalid or unresolvable
            selection, or for a step whose settings do not satisfy its contract
            (recorded as run evidence first).
        """
        if not self._running.acquire(blocking=False):
            raise AppError(f"workflow '{self.name}' is already running.")
        outcome: Optional[str] = FAILED
        try:
            self._direction_steps = ()
            self._observed_steps.clear()
            self._become(RUNNING)
            outcome = self._direct(self.runtime, apply=apply, step=step,
                                   from_step=from_step, to_step=to_step, metadata=metadata)
            return outcome
        finally:
            try:
                # The direction's outcome is this Workflow's state until the next
                # one; a direction that raised failed. A refused one ran nothing.
                self._become(IDLE if outcome is None else outcome)
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
    ) -> Optional[str]:
        """The release itself, while this Workflow is RUNNING (see :meth:`direct`)."""
        name = self.name
        # Being switched off is a governed outcome, reported as a refused run.
        if self.enabled is False:
            return self._refused(ctx, apply=apply)

        steps = list(self.steps.values())
        selected = self._select(steps, step=step, from_step=from_step, to_step=to_step)
        self._direction_steps = tuple(one for index, one in enumerate(steps) if index in selected)
        self._changed()

        # This direction's execution values, handed to each step it releases or
        # holds back: what one step writes in ``data`` or ``metadata``, a later
        # step of the same direction reads. Fresh for every direction.
        run_log = self.log
        data: dict[str, Any] = {}
        shared_metadata: dict[str, Any] = dict(metadata or {})

        try:
            ctx.workflow_name = name
        except (AttributeError, TypeError):
            # A bare object() ctx cannot accept dynamic attributes.
            pass
        # Workflow semantic base -> level 4 (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
        run_log.set_nest_level("workflow")
        log_run_start(run_log, workflow=name, apply=apply)
        # Bound so file_utils records file operations through it. One scope, one
        # clear_run(): whatever leaves this direction, the binding is undone (#1045).
        bind_run(run_log)
        try:
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
                # released; the Step records its NOT RUN transition through its own Run.
                if (first_failure is not None and not one.always_run) or (
                    not apply and one.apply_only
                ):
                    _logger.info("workflow '%s' step '%s' not released.", name, one.id)
                    one.not_released(
                        control=self.control, parent_run_id=self.run_id, log=run_log,
                        apply=apply, data=data, metadata=shared_metadata, workflow_name=name,
                    )
                    continue

                self.released = one
                # Released: the step checks its own contract, creates its own Run and
                # executes through it. A refusal raises before any Step Run exists.
                try:
                    child = one.run(control=self.control, parent_run_id=self.run_id, log=run_log,
                                    apply=apply, data=data, metadata=shared_metadata, ctx=ctx,
                                    workflow_name=name)
                except AppError as refusal:
                    # The step's own refusal propagates unchanged; the Workflow only
                    # records the consequence for its direction.
                    self._pre_execution_failure(one, refusal)
                    raise
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
            run_log.enter_phase("close")
            return SUCCEEDED if first_failure is None else FAILED
        finally:
            clear_run()

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
        processes selects by membership with ``step`` -- exactly the steps that
        process holds, in release order, never a step of another process between
        them -- and is a range endpoint with ``from_step`` (its first step) and
        ``to_step`` (its last), where the range is positional (backlog 1044).
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
            for process in self.processes:
                if process.name == step and process.steps:
                    return {index for index, one in enumerate(steps) if one.process is process}
            return {self._resolve_index(steps, step)}

        start = self._resolve_span(steps, from_step)[0] if from_step is not None else 0
        end = self._resolve_span(steps, to_step)[1] if to_step is not None else total - 1
        if start > end:
            raise AppError(
                f"workflow '{name}': from_step '{from_step}' resolves after to_step "
                f"'{to_step}' (empty range)."
            )
        return set(range(start, end + 1))

    def _resolve_span(self, steps: list[WorkflowStep], identifier: str) -> tuple[int, int]:
        """The first and last step index ``identifier`` stands for, as a range endpoint.

        One of this Workflow's processes, by name: its first and last step in
        release order. Otherwise the one step it matches. A range concern only:
        it does not define a process's membership (see :meth:`_select`).
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

        A RUN_START and a terminal RUN_COMPLETE; no step is released. The run log is finalized by the app's common shutdown.
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
        try:
            _logger.warning("%s", message)
            log_run_complete(run_log, "refused", message=message)
        finally:
            clear_run()

    def _pre_execution_failure(self, one: WorkflowStep, error: AppError) -> None:
        """Record a released step's contract refusal as this direction's failure.

        The evidence is the originating error's own, through the canonical
        evidence path (``build_safe_error_payload``) an in-step failure uses in
        Run.execute; nothing here relabels or recreates it. The caller re-raises
        that same error. Not reported here: it propagates, and the AppError
        boundary reports it once.
        """
        # Run-level evidence: written at the workflow's level, which the Workflow resets.
        run_log = self.log
        run_log.set_nest_level("workflow")
        payload = build_safe_error_payload(
            error,
            failed_step_id=one.id,
            failed_step_name=one.label or one.id,
            failed_step_sequence=one.ordinal,
            process=one.process_name,
        )
        failure_id = log_step_failure(run_log, **payload)
        log_run_complete(run_log,
            "failed",
            message=payload["message"],
            failure_record_id=failure_id,
            failed_step_id=one.id,
            failed_step_name=one.label or one.id,
            failure_message=payload["message"],
        )


def _governed_id(value: Any) -> int | None:
    """The control.workflow id ``value`` denotes, or None when it denotes none.

    An int, or a non-empty string of the ASCII digits 0-9 exactly as given --
    no surrounding space, sign or other digit script. A bool is not an id.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and all(char in "0123456789" for char in value):
        return int(value)
    return None
