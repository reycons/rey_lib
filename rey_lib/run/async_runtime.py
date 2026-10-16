"""The Rey async wrapper: the one module that reaches AnyIO.

AppExecute's execution and communication run on AnyIO (user, 2026-10-16;
backlog console.workflow_map), and only through this module: nothing else in
rey_lib imports AnyIO. It offers exactly what AppExecute uses -- a loop that
synchronous callers drive, a child process with its stdout, a localhost TCP
listener and connection, and a stream of plain JSON messages, one per line --
and owns no execution or domain behaviour of its own.

Public API
----------
Loop            An event loop on its own thread, driven from synchronous code.
MessageStream   Plain JSON messages over one connection, one per line.
open_process    Start a child process with its stdout piped.
listen_local    A localhost TCP listener on a free port.
connect_local   Connect to a localhost TCP listener.
"""

from __future__ import annotations

import json
import subprocess

from concurrent.futures import Future
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import anyio

from anyio.abc import Listener, Process, SocketAttribute, SocketStream
from anyio.from_thread import start_blocking_portal
from anyio.streams.buffered import BufferedByteReceiveStream

__all__ = [
    "Loop",
    "MessageStream",
    "accept_one",
    "connect_local",
    "listen_local",
    "open_process",
    "read_chunks",
]

#: The only host the channel binds or connects to.
LOCALHOST = "127.0.0.1"
#: The longest one message may be, framed as one line.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class Loop:
    """An AnyIO event loop on its own thread, driven from synchronous code."""

    def __init__(self) -> None:
        self._context = start_blocking_portal()
        self._portal = self._context.__enter__()
        self._tasks: list[Future[Any]] = []

    def call(self, function: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Run ``function(*args)`` on the loop and return its result."""
        return self._portal.call(function, *args)

    def start(self, function: Callable[..., Awaitable[Any]], *args: Any) -> Future[Any]:
        """Run ``function(*args)`` on the loop in the background."""
        task = self._portal.start_task_soon(function, *args)
        self._tasks.append(task)
        return task

    def close(self) -> None:
        """Cancel what still runs in the background, then stop the loop."""
        for task in self._tasks:
            task.cancel()
        self._context.__exit__(None, None, None)


class MessageStream:
    """Plain JSON objects over one connection, one per line."""

    def __init__(self, stream: SocketStream) -> None:
        self._stream = stream
        self._received = BufferedByteReceiveStream(stream)

    async def send(self, message: Mapping[str, Any]) -> None:
        """Send one message."""
        await self._stream.send(json.dumps(dict(message)).encode("utf-8") + b"\n")

    async def receive(self) -> Optional[dict[str, Any]]:
        """The next message, or None once the other end has closed."""
        try:
            line = await self._received.receive_until(b"\n", MAX_MESSAGE_BYTES)
        except (anyio.EndOfStream, anyio.IncompleteRead, anyio.BrokenResourceError,
                anyio.ClosedResourceError):
            return None
        message = json.loads(line.decode("utf-8"))
        return message if isinstance(message, dict) else None

    async def close(self) -> None:
        """Close the connection."""
        await self._stream.aclose()


async def open_process(
    command: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Process:
    """Start ``command`` with its stdout piped; stdin closed, stderr inherited."""
    return await anyio.open_process(
        list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=None,
        cwd=cwd, env=dict(env) if env is not None else None,
    )


async def listen_local() -> tuple[Listener[SocketStream], int]:
    """A TCP listener on a free localhost port, and that port."""
    listener = await anyio.create_tcp_listener(local_host=LOCALHOST, local_port=0)
    return listener, int(listener.extra(SocketAttribute.local_port))


async def accept_one(listener: Listener[SocketStream]) -> MessageStream:
    """Accept the next connection on ``listener``."""
    # A localhost listener on one address family is a single listener underneath.
    return MessageStream(await listener.listeners[0].accept())  # type: ignore[attr-defined]


async def connect_local(port: int) -> MessageStream:
    """Connect to the localhost listener on ``port``."""
    return MessageStream(await anyio.connect_tcp(LOCALHOST, port))


async def read_chunks(process: Process, deliver: Callable[[str], None]) -> None:
    """Hand each chunk of ``process``'s stdout to ``deliver`` until it closes."""
    if process.stdout is None:
        return
    async for chunk in process.stdout:
        deliver(chunk.decode("utf-8", errors="replace"))
