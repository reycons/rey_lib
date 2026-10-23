"""
The application channel: the two ends of the connection between a launcher and the
application it launched.

The shared app communication every application uses alike -- workflow_app, ai_app
-- separated from the Run it used to live on (backlog 1042, user 2026-10-17):

    LaunchedApp   the launcher's end: launch / launch_operation, the child process,
                  the channel, the application's events and stdout, send, close
    AttachedApp   the application's end: attach(run_id), commands in, emit out
                  (its values made JSON-safe)

An attachment is one listening port and one one-time token, for the one
application its launcher started. An application never launches, attaches or
holds another: a subject it opens follows that subject's own binding (#1189).

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
import uuid
from typing import Any, Iterator, Mapping, Optional, Sequence

from rey_lib.errors.error_utils import AppError, ConfigError, StateError
from rey_lib.files.json import json_value
from rey_lib.run import async_runtime

__all__ = [
    "AttachedApp", "CONNECTED", "EXITED", "IO_REQUEST_SECONDS", "LaunchedApp", "STARTING",
    "STOPPED",
]

#: Where a launched application finds its launcher, and the token it proves itself with.
_ADDRESS_ENV = "REY_APP_EXECUTE_PORT"
_TOKEN_ENV = "REY_APP_EXECUTE_TOKEN"

#: How long an application asked to close has to end on its own before it is stopped.
_CLOSE_GRACE_SECONDS = 10.0
#: How long an application stopped with SIGTERM is given to end before it is killed.
_KILL_GRACE_SECONDS = 5.0
#: How long a request waits for its answer: the one bound for every request the IO layer
#: carries (#1127), whichever end asks -- a launcher of its application, or an application
#: of another through its execution context's transports (#1196).
IO_REQUEST_SECONDS = 30.0
#: How often a waiting request looks to see whether its channel has closed.
_REQUEST_POLL_SECONDS = 0.2

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
    """

    @classmethod
    def launch(
        cls,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
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
        """
        return cls(command, cwd=cwd, env=env)

    @classmethod
    def launch_operation(
        cls,
        ctx: Any,
        application: str,
        operation: str,
        inputs: Mapping[str, Any],
    ) -> "LaunchedApp":
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
        self._process: Any = None
        #: Whether close has released this launch's process transport (#1168).
        self._released = False
        self._loop = async_runtime.Loop()
        listener, port = self._loop.call(async_runtime.listen_local)
        #: The attachment's listener, until the first attempt spends it -- held so a
        #: process that ends first can close it: it can no longer attach (#1175).
        self._listener = listener
        self._loop.start(self._accept, listener)
        child_env = {**os.environ, **dict(env or {}),
                     _ADDRESS_ENV: str(port), _TOKEN_ENV: self._token}
        self._process = self._loop.call(self._open, list(command), cwd, child_env)
        self._mark("process started")
        self._loop.start(async_runtime.read_chunks, self._process, self._output.put)
        self._exited = self._loop.start(self._wait)

    async def _open(self, command: list[str], cwd: Optional[str],
                    env: Mapping[str, str]) -> Any:
        return await self._io.open_process(command, cwd=cwd, env=env)

    async def _accept(self, listener: Any) -> None:
        """Accept the application's attachment, check its token, then relay its events.

        One connection is accepted and the listener closed before its hello is
        read: the attachment is spent by the first attempt, refused or not.
        """
        self._mark("accepting")
        try:
            channel = await self._io.accept_one(listener, None)
        except async_runtime.CLOSED_ERRORS:
            # Closed under the accept: the process ended before attaching (#1175).
            channel = None
        finally:
            await listener.aclose()
        if channel is None:
            self._mark("not attached")
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

    def _refused(self) -> None:
        """The application did not attach: say so, and end its events.

        Its events end: no channel will carry any (#1175). Its process's end is
        its own exit.
        """
        self._connected.put(False)
        self._events.put(None)

    def _ended(self, exit_code: int) -> None:
        """This handle's end, where no exit was observed: its channel is over."""
        self.exit_code = exit_code
        self._mark(f"ended ({exit_code})")
        if self.state != STOPPED:
            self.state = EXITED

    async def _wait(self) -> None:
        self.exit_code = await self._process.wait()
        self._mark(f"exit observed ({self.exit_code})")
        if self.state != STOPPED:
            self.state = EXITED
        if self._channel is None:
            # Ended before attaching: it can no longer attach, so the attachment is
            # closed and its accept refused -- the launch is over (#1175).
            await self._listener.aclose()
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
        """Wait for the application to end, and answer its exit code.

        Raises:
            TimeoutError: When it has not ended within ``timeout`` seconds.
        """
        self._exited.result(timeout=timeout)
        return self.exit_code

    def _mark(self, point: str) -> None:
        self.trace.append((time.monotonic(), point))

    def close(self) -> None:
        """End the application, then release the loop.

        An application that has attached and is still running is asked to close
        (``{"command": "close"}``) and given ``_CLOSE_GRACE_SECONDS`` to end on its
        own; one that has not ended by then, or never attached, is stopped, and
        killed if it has not ended ``_KILL_GRACE_SECONDS`` after that. One
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
            # SIGTERM is cooperative where its application declared a termination
            # policy (#1182): bounded, then killed -- this handle's own process only.
            try:
                self.wait(timeout=_KILL_GRACE_SECONDS)
            except TimeoutError:
                self._mark("killed after the termination grace")
                self._process.kill()
                self.wait()
        if not self._released:
            # The process has ended; its transport is released on its own loop now,
            # not by a collector after the loop is gone, and whatever still writes to
            # its output keeps a reader (#1168).
            # Once: a second close finds the loop already closed.
            self._released = True
            kept = self._loop.call(async_runtime.release_process, self._process)
            if kept is not None:
                async_runtime.drain(kept)
        self._loop.close()
        # Its channel is over with the loop, whatever launched it: whatever reads its
        # events is told so, attached or never, and whatever waits for it to attach
        # is told it never will (#1175). Said again where it was already said.
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
        self._channel: Any = None
        try:
            self._channel = self._loop.call(async_runtime.connect_local, int(port))
            self._loop.call(self._channel.send, {"hello": run_id, "token": token})
        except BaseException:
            # Nothing half-built outlives a failed attach: the channel if one was
            # made, then the loop, whose thread would otherwise keep this process
            # alive after the failure that ended it (#1167).
            if self._channel is not None:
                self._loop.call(self._channel.close)
            self._loop.close()
            raise
        # One sender at a time: an application may emit from more than one thread.
        self._sending = threading.Lock()
        # The requests this application has sent and not yet had answered, by
        # request_id: where commands() hands each answer when it arrives.
        self._pending: dict[str, queue.Queue[Optional[dict[str, Any]]]] = {}
        self._waiting = threading.Lock()

    def commands(self) -> Iterator[dict[str, Any]]:
        """The launcher's commands, in order, until it closes the channel.

        An answer to one of this application's own requests (:meth:`request`) is
        not a command: it goes to the request waiting for it, by its request_id,
        and is never yielded. When the channel closes, every request still waiting
        is told so.
        """
        while (message := self._loop.call(self._channel.receive)) is not None:
            waiter = self._waiter(message)
            if waiter is not None:
                _hand(waiter, message)
                continue
            yield message
        self._release_waiting()

    def request(
        self,
        destination: str,
        payload: Mapping[str, Any],
        timeout: float = IO_REQUEST_SECONDS,
    ) -> dict[str, Any]:
        """Send one request to another application, and answer what comes back (#1196).

        ``destination`` is the target instance's transport token in this
        application's execution context; ``payload`` is the command the target
        answers. The request travels over this application's own channel -- the
        execution context's transports route it and route the answer back -- under
        a request_id minted here. The answer is returned exactly as it arrived:
        ``{"answer": <the target's message>, "request_id": ...}`` or ``{"error":
        ..., "request_id": ...}``; what it means is the caller's.

        The answer is read by :meth:`commands`, so this must be called from a
        thread other than the one iterating it, as any work that outlives a
        command already runs (recipe app.serve_a_live_object_to_a_panel step 4).

        Raises:
            AppError: When no answer arrives within ``timeout`` seconds, or the
                channel closes first -- naming the destination asked.
        """
        request_id = uuid.uuid4().hex
        answered: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(maxsize=1)
        with self._waiting:
            self._pending[request_id] = answered
        try:
            self.emit({"route": {"destination": str(destination), "payload": dict(payload)},
                       "request_id": request_id})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(
                        f"No answer from {destination!r} within {timeout:g} s."
                    )
                try:
                    message = answered.get(timeout=min(_REQUEST_POLL_SECONDS, remaining))
                except queue.Empty:
                    continue
                if message is None:
                    raise AppError(
                        f"The channel closed before {destination!r} answered."
                    )
                return message
        finally:
            with self._waiting:
                self._pending.pop(request_id, None)

    def subject_request(
        self,
        payload: Mapping[str, Any],
        timeout: float = IO_REQUEST_SECONDS,
    ) -> dict[str, Any]:
        """Ask for an operation on the subject this application is bound to (#1141 S3).

        Destination-free: the application names no host, transport, token or
        ref -- reaching the already-bound subject is the communication layer's,
        never the application's (recipes #42 step 5, #45 step 10). ``payload``
        is opaque here and below: it travels as ``{"subject_request": payload,
        "request_id": ...}`` on this application's own channel, under a
        request_id minted here, and the answer is returned exactly as it
        arrived; what either means is the caller's and the subject host's.

        The answer is read by :meth:`commands`, so this must be called from a
        thread other than the one iterating it.

        Raises:
            AppError: When no answer arrives within ``timeout`` seconds, or the
                channel closes first.
        """
        request_id = uuid.uuid4().hex
        answered: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(maxsize=1)
        with self._waiting:
            self._pending[request_id] = answered
        try:
            self.emit({"subject_request": dict(payload), "request_id": request_id})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(
                        f"No answer from the bound subject within {timeout:g} s."
                    )
                try:
                    message = answered.get(timeout=min(_REQUEST_POLL_SECONDS, remaining))
                except queue.Empty:
                    continue
                if message is None:
                    raise AppError("The channel closed before the bound subject answered.")
                return message
        finally:
            with self._waiting:
                self._pending.pop(request_id, None)

    def _waiter(self, message: Any) -> Optional[queue.Queue[Optional[dict[str, Any]]]]:
        """The request waiting for this message, or None when it is a command."""
        if not isinstance(message, dict):
            return None
        with self._waiting:
            return self._pending.get(str(message.get("request_id") or ""))

    def _release_waiting(self) -> None:
        """Tell every request still waiting that the channel has closed."""
        with self._waiting:
            waiting = list(self._pending.values())
        for answered in waiting:
            _hand(answered, None)

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
        """Close the channel and release the loop; a request still waiting is told so."""
        self._release_waiting()
        self._loop.call(self._channel.close)
        self._loop.close()


def _hand(answered: "queue.Queue[Optional[dict[str, Any]]]",
          message: Optional[dict[str, Any]]) -> None:
    """Give a waiting request its answer, or None for a closed channel -- the first one only.

    A request waits for exactly one message; a second (a duplicate answer, or the
    close arriving after the answer) has no one left to receive it.
    """
    try:
        answered.put_nowait(message)
    except queue.Full:
        pass
