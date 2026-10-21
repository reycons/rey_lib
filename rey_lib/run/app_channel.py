"""
The application channel: the two ends of the connection between a launcher and the
application it launched.

The shared app communication every application uses alike -- workflow_app, ai_app
-- separated from the Run it used to live on (backlog 1042, user 2026-10-17):

    LaunchedApp   the launcher's end: launch / launch_operation, the child process,
                  the channel, the application's events and stdout, send, close;
                  or (accepting) the same end for an application launched elsewhere
    AttachedApp   the application's end: attach(run_id), commands in, emit out
                  (its values made JSON-safe), request_attachment

An attachment is one listening port and one one-time token. An attached
application may ask its launcher for another (``request_attachment``) and launch
another application into it (``launch(attachment=...)``): that application
attaches to the same launcher, exactly as one the launcher started itself (#1099).

It carries plain messages and owns no application's behaviour or state. The
application's Run is not part of it: an application attaches with its Application
Run's id, which the hello carries as its identity, and nothing here reads or
changes that Run.
"""

from __future__ import annotations

import os
import queue
import secrets
import threading
import time
from collections import deque
from typing import Any, Iterator, Mapping, Optional, Sequence

from rey_lib.errors.error_utils import AppError, ConfigError, StateError
from rey_lib.files.json import json_value
from rey_lib.run import async_runtime

__all__ = [
    "ATTACH_SECONDS", "AttachedApp", "CONNECTED", "EXITED", "LaunchedApp", "STARTING",
    "STOPPED",
]

#: Where a launched application finds its launcher, and the token it proves itself with.
_ADDRESS_ENV = "REY_APP_EXECUTE_PORT"
_TOKEN_ENV = "REY_APP_EXECUTE_TOKEN"

#: How long an attachment is open for its application, and how long a request for
#: one waits for its answer.
ATTACH_SECONDS = 30.0

#: How long an application asked to close has to end on its own before it is stopped.
_CLOSE_GRACE_SECONDS = 10.0

#: A launched execution's lifecycle: the launch and its connection, not the Run.
STARTING = "STARTING"
CONNECTED = "CONNECTED"
EXITED = "EXITED"
STOPPED = "STOPPED"


class LaunchedApp:
    """The launcher's handle on one application execution.

    It holds the child process, the channel to it, its stdout and the events it
    sends, and -- once the application has said hello -- that application's
    Application Run id. It is not a Run.

    Launched into an attachment another launcher holds, it holds only the
    process: the application attaches there, not here. Accepting
    (:meth:`accepting`), it holds only the channel: the application is launched
    elsewhere, into this handle's ``attachment``.
    """

    @classmethod
    def launch(
        cls,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        attachment: Optional[Mapping[str, Any]] = None,
    ) -> "LaunchedApp":
        """Start an application and hold the execution and channel to it.

        The application owns its own Application Run; it attaches
        (:meth:`AttachedApp.attach`) and says hello with that run's id, which the
        returned handle then carries. The handle is the launcher's execution and
        communication handle, not a second Run: it creates no manifest row and
        owns no Run status. Its lifecycle -- STARTING, CONNECTED, then EXITED or
        STOPPED -- is the launch and connection's, not the Run's.

        Args:
            command: The application's command line.
            cwd: Its working directory.
            env: Added to this process's environment for it.
            attachment: ``{"port", "token"}`` of an attachment another launcher
                holds (:meth:`AttachedApp.request_attachment`). The application
                attaches there; this handle opens no listener, never attaches,
                and holds only the process. Its stdout and stderr are this
                process's own, shared, not piped here: :meth:`output` is empty.
        """
        return cls(command, cwd=cwd, env=env, attachment=attachment)

    @classmethod
    def launch_operation(
        cls,
        ctx: Any,
        application: str,
        operation: str,
        inputs: Mapping[str, Any],
        attachment: Optional[Mapping[str, Any]] = None,
    ) -> "LaunchedApp":
        """Launch one published operation of a configured application (see :meth:`launch`).

        The application builds its own invocation from its configuration and
        the operation's semantic ``inputs``
        (``Application.command_line``); this launches it, into ``attachment``
        when one is given.

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
        return cls.launch(configured.command_line(ctx, operation, inputs), attachment=attachment)

    @classmethod
    def accepting(cls, timeout: float = ATTACH_SECONDS) -> "LaunchedApp":
        """Open one attachment for an application launched elsewhere, and hold its end.

        The same one-shot attachment a launch opens -- one listening port, one
        one-time token, one accepted connection, one hello checked -- with no
        process: ``attachment`` is handed to whoever launches the application
        (``launch(attachment=...)``). The first connection consumes it, whether
        its token is accepted or refused. One that does not come within
        ``timeout`` seconds closes it: the handle is then refused and ended.

        The application's events arrive, and commands go to it, exactly as for
        one launched here. It has no stdout here. It has ended once its channel
        closes (exit code 0) or it was refused (exit code -1).
        """
        return cls(None, cwd=None, env=None, accept_seconds=timeout)

    def __init__(
        self,
        command: Optional[Sequence[str]],
        *,
        cwd: Optional[str],
        env: Optional[Mapping[str, str]],
        attachment: Optional[Mapping[str, Any]] = None,
        accept_seconds: Optional[float] = None,
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
        self._process: Any = None
        #: This handle's own attachment, ``{"port", "token"}``; None when launched
        #: into another launcher's.
        self.attachment: Optional[dict[str, Any]] = None
        self._loop = async_runtime.Loop()
        accepting: Any = None
        if attachment is None:
            listener, port = self._loop.call(async_runtime.listen_local)
            self.attachment = {"port": port, "token": self._token}
            accepting = self._loop.start(self._accept, listener, accept_seconds)
        if command is None:
            # Accepting: the application is launched elsewhere; this end has ended
            # once its channel has.
            self._output.put(None)
            self._exited = accepting
            return
        given = attachment if attachment is not None else self.attachment or {}
        child_env = {**os.environ, **dict(env or {}),
                     _ADDRESS_ENV: str(given.get("port")), _TOKEN_ENV: str(given.get("token"))}
        # Launched into another's attachment, the application shares this
        # process's stdout and stderr: no pipe of this launcher's carries it, so
        # it writes on whether or not this launcher lives (#1168).
        shared_output = attachment is not None
        self._process = self._loop.call(self._open, list(command), cwd, child_env, shared_output)
        self._mark("process started")
        if not shared_output:
            self._loop.start(async_runtime.read_chunks, self._process, self._output.put)
        self._exited = self._loop.start(self._wait)

    async def _open(self, command: list[str], cwd: Optional[str],
                    env: Mapping[str, str], shared_output: bool) -> Any:
        return await self._io.open_process(command, cwd=cwd, env=env,
                                           shared_output=shared_output)

    async def _accept(self, listener: Any, timeout: Optional[float]) -> None:
        """Accept the application's attachment, check its token, then relay its events.

        One connection is accepted and the listener closed before its hello is
        read: the attachment is spent by the first attempt, refused or not.
        """
        self._mark("accepting")
        try:
            channel = await self._io.accept_one(listener, timeout)
        finally:
            await listener.aclose()
        if channel is None:
            self._mark("not attached in time")
            self._refused()
            return
        self._mark("attached")
        hello = await channel.receive()
        if not hello or hello.get("token") != self._token:
            self._mark("token refused")
            await channel.close()
            self._refused()
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
        if self._process is None:
            self._ended(0)

    def _refused(self) -> None:
        """The application did not attach: say so, and end a handle with no process."""
        self._connected.put(False)
        if self._process is None:
            self._events.put(None)
            self._ended(-1)

    def _ended(self, exit_code: int) -> None:
        """An accepting handle's end: its channel is over."""
        self.exit_code = exit_code
        self._mark(f"ended ({exit_code})")
        if self.state != STOPPED:
            self.state = EXITED

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
            StateError: Before the application has attached; or, accepting,
                once it has ended -- an attachment does not outlive its application.
        """
        if self._channel is None:
            raise StateError("the launched application has not attached yet.")
        if self._process is None and self.exit_code is not None:
            raise StateError("the attached application has ended; its attachment is spent.")
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
        """Terminate the application; accepting, close its channel instead."""
        if self.exit_code is None:
            self.state = STOPPED
            if self._process is None:
                if self._channel is not None:
                    self._loop.call(self._channel.close)
                return
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
        that has already ended is not asked. Accepting, stopping closes the
        channel, and an attachment not yet used is closed with the loop.
        """
        if self.run_id is not None and self.exit_code is None:
            self.send({"command": "close"})
            try:
                self.wait(timeout=_CLOSE_GRACE_SECONDS)
            except TimeoutError:
                # It did not end when asked; it is stopped below.
                pass
        self.stop()
        if self.exit_code is None and self._process is not None:
            self.wait()
        self._loop.close()
        if self._process is None:
            # Accepting, its channel is over with the loop: whatever reads its
            # events is told so, attached or never, and whatever waits for it
            # to attach is told it never will.
            self._events.put(None)
            try:
                self._connected.put_nowait(False)
            except queue.Full:
                # It had already attached, or been refused: that answer stands.
                pass
        if self.exit_code is None:
            self._ended(0)


class AttachedApp:
    """A launched application's end of the channel to its launcher."""

    @classmethod
    def attach(cls, run_id: int) -> "AttachedApp":
        """From inside a launched application: the channel to whoever launched it.

        Connects to the launcher's address with the one-time token it was given,
        and says hello with ``run_id`` -- the id of the Application Run this
        application created, carried as its identity; the Run itself is not
        touched. Commands arrive through :meth:`commands`; events leave through
        :meth:`emit`.

        Raises:
            StateError: When this process was not launched through
                :meth:`LaunchedApp.launch`.
        """
        return cls(run_id)

    def __init__(self, run_id: int) -> None:
        port = os.environ.get(_ADDRESS_ENV)
        token = os.environ.get(_TOKEN_ENV)
        if not port or not token:
            raise StateError("this application was not launched through LaunchedApp.launch.")
        self._loop = async_runtime.Loop()
        self._channel = self._loop.call(async_runtime.connect_local, int(port))
        self._loop.call(self._channel.send, {"hello": run_id, "token": token})
        # One sender at a time: an application may emit from more than one thread.
        self._sending = threading.Lock()
        # One reader at a time, and one order: what the launcher sent while a
        # request awaited its answer is held here and read first, as it came.
        self._reading = threading.Lock()
        self._held: deque[dict[str, Any]] = deque()

    def commands(self) -> Iterator[dict[str, Any]]:
        """The launcher's commands, in order, until it closes the channel."""
        while (message := self._next()) is not None:
            yield message

    def request_attachment(self, timeout: float = ATTACH_SECONDS) -> dict[str, Any]:
        """Ask the launcher for one more attachment to it, for another application.

        The launcher answers ``{"port", "token"}``: one one-shot attachment, as
        it opens for an application it launches itself. Launching another
        application into it (``LaunchedApp.launch(attachment=...)``) attaches
        that application to the same launcher. Commands that arrive meanwhile
        are kept, in order, for :meth:`commands`.

        Returns:
            The attachment, with the ``request_id`` it was granted for: what
            :meth:`report_ended` names it by.

        Raises:
            AppError: When no attachment is answered within ``timeout`` seconds,
                or the launcher closes the channel first.
        """
        request_id = secrets.token_hex(8)
        self.emit({"request_attachment": request_id})
        deadline = time.monotonic() + timeout
        with self._reading:
            while (remaining := deadline - time.monotonic()) > 0:
                arrived, message = self._loop.call(self._channel.receive_within, remaining)
                if not arrived:
                    break
                if message is None:
                    raise AppError("The launcher closed the channel before granting an attachment.")
                if message.get("request_id") == request_id and "attachment" in message:
                    return {**dict(message["attachment"]), "request_id": request_id}
                self._held.append(message)
        raise AppError(f"The launcher granted no attachment within {timeout:g} s.")

    def report_ended(self, attachment: Mapping[str, Any], launched: "LaunchedApp") -> None:
        """Tell the launcher when the application launched into ``attachment`` ends.

        The launcher holds the attachment but not the process: only this end
        knows whether the application it launched is still running. When it
        ends, ``{"attachment_ended": <request_id>, "exit_code"}`` is emitted --
        so an application that ends before attaching is known at once, not
        after the attachment's bound. One that attached and ends later is the
        launcher's to ignore. Watched on a thread of its own; nothing here
        waits.
        """
        request_id = attachment.get("request_id")

        def watch() -> None:
            exit_code = launched.wait()
            try:
                self.emit({"attachment_ended": request_id, "exit_code": exit_code})
            except async_runtime.CLOSED_ERRORS:
                # This end has detached: there is no launcher left to tell.
                return

        threading.Thread(target=watch, daemon=True).start()

    def _next(self) -> Optional[dict[str, Any]]:
        """The next command: one held while a request waited, else the next one sent."""
        with self._reading:
            if self._held:
                return self._held.popleft()
            return self._loop.call(self._channel.receive)

    def emit(self, message: Mapping[str, Any]) -> None:
        """Send one plain message -- state or an event -- to the launcher.

        Its values are made JSON-safe on the way out (``json_value``): a date in
        ISO form, a mapping or a sequence value by value, anything else as its
        text. The message keeps its structure and meaning; nothing in it is
        interpreted.

        Safe from any thread: messages sent at once go one after another, each whole.
        """
        with self._sending:
            self._loop.call(self._channel.send, json_value(message))

    def close(self) -> None:
        """Close the channel and release the loop."""
        self._loop.call(self._channel.close)
        self._loop.close()
