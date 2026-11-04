"""
AppExecute: the one implementation of a run -- one identity, created by recording it.

AppExecute is the single implementation and canonical owner of a run's identity,
start, state, execution, logging and finish (user, 2026-10-16; backlog
console.workflow_map). ``Run`` is a temporary compatibility alias for it --
``Run = AppExecute``, literally, not a wrapper or a second implementation -- so
every existing caller of ``Run.start``, ``Run.open``, ``execute`` and the rest is
unchanged. "Run" below names the same object in its compatibility vocabulary.

A Run exists because a row exists. ``Run.start`` inserts into
``control.run_manifest``, the database generates ``run_manifest_id``, and that
value *is* the Run identity -- the application calls it ``run_id``. There is no
separately minted id and no translation table: one value, two vocabularies, and
the procedure map binding is the only place the two names meet.

That is what makes a finished run knowable. Before this, a run was reconstructed
by reading its JSONL log, so a run whose log was unreachable was unreachable
itself, and a client had to carry a path to ask about one. The durable row
outlives the process, so a poll that arrives after the process is gone is
answered from the row rather than failing.

The active run and its stdout (#1311)
-------------------------------------
AppExecute is the traffic cop for an active run and owns that run's stdout
(user, 2026-11-03). The application's objects decide what is logged and write
their output to their own run (:meth:`write`); each run owns its own stdout, a
child run's included, and a child's output is never copied into its parent's.
The run that owns a process -- a workflow's parent run, whose steps run in that
process -- owns the process's stdout and stderr (:meth:`capture_process_output`),
and it owns its child runs: its projection carries each of them, with its own
identity and output, and its observers are told when any of them is written to.
:meth:`projection` is the live run as this object holds it -- identity and
parent, lifecycle status and timestamps, detail, error and artifacts, and the
stdout after a cursor with the cursor that follows it.

What this is not
----------------
Not a reader of execution evidence. It does not interpret log records, count
steps or derive metrics: a workflow's progress, current step and step metrics
are the Workflow's own projection. Nor does it reconstruct a historical run:
:meth:`open` answers a finished run from its manifest row alone, and telling a
caller the run has finished needs only that row.

Layering
--------
``rey_lib.run`` owns the Run domain, and two shortcuts are prohibited: it must
not reach the DB adapter directly, and ``rey_lib.logs`` must not reach up into
it. So the manifest is reached through ``Control`` -- the same chain every other
control operation uses -- and nothing in the logging layer imports this module.

Execution of a workflow step (backlog 950)
------------------------------------------
A Workflow creates one child Run for every WorkflowStep it releases, under the
parent Run it was given. The child Run is the step's execution instance: it
executes the step's bound operation (:meth:`execute`), or records the step as
skipped (:meth:`skip`), and finishes its own lifecycle with ``SUCCEEDED``,
``FAILED`` or ``SKIPPED``. It is the execution context the operation's handler
receives -- ``apply``, ``data`` and ``metadata`` -- and a child Run works in its
parent's ``data`` and ``metadata``, so what one step writes a later step under
the same parent reads.
"""

from __future__ import annotations

import codecs
import logging as _logging
import contextlib
import os
import sys
import threading
import time
from typing import Any, Callable, Iterator, Optional

from rey_lib.errors.error_utils import AppError, build_safe_error_payload
from rey_lib.logs import (
    bind_step,
    clear_step,
    log_step_end,
    log_step_failure,
    log_step_start,
    monotonic_ms,
)

__all__ = ["AppExecute", "Run"]

#: The terminal statuses of a step's child Run.
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
#: A step that receives a Run record but is never released for execution,
#: because a run-time release rule prevents it (backlog 964). Distinct from
#: SKIPPED: the step was not released, not released and then skipped.
NOT_RUN = "NOT RUN"
#: The reason recorded for a NOT RUN step.
NOT_RELEASED = "not released"


class AppExecute:
    """One execution, identified by the manifest row that records it."""

    def __init__(
        self,
        *,
        run_id: int,
        control: Any,
        subject_type: str = "",
        subject_id: str = "",
        subject_name: str = "",
        app_name: str = "",
        parent_run_id: Optional[int] = None,
        settings: Optional[dict[str, Any]] = None,
        status: str = "RUNNING",
        started_at: str = "",
        finished_at: str = "",
    ) -> None:
        """Bind an identity to the Control that recorded it.

        Constructed by :meth:`start` or :meth:`open` rather than directly: a Run
        without a manifest row behind it has an identity nothing else can
        resolve.

        Parameters
        ----------
        run_id : int
            The manifest's ``run_manifest_id``.
        control : Any
            The ``Control`` this run reaches its manifest through.
        subject_type, subject_id, subject_name : str
            What ran -- kind, identifier and display name. Creation facts,
            supplied by the launch site, never recovered later from evidence.
        app_name : str
            The executing owner, which is not the subject.
        parent_run_id : int | None
            The run this one belongs to, for a child execution.
        settings : dict | None
            The launch request, recorded once at creation.
        status, started_at, finished_at : str
            Lifecycle facts as the manifest holds them.
        """
        self.run_id = int(run_id)
        self._control = control
        self.subject_type = subject_type
        self.subject_id = subject_id
        self.subject_name = subject_name
        self.app_name = app_name
        self.parent_run_id = parent_run_id
        self.settings = dict(settings or {})
        self.status = status
        self.started_at = started_at
        self.finished_at = finished_at
        #: The execution context a step's handler receives (backlog 950).
        #: False is a dry run. A child Run started with ``parent`` works in its
        #: parent's ``data`` and ``metadata``: what one step writes, a later
        #: step under the same parent reads.
        self.apply: bool = False
        self.data: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {}
        #: The RunLog this run's records are written through. A child Run
        #: started with ``parent`` writes through its parent's.
        self.log: Any = None
        #: A step execution's outcome, besides its terminal ``status``.
        self.detail: str = ""
        self.error: Optional[str] = None
        self.artifacts: list[Any] = []
        #: The STEP_FAILURE record id when the step failed in execution.
        self.failure_record_id: Any = None
        #: This run's own stdout (#1311): what the application's objects wrote to
        #: it, in order. A child run's is its own and is never its parent's, so it
        #: is not shared by ``start(parent=...)``. Guarded, because the objects
        #: writing and the reader projecting are on different threads.
        self._stdout: list[str] = []
        self._stdout_guard = threading.Lock()
        #: The child runs this run owns, in the order they started, and the parent
        #: that owns this one (``start(parent=...)``): a parent projects its
        #: children, each with its own identity and output.
        self._children: list["AppExecute"] = []
        self._parent: Optional["AppExecute"] = None
        #: Told after every write to this run or to any run it owns (``observe``).
        self._observers: list[Callable[["AppExecute"], None]] = []

    def __repr__(self) -> str:
        """Identify the run by id, subject and status."""
        return (f"Run(run_id={self.run_id}, subject={self.subject_type}:"
                f"{self.subject_id}, status={self.status})")

    # -- creation -----------------------------------------------------------

    @classmethod
    def start(
        cls,
        control: Any,
        *,
        subject_type: str,
        subject_id: str,
        subject_name: str = "",
        app_name: str = "",
        parent_run_id: Optional[int] = None,
        settings: Optional[dict[str, Any]] = None,
        parent: Optional["AppExecute"] = None,
    ) -> "AppExecute":
        """Record a starting run and return it, carrying the id it was given.

        Recording is what creates the identity, so this is the first thing a
        launch does and everything downstream receives the result. A failure
        here is a failure to start: there is no id to run under, so it raises
        rather than degrading to an unrecorded run.

        The subject facts are arguments because they are creation facts. Each
        launch site already holds them -- a workflow route knows its workflow, a
        pipeline coordinator knows its pipeline -- and passing them here is what
        stops them being reconstructed later from a log.

        Parameters
        ----------
        control : Any
            The ``Control`` for this installation's control database.
        subject_type : str
            What kind of thing ran: ``workflow``, ``pipeline``, ``app``.
        subject_id : str
            Its identifier, stable across runs of the same subject.
        subject_name : str
            Display name; falls back to ``subject_id``.
        app_name : str
            The executing owner application.
        parent_run_id : int | None
            The parent run for a child execution.
        settings : dict | None
            The launch request as configured.
        parent : Run | None
            The parent Run of a child execution. The child records
            ``parent.run_id`` as its ``parent_run_id``, and works in the
            parent's ``apply``, ``data`` and ``metadata``.

        Returns
        -------
        Run
            The started run, with ``run_id`` set from the manifest.

        Raises
        ------
        DatabaseError
            If the run could not be recorded, and therefore has no identity.
        """
        if parent is not None:
            parent_run_id = parent.run_id
        # TEMPORARY timing (user, 2026-11-04): step the 30 s before the manifest.
        _t_start = time.monotonic()
        _logging.getLogger(__name__).debug("run timing: AppExecute.start entered")
        run_id = control.create_run_manifest(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name or subject_id,
            app_name=app_name,
            parent_run_id=parent_run_id,
            settings=settings,
            required=True,
        )
        _logging.getLogger(__name__).debug(
            "run timing: create_run_manifest %.3fs", time.monotonic() - _t_start)
        run = cls(
            run_id=int(run_id),
            control=control,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name or subject_id,
            app_name=app_name,
            parent_run_id=parent_run_id,
            settings=settings,
        )
        if parent is not None:
            # The same objects, not copies: working data and metadata written
            # by one child are what a later child of the same parent reads.
            run.apply = parent.apply
            run.data = parent.data
            run.metadata = parent.metadata
            run.log = parent.log
            # The parent owns its child runs: it holds and projects them.
            run._parent = parent
            with parent._stdout_guard:
                parent._children.append(run)
        return run

    # -- reconstruction -----------------------------------------------------

    @classmethod
    def open(cls, control: Any, run_id: int) -> Optional["AppExecute"]:
        """Return the durable Run for ``run_id``, or None if it was never recorded.

        No process, no log path and no evidence: the row is the whole answer.
        This is what a poll falls through to once the live run is gone, and
        keeping it to the row is why a finished run stays knowable for as long
        as the manifest keeps it.

        Parameters
        ----------
        control : Any
            The ``Control`` for this installation's control database.
        run_id : int
            The manifest's ``run_manifest_id``.

        Returns
        -------
        Run | None
            The reconstructed run, or None when no such run was recorded.
        """
        row = control.get_run_manifest(int(run_id))
        if not row:
            return None
        return cls(
            run_id=int(row.get("run_manifest_id") or run_id),
            control=control,
            subject_type=str(row.get("subject_type") or ""),
            subject_id=str(row.get("subject_id") or ""),
            subject_name=str(row.get("subject_name") or ""),
            app_name=str(row.get("app_name") or ""),
            parent_run_id=row.get("parent_run_manifest_id"),
            settings=row.get("settings") or {},
            status=str(row.get("status") or ""),
            started_at=_text(row.get("started_at")),
            finished_at=_text(row.get("finished_at")),
        )

    # -- step execution (backlog 950) ---------------------------------------

    def execute(
        self,
        step: Any,
        ctx: Any,
        *,
        sequence: int,
        workflow_name: str,
    ) -> None:
        """Execute a released WorkflowStep's bound operation, then finish this Run.

        The canonical execution boundary of an application operation: the
        step's approved implementation is called with this Run as its execution
        context and with the step itself, whose parent process holds the
        application configuration; any exception it raises is caught here, reported through
        AppError, recorded as STEP_FAILURE and STEP_END failed, and finishes
        this Run ``FAILED``. It does not escape. A returned result's status
        finishes it ``FAILED`` (``failed``), ``SKIPPED`` (``skipped``) or
        ``SUCCEEDED``. The indent level is the Workflow's, set before this is
        called; the step records go through the existing logging unchanged.

        Parameters
        ----------
        step : WorkflowStep
            The released step: its implementation, settings, id and label.
        ctx : Any
            Resolved application context, the implementation's domain input.
        sequence : int
            The step's sequence, assigned by the Workflow.
        workflow_name : str
            The workflow running the step, for records and messages.
        """
        run_log = self.log
        step_name = step.label or step.id
        bind_step(
            step_id=step.id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=workflow_name,
        )
        # One monotonic mark, read again at whichever STEP_END the step reaches.
        step_started = time.monotonic()
        status = SUCCEEDED
        try:
            log_step_start(
                run_log, step_name, sequence, step_type=step.process_name, step_id=step.id
            )
            # Handler, lifecycle, file and result evidence sits beneath STEP_START.
            run_log.enter()
            try:
                try:
                    # The canonical step itself goes too: what its operation needs
                    # of its application's configuration is its parent process's
                    # (step.process.config), already hydrated (backlog 947).
                    result = step.implementation(ctx, run_log, step.settings, self, step)
                except Exception as exc:  # noqa: BLE001 — canonical execution boundary
                    error_payload = build_safe_error_payload(
                        exc,
                        message=f"workflow '{workflow_name}' step '{step.id}' failed",
                        failed_step_id=step.id,
                        failed_step_name=step_name,
                        failed_step_sequence=sequence,
                    )
                    failure_message = str(error_payload["message"])
                    # Handled -- recorded, and settled as a failed Run rather
                    # than raised -- so reported here (rule 79).
                    error = exc if isinstance(exc, AppError) else AppError(
                        f"workflow '{workflow_name}' step '{step.id}' failed")
                    if error is not exc:
                        error.__cause__ = exc
                    error.report(
                        f"workflow '{workflow_name}' step '{step.id}'", source=__name__
                    )
                    self.failure_record_id = log_step_failure(run_log, **error_payload)
                    log_step_end(run_log, step_name, "failed", message=failure_message,
                                 duration_ms=monotonic_ms(step_started))
                    self.error = failure_message
                    status = FAILED
                else:
                    returned = (
                        str(getattr(result, "status", "ok")) if result is not None else "ok"
                    )
                    self.detail = (
                        str(getattr(result, "detail", "")) if result is not None else ""
                    )
                    self.artifacts = (
                        list(getattr(result, "artifacts", []) or [])
                        if result is not None else []
                    )
                    log_step_end(run_log, step_name, returned, message=self.detail,
                                 duration_ms=monotonic_ms(step_started))
                    if returned == "failed":
                        failure_message = self.detail or f"workflow step '{step.id}' failed."
                        self.failure_record_id = log_step_failure(run_log,
                            failed_step_id=step.id,
                            failed_step_name=step_name,
                            message=failure_message,
                            error_type="WorkflowStepFailed",
                            error_message=failure_message,
                            sanitized_exception=failure_message,
                            failed_step_sequence=sequence,
                        )
                        status = FAILED
                    elif returned == "skipped":
                        status = SKIPPED
            finally:
                # The depth entered beneath STEP_START is left again.
                run_log.exit()
        finally:
            clear_step()
        self.finish(status)

    def skip(
        self,
        detail: str,
        *,
        step: Any = None,
        sequence: Optional[int] = None,
        workflow_name: str = "",
    ) -> None:
        """Record a released WorkflowStep as skipped, then finish this Run ``SKIPPED``.

        A step skipped with a sequence (a dry run) writes STEP_START and STEP_END
        skipped through the existing logging; one skipped without (an earlier
        step failed) writes no step record, as the legacy engine did.
        """
        self.detail = detail
        run_log = self.log
        if run_log is not None and step is not None and sequence is not None:
            step_name = step.label or step.id
            bind_step(
                step_id=step.id,
                step_name=step_name,
                step_sequence=sequence,
                workflow_name=workflow_name,
            )
            step_started = time.monotonic()
            try:
                log_step_start(
                    run_log, step_name, sequence, step_type=step.process_name,
                    step_id=step.id,
                )
                run_log.enter()
                try:
                    log_step_end(run_log, step_name, "skipped", message=detail,
                                 duration_ms=monotonic_ms(step_started))
                finally:
                    run_log.exit()
            finally:
                clear_step()
        self.finish(SKIPPED)

    def not_released(
        self,
        *,
        step: Any,
        sequence: int,
        workflow_name: str = "",
    ) -> None:
        """Record a WorkflowStep that was never released, then finish ``NOT RUN``.

        For a step a run-time release rule held back (backlog 964): it gets a
        Run record, but it is not executed and not skipped. The reason, "not
        released", is recorded in this Run's log the way a skipped step's is --
        STEP_START, then STEP_END with the status and the reason -- and the
        manifest holds ``NOT RUN``.
        """
        self.detail = NOT_RELEASED
        run_log = self.log
        step_name = step.label or step.id
        bind_step(
            step_id=step.id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=workflow_name,
        )
        step_started = time.monotonic()
        try:
            log_step_start(
                run_log, step_name, sequence, step_type=step.process_name,
                step_id=step.id,
            )
            run_log.enter()
            try:
                log_step_end(run_log, step_name, "not run", message=NOT_RELEASED,
                             duration_ms=monotonic_ms(step_started))
            finally:
                run_log.exit()
        finally:
            clear_step()
        self.finish(NOT_RUN)

    # -- completion ---------------------------------------------------------

    def finish(self, status: str, finished_at: Optional[str] = None) -> None:
        """Record the terminal status, after which the live run may disappear.

        Written before the live run is released, so there is never a moment when
        neither the live run nor the manifest can answer for this id.

        Parameters
        ----------
        status : str
            The terminal status, e.g. ``SUCCEEDED``, ``FAILED``, ``SKIPPED``,
            ``ABORTED``.
        finished_at : str | None
            When it ended; the database stamps the current time when omitted.
        """
        self._control.finish_run_manifest(
            self.run_id, status, finished_at=finished_at, required=True)
        self.status = status
        if finished_at:
            self.finished_at = finished_at

    # -- reporting ----------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Return this run's durable state, in the shape a reader polls for.

        The durable facts only. Progress, current step and metrics belong to a
        live run and are derived from its evidence by ``rey_lib.run.state``;
        a finished run reports what the manifest holds, which is what tells a
        reader the run is over.
        """
        return {
            "run_id":        self.run_id,
            "found":         True,
            "live":          False,
            "status":        self.status,
            "subject_type":  self.subject_type,
            "subject_id":    self.subject_id,
            "subject_name":  self.subject_name,
            "app_name":      self.app_name,
            "parent_run_id": self.parent_run_id,
            "settings":      dict(self.settings),
            "started_at":    self.started_at,
            "finished_at":   self.finished_at,
        }

    def rollback_available(self) -> bool:
        """Whether this Run currently offers rollback of its governed file changes.

        The Run owns lifecycle eligibility; Control's existing file-history read
        answers whether mutations remain. Preview and reversal still belong to
        FileManifest/Rollback, never to a Tree row or this execution object.
        """
        # A rollback may outlive the manifest snapshot used to open this Run.
        # Re-read the durable lifecycle before offering or accepting an action.
        row = self._control.get_run_manifest(self.run_id)
        if not row or row.get("status") not in (SUCCEEDED, FAILED, "ABORTED"):
            return False
        return bool(self._control.request_file_rollback(run_id=self.run_id))

    # -- the active run's stdout (#1311) --------------------------------------

    def write(self, text: str) -> None:
        """Append ``text`` to this run's own stdout, and tell its observers.

        Called by the application's objects for the run they are executing in
        -- a workflow writes to the workflow run, a step to its step's run -- and
        by :meth:`capture_process_output` for the process's stdout/stderr. Nothing
        is copied to a parent run; the parent is told, because it owns this one.

        Parameters
        ----------
        text : str
            Output, exactly as it was written; an empty string changes nothing.
        """
        if not text:
            return
        with self._stdout_guard:
            self._stdout.append(str(text))
        self._tell(self)

    def observe(self, observer: Callable[["AppExecute"], None]) -> None:
        """Be told after every write to this run or to any child run it owns.

        ``observer`` receives the run that was written to. It is told on the
        writer's thread and must not write output itself. One that raises is
        detached, then reported once.
        """
        with self._stdout_guard:
            self._observers.append(observer)

    def _tell(self, written: "AppExecute") -> None:
        """Tell this run's observers, then its parent's, that ``written`` changed."""
        with self._stdout_guard:
            observers = list(self._observers)
        for observer in observers:
            try:
                observer(written)
            except Exception as exc:  # noqa: BLE001 -- an observer is external behaviour
                with self._stdout_guard:
                    if observer in self._observers:
                        self._observers.remove(observer)
                error = AppError(f"Run {self.run_id}: an output observer failed.")
                error.__cause__ = exc
                error.report(f"run {self.run_id} output observer", source=__name__)
        if self._parent is not None:
            self._parent._tell(written)

    @contextlib.contextmanager
    def capture_process_output(self) -> Iterator["AppExecute"]:
        """Own this process's stdout and stderr for as long as the block runs.

        For the run that owns the process -- a workflow's parent run, whose
        steps run in-process. Each of file descriptors 1 and 2 is pointed at a
        pipe this run reads; every byte is written on, unchanged, to the
        descriptor it was meant for, so the terminal, the launcher's capture and
        AppLogger's console handler see exactly what they did, and its text is
        this run's output (:meth:`write`). stdout and stderr are one output, in
        the order each was read. Restored when the block ends.
        """
        held: list[tuple[int, int, threading.Thread]] = []
        try:
            for descriptor, stream in ((1, sys.stdout), (2, sys.stderr)):
                stream.flush()
                original = os.dup(descriptor)
                read_end, write_end = os.pipe()
                os.dup2(write_end, descriptor)
                os.close(write_end)
                relay = threading.Thread(
                    target=self._relay_descriptor, args=(read_end, original),
                    name=f"run-{self.run_id}-fd{descriptor}", daemon=True)
                relay.start()
                held.append((descriptor, original, relay))
            yield self
        finally:
            for descriptor, original, _relay in held:
                (sys.stdout if descriptor == 1 else sys.stderr).flush()
                # Pointing the descriptor back closes the pipe's last write end,
                # so its relay reads to the end and stops.
                os.dup2(original, descriptor)
            for _descriptor, original, relay in held:
                relay.join(timeout=5)
                if not relay.is_alive():
                    os.close(original)

    def _relay_descriptor(self, read_end: int, original: int) -> None:
        """Copy one captured descriptor on to where it was going, and into this run."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with os.fdopen(read_end, "rb", buffering=0) as source:
            while chunk := source.read(65536):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(original, view):]
                self.write(decoder.decode(chunk))
        self.write(decoder.decode(b"", final=True))

    def projection(
        self, cursor: Optional[int] = None, *, include_rollback: bool = False,
    ) -> dict[str, Any]:
        """Return the run as this object holds it, with its stdout after ``cursor``.

        The durable facts of :meth:`state`, the run's detail, error and artifacts,
        and its own stdout: ``stdout`` is what was written after ``cursor`` (all of
        it when ``cursor`` is None) and ``stdout_cursor`` is the end of the output
        available now, which is the cursor the next read passes. The cursor is an
        offset into this run's stdout, not a record id. A finished run still held
        keeps its final stdout; a run answered by :meth:`open` has none.

        Parameters
        ----------
        cursor : int | None
            How much of this run's stdout the reader already has.
        include_rollback : bool
            Include Run-owned rollback availability. The rollback presentation
            requests this file-history read; ordinary stdout projections do not.

        Returns
        -------
        dict[str, Any]
            ``{**state(), "detail", "error", "artifacts", "stdout", "stdout_cursor",
            "children"}`` -- ``children`` is each child run this run owns, as that
            run projects itself (all of its output), in the order they started.

        Raises
        ------
        AppError
            When ``cursor`` is negative or past the end of this run's stdout: a
            position this run never handed out.
        """
        with self._stdout_guard:
            output = "".join(self._stdout)
            children = list(self._children)
        start = 0 if cursor is None else int(cursor)
        if start < 0 or start > len(output):
            raise AppError(
                f"Run {self.run_id}: stdout cursor {start} is outside its output "
                f"(0..{len(output)})."
            )
        return {
            **self.state(),
            "detail":        self.detail,
            "error":         self.error,
            "artifacts":     list(self.artifacts),
            "stdout":        output[start:],
            "stdout_cursor": len(output),
            "children":      [child.projection() for child in children],
            **({"rollback_available": self.rollback_available()} if include_rollback else {}),
        }


#: The compatibility vocabulary (user, 2026-10-16): the same class under its
#: existing name, so existing callers are unchanged. Removed once callers move to
#: the final public name.
Run = AppExecute


def _text(value: Any) -> str:
    """Render a timestamp as ISO text, and anything absent as an empty string."""
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)
