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

What this is not
----------------
Not a registry of live runs. While a run executes, its live state is owned by
the execution service and the active-run record it keeps; this object is the
durable identity and the reconstruction of a finished run. The two lifetimes
stay separate: whether a *process* is alive is the execution service's answer,
and what the *Run* is, is this.

Not a second reader of execution evidence. Status, progress, current step and
metrics during execution are derived from the run's own records by
``rey_lib.run.state``, which takes a log path and stays identity-free. Nothing
here duplicates that, and the terminal read deliberately does not reach for it:
telling a caller the run has finished needs only the manifest row.

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

import os
import queue
import secrets
import time
from typing import Any, Iterator, Mapping, Optional, Sequence

from rey_lib.errors.error_utils import AppError, ConfigError, StateError, build_safe_error_payload
from rey_lib.run import async_runtime
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
        run_id = control.create_run_manifest(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name or subject_id,
            app_name=app_name,
            parent_run_id=parent_run_id,
            settings=settings,
            required=True,
        )
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
        return run

    # -- reconstruction -----------------------------------------------------

    # -- execution and communication (user, 2026-10-16) ---------------------

    @classmethod
    def launch(
        cls,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "_LaunchedApp":
        """Start an application and hold the execution and channel to it.

        The application owns its own Application Run; it attaches
        (:meth:`attach`) and says hello with that run's id, which the returned
        handle then carries. The handle is the launcher's execution and
        communication handle, not a second Run: it creates no manifest row and
        owns no Run status. Its lifecycle -- STARTING, CONNECTED, then EXITED or
        STOPPED -- is the launch and connection's, not the Run's.

        Args:
            command: The application's command line.
            cwd: Its working directory.
            env: Added to this process's environment for it.
        """
        return _LaunchedApp(command, cwd=cwd, env=env)

    @classmethod
    def launch_operation(
        cls,
        ctx: Any,
        application: str,
        operation: str,
        inputs: Mapping[str, Any],
    ) -> "_LaunchedApp":
        """Launch one published operation of a configured application (see :meth:`launch`).

        The application builds its own invocation from its configuration and
        the operation's semantic ``inputs``
        (``Application.command_line``); this launches it.

        Raises:
            ConfigError: When ``ctx`` configures no such application, or the
                application refuses the operation or its inputs.
        """
        configured = next(
            (one for one in getattr(ctx, "applications", None) or () if one.name == application),
            None,
        )
        if configured is None:
            raise ConfigError(f"application '{application}' is not configured.")
        return cls.launch(configured.command_line(ctx, operation, inputs))

    @classmethod
    def attach(cls, run: "AppExecute") -> "_AttachedApp":
        """From inside a launched application: the channel to whoever launched it.

        Connects to the launcher's address with the one-time token it was given,
        and says hello with ``run``'s id -- the Application Run this application
        created. Commands arrive through :meth:`_AttachedApp.commands`; events
        leave through :meth:`_AttachedApp.emit`.

        Raises:
            StateError: When this process was not launched through
                :meth:`launch`.
        """
        return _AttachedApp(run)

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


#: Where a launched application finds its launcher, and the token it proves itself with.
_ADDRESS_ENV = "REY_APP_EXECUTE_PORT"
_TOKEN_ENV = "REY_APP_EXECUTE_TOKEN"

#: How long an application asked to close has to end on its own before it is stopped.
_CLOSE_GRACE_SECONDS = 10.0

#: A launched execution's lifecycle: the launch and its connection, not the Run.
STARTING = "STARTING"
CONNECTED = "CONNECTED"
EXITED = "EXITED"
STOPPED = "STOPPED"


class _LaunchedApp:
    """The launcher's handle on one application execution (see AppExecute.launch).

    It holds the child process, the channel to it, its stdout and the events it
    sends, and -- once the application has said hello -- that application's
    Application Run id. It is not a Run.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str],
        env: Optional[Mapping[str, str]],
    ) -> None:
        self._io = async_runtime
        self.state = STARTING
        #: Each lifecycle point reached, with its monotonic time: launch, process
        #: started, accepting, attached, token accepted/refused, hello, connected,
        #: command sent, event, stdout closed, exit observed. Read when a launch has
        #: to be explained.
        self.trace: list[tuple[float, str]] = []
        self._mark("launch")
        #: The launched application's own Application Run id, from its hello.
        self.run_id: Optional[int] = None
        self.exit_code: Optional[int] = None
        self._token = secrets.token_hex(16)
        self._events: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
        self._output: queue.Queue[Optional[str]] = queue.Queue()
        self._connected: queue.Queue[bool] = queue.Queue(maxsize=1)
        self._channel: Any = None
        self._loop = async_runtime.Loop()
        listener, port = self._loop.call(async_runtime.listen_local)
        child_env = {**os.environ, **dict(env or {}),
                     _ADDRESS_ENV: str(port), _TOKEN_ENV: self._token}
        self._process = self._loop.call(self._open, list(command), cwd, child_env)
        self._mark("process started")
        self._loop.start(self._accept, listener)
        self._loop.start(async_runtime.read_chunks, self._process, self._output.put)
        self._exited = self._loop.start(self._wait)

    async def _open(self, command: list[str], cwd: Optional[str],
                    env: Mapping[str, str]) -> Any:
        return await self._io.open_process(command, cwd=cwd, env=env)

    async def _accept(self, listener: Any) -> None:
        """Accept the application's attachment, check its token, then relay its events."""
        self._mark("accepting")
        channel = await self._io.accept_one(listener)
        self._mark("attached")
        await listener.aclose()
        hello = await channel.receive()
        if not hello or hello.get("token") != self._token:
            self._mark("token refused")
            await channel.close()
            self._connected.put(False)
            return
        self._mark("token accepted, hello received")
        self._channel = channel
        self.run_id = hello.get("hello")
        self.state = CONNECTED
        self._mark("connected")
        self._connected.put(True)
        while (message := await channel.receive()) is not None:
            self._mark("event")
            self._events.put(message)
        self._mark("channel closed")
        self._events.put(None)

    async def _wait(self) -> None:
        self.exit_code = await self._process.wait()
        self._mark(f"exit observed ({self.exit_code})")
        if self.state != STOPPED:
            self.state = EXITED
        self._output.put(None)

    def connected(self, timeout: Optional[float] = None) -> bool:
        """Wait until the application has attached; False when it was refused or timed out."""
        try:
            return bool(self._connected.get(timeout=timeout))
        except queue.Empty:
            return False

    def send(self, message: Mapping[str, Any]) -> None:
        """Send one plain message -- a command -- to the application.

        Raises:
            StateError: Before the application has attached.
        """
        if self._channel is None:
            raise StateError("the launched application has not attached yet.")
        self._loop.call(self._channel.send, message)
        self._mark("command sent")

    def events(self, timeout: Optional[float] = None) -> Iterator[dict[str, Any]]:
        """The application's events, in order, until its channel closes."""
        while True:
            try:
                event = self._events.get(timeout=timeout)
            except queue.Empty:
                return
            if event is None:
                return
            yield event

    def output(self, timeout: Optional[float] = None) -> Iterator[str]:
        """The application's stdout, chunk by chunk, until it closes."""
        while True:
            try:
                chunk = self._output.get(timeout=timeout)
            except queue.Empty:
                return
            if chunk is None:
                return
            yield chunk

    def stop(self) -> None:
        """Terminate the application."""
        if self.exit_code is None:
            self.state = STOPPED
            self._process.terminate()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Wait for the application to end, and answer its exit code."""
        self._exited.result(timeout=timeout)
        return self.exit_code

    def _mark(self, point: str) -> None:
        self.trace.append((time.monotonic(), point))

    def close(self) -> None:
        """End the application, then release the loop.

        An application that has attached and is still running is asked to close
        (``{"command": "close"}``) and given ``_CLOSE_GRACE_SECONDS`` to end on its
        own; one that has not ended by then, or never attached, is stopped. One
        that has already ended is not asked.
        """
        if self.run_id is not None and self.exit_code is None:
            self.send({"command": "close"})
            try:
                self.wait(timeout=_CLOSE_GRACE_SECONDS)
            except TimeoutError:
                # It did not end when asked; it is stopped below.
                pass
        self.stop()
        if self.exit_code is None:
            self.wait()
        self._loop.close()


class _AttachedApp:
    """A launched application's end of the channel to its launcher (see AppExecute.attach)."""

    def __init__(self, run: "AppExecute") -> None:
        port = os.environ.get(_ADDRESS_ENV)
        token = os.environ.get(_TOKEN_ENV)
        if not port or not token:
            raise StateError("this application was not launched through AppExecute.launch.")
        self._loop = async_runtime.Loop()
        self._channel = self._loop.call(async_runtime.connect_local, int(port))
        self._loop.call(self._channel.send, {"hello": run.run_id, "token": token})

    def commands(self) -> Iterator[dict[str, Any]]:
        """The launcher's commands, in order, until it closes the channel."""
        while (message := self._loop.call(self._channel.receive)) is not None:
            yield message

    def emit(self, message: Mapping[str, Any]) -> None:
        """Send one plain message -- state or an event -- to the launcher."""
        self._loop.call(self._channel.send, message)

    def close(self) -> None:
        """Close the channel and release the loop."""
        self._loop.call(self._channel.close)
        self._loop.close()


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
