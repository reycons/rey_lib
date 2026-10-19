"""
The application channel: the two ends of the connection between a launcher and the
application it launched.

The shared app communication every application uses alike -- workflow_app, ai_app
-- separated from the Run it used to live on (backlog 1042, user 2026-10-17):

    LaunchedApp   the launcher's end: launch / launch_operation, the child process,
                  the channel, the application's events and stdout, send, close
    AttachedApp   the application's end: attach(run_id), commands in, emit out
                  (its values made JSON-safe)

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
from typing import Any, Iterator, Mapping, Optional, Sequence

from rey_lib.errors.error_utils import ConfigError, StateError
from rey_lib.files.json import json_value
from rey_lib.run import async_runtime

__all__ = [
    "AttachedApp", "CONNECTED", "EXITED", "LaunchedApp", "STARTING", "STOPPED",
]

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

    def commands(self) -> Iterator[dict[str, Any]]:
        """The launcher's commands, in order, until it closes the channel."""
        while (message := self._loop.call(self._channel.receive)) is not None:
            yield message

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
