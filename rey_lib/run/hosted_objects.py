"""The objects one running application hosts: the reusable multi-object contract (#1223).

An application exposes this contract wherever it runs; the invoking context owns
whether and for how long an instance is retained -- a BFF PageRuntime starts one
and reuses it while it lives, a Workflow by its own lifetime, a CLI for one
invocation. The application owns its hosted objects' lifetimes, and a consumer
holds only a reference to one object -- its opaque id, over the instance's
transport::

    invoking context -> one application instance -> HostedObjects -> object A, object B ...
    Panel A -> {transport, object A}        Panel B -> {transport, object B}

The protocol on the application's channel:

    {"command": "open", "subject"?, "identity", "request_id"}
        -> {"object": <id>, "projection": ..., "request_id"}
    {"command": "projection", "object", "request_id"?}
        -> {"object": <id>, "projection": ..., "request_id"?}
    {"command": "release", "object", "request_id"?}
        -> {"object": <id>, "released": true, "request_id"?}
    {"command": <any other>, "object", ...}
        -> answered by that object (its own commands: update, execute, invoke, ...)
    {"command": "dispatch", "object", "payload": <semantic command>}
        -> answered by that object, preserving a payload's internal part selector
    {"command": "close"}            ends serving

Every message an object sends names it, so one transport carries many objects'
messages. A refusal is answered ``{"error", "command", "request_id", "object"?}``;
serving goes on. Releasing an object ends the object, never the application;
when serving ends, every object still held is released.

Nothing here knows any subject: the application constructs each subject's hosted
object (``Hosted``) from its identity, handing it the ``emit`` its messages go
out through.
"""

from __future__ import annotations

import uuid
import queue
import threading
from concurrent.futures import Future

from typing import Any, Callable, Mapping, Protocol

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import AppLogger
from rey_lib.run.app_channel import IO_REQUEST_SECONDS

__all__ = ["Emit", "Hosted", "HostedObjects", "ServedObject"]

_logger = AppLogger(__name__)


def _release_idle_sessions() -> None:
    """Give back every idle database session the runtime's owner holds (#1309)."""
    # rey_lib.run is never an import-time dependency of rey_lib.db.
    from rey_lib.db.connection import connection_owner  # noqa: PLC0415

    connection_owner().release_idle()


#: Sends one message about one hosted object; the object's id is added to it.
Emit = Callable[[dict[str, Any]], None]

#: The commands HostedObjects answers itself; any other goes to the object.
_PROTOCOL = frozenset({"open", "projection", "release", "close", "dispatch"})


class Hosted(Protocol):
    """One hosted object, as its application wraps its canonical object."""

    def projection(self) -> dict[str, Any]:
        """The object's current projection, under its governed presentation."""

    def answer(self, command: dict[str, Any]) -> None:
        """Answer one of the object's own commands, through the emit it was given.

        Raises:
            AppError: When the object refuses the command.
        """

    def release(self) -> None:
        """The object is no longer held: let go of what it holds."""


#: Constructs a subject's hosted object from its identity, with its emit.
Construct = Callable[[str, Mapping[str, Any], Emit], Hosted]


class ServedObject:
    """A serving function's channel, scoped to one HostedObjects object.

    Existing hosts keep their serving loops and workers. This carries their
    commands/events on the enclosing application's channel; it launches no
    process and owns no domain state. The enclosing HostedObjects owns release.
    """

    def __init__(self, serve: Callable[[Any], None], emit: Emit,
                 project: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._serve = serve
        self._emit = emit
        self._project = project or (lambda value: value)
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self._ready: Future[dict[str, Any]] = Future()
        self._projection: dict[str, Any] | None = None
        self._waiting: dict[str, Future[dict[str, Any]]] = {}
        self._parts: dict[str, Emit | None] = {}
        self._part_requests: dict[str, str] = {}
        self._guard = threading.RLock()
        self._thread: threading.Thread | None = None
        self._released = False

    def _start(self) -> None:
        with self._guard:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="hosted-object")
                self._thread.start()

    def _run(self) -> None:
        try:
            self._serve(self)
        except Exception as error:
            failure = error if isinstance(error, AppError) else AppError(str(error))
            if failure is not error:
                failure.__cause__ = error
            with self._guard:
                opened = self._ready.done() and not self._released
                if not self._ready.done():
                    self._ready.set_exception(failure)
                waiting = list(self._waiting.values())
            for future in waiting:
                if not future.done():
                    future.set_exception(failure)
            if opened:
                failure.report("hosted object", source=__name__)
                self._emit({"error": str(failure)})
        finally:
            with self._guard:
                if not self._ready.done():
                    self._ready.set_exception(ConfigError("The object ended before projecting."))
            self._emit({"released": True})

    def commands(self):
        while True:
            command = self._commands.get()
            yield command
            _release_idle_sessions()
            if command.get("command") == "close":
                return

    def emit(self, message: dict[str, Any]) -> None:
        with self._guard:
            if self._released:
                return
            waiting = self._waiting.get(str(message.get("request_id") or ""))
        if waiting is not None:
            if not waiting.done():
                waiting.set_result(message)
            return
        with self._guard:
            addressed = self._part_requests.pop(str(message.get("request_id") or ""), "")
        part = str(message.get("object") or addressed)
        if part:
            emit = self._parts.get(part)
            if emit is None:
                if part in self._parts:
                    return  # the presentation of this existing part was released
                raise ConfigError(f"No hosted part is held as {part!r}.")
            emit({key: value for key, value in message.items() if key != "object"})
            return
        if isinstance(message.get("projection"), dict):
            value = self._project(message["projection"])
            with self._guard:
                self._projection = value
                if not self._ready.done():
                    self._ready.set_result(value)
                    return
            message = {**message, "projection": value}
        self._emit(message)

    def projection(self) -> dict[str, Any]:
        self._start()
        try:
            self._ready.result(timeout=IO_REQUEST_SECONDS)
        except TimeoutError as error:
            raise ConfigError("The hosted object sent no initial projection.") from error
        return dict(self._projection or {})

    def answer(self, command: dict[str, Any]) -> None:
        if self._released:
            raise ConfigError("This hosted object has been released.")
        self._commands.put({key: value for key, value in command.items() if key != "object"})

    def dispatch(self, command: dict[str, Any]) -> None:
        """Carry a semantic command with its own object field without replacing it."""
        if self._released:
            raise ConfigError("This hosted object has been released.")
        self._commands.put(dict(command))

    def part(self, name: str, initial: dict[str, Any], emit: Emit,
             on_release: Callable[[], None] | None = None) -> Hosted:
        """Expose an existing serving loop's addressed child through HostedObjects."""
        parent = self
        self._parts[name] = emit

        class Part:
            first = True

            def projection(self) -> dict[str, Any]:
                if self.first:
                    self.first = False
                    return initial
                request_id = uuid.uuid4().hex
                future: Future[dict[str, Any]] = Future()
                with parent._guard:
                    parent._waiting[request_id] = future
                try:
                    parent._commands.put({"command": "projection", "object": name,
                                          "request_id": request_id})
                    result = future.result(timeout=IO_REQUEST_SECONDS)
                    if "error" in result:
                        raise ConfigError(str(result["error"]))
                    return result["projection"]
                finally:
                    with parent._guard:
                        parent._waiting.pop(request_id, None)

            def answer(self, command: dict[str, Any]) -> None:
                if command.get("request_id"):
                    with parent._guard:
                        parent._part_requests[str(command["request_id"])] = name
                parent._commands.put({**command, "object": name})

            def release(self) -> None:
                with parent._guard:
                    parent._parts[name] = None
                    parent._part_requests = {key: part for key, part in parent._part_requests.items()
                                             if part != name}
                if on_release is not None:
                    on_release()

        return Part()

    def release(self) -> None:
        with self._guard:
            if not self._released:
                self._released = True
                if self._thread is not None:
                    self._commands.put({"command": "close"})

    def wait(self) -> None:
        """At application end, wait for this object's existing close/work policy."""
        if self._thread is not None:
            self._thread.join()

    def ended(self) -> bool:
        return self._thread is None or not self._thread.is_alive()


class HostedObjects:
    """One running application's hosted objects, by opaque id.

    Args:
        construct: How the application constructs a subject's hosted object from
            its identity, given the emit its messages go out through. Refuses
            (``AppError``) a subject or identity it cannot serve.
    """

    def __init__(self, construct: Construct) -> None:
        self._construct = construct
        self._guard = threading.RLock()
        self._held: dict[str, Hosted] = {}
        self._retired: list[ServedObject] = []
        # Where the channel this instance serves sends; set when serving begins.
        self._emit: Callable[[dict[str, Any]], None] | None = None

    def open(self, subject: str, identity: Mapping[str, Any],
             emit: Callable[[dict[str, Any]], None]) -> tuple[str, dict[str, Any]]:
        """Construct and hold ``subject``'s object; its new opaque id and its projection.

        Args:
            subject: The governed subject the opening declares, or empty where the
                application forms its subject from the object it constructs.
            identity: Its identity, filters or arguments, as the opening declared.
            emit: Where the application's messages go; each of this object's
                messages carries its id.

        Raises:
            AppError: When the application cannot construct or project it.
        """
        object_id = uuid.uuid4().hex
        hosted = self._construct(subject, dict(identity or {}),
                                 lambda message: self._object_emit(object_id, message, emit))
        with self._guard:
            self._held[object_id] = hosted
        try:
            projection = hosted.projection()
            self._holding(object_id)
        except BaseException:
            with self._guard:
                held = self._held.pop(object_id, None)
            if held is not None:
                self._release(held)
            raise
        return object_id, projection

    def _object_emit(self, object_id: str, message: dict[str, Any], emit: Emit) -> None:
        if message.get("released") is True:
            with self._guard:
                hosted = self._held.pop(object_id, None)
            if hosted is None:
                return
            self._release(hosted)
        emit({**message, "object": object_id})

    def hold(self, make: Callable[[Callable[[dict[str, Any]], None]], Hosted],
             ) -> tuple[str, dict[str, Any]]:
        """Hold an object a held object's own behaviour established (#1141).

        Held exactly as an opened object is -- its own opaque id, every message it
        emits carrying that id, addressed and released like any other -- but made
        by the application, not constructed from an opening's identity.

        Args:
            make: Builds the object, given the emit its messages go out through.

        Returns:
            Its new opaque id and its projection.

        Raises:
            ConfigError: When this instance is not serving a channel.
        """
        if self._emit is None:
            raise ConfigError("Hosted objects hold an established object only while serving.")
        emit = self._emit
        object_id = uuid.uuid4().hex
        hosted = make(lambda message: self._object_emit(object_id, message, emit))
        projection = hosted.projection()
        with self._guard:
            self._held[object_id] = hosted
        return object_id, projection

    def projection(self, object_id: str) -> dict[str, Any]:
        """The current projection of the object held as ``object_id``.

        Raises:
            ConfigError: When no object is held under that id.
        """
        return self._holding(object_id).projection()

    def release(self, object_id: str) -> None:
        """Stop holding the object ``object_id``: the object ends, the application does not.

        Raises:
            ConfigError: When no object is held under that id.
        """
        with self._guard:
            hosted = self._holding(object_id)
            del self._held[object_id]
        self._release(hosted)
        if self._emit is not None:
            self._emit({"object": object_id, "released": True})

    def _release(self, hosted: Hosted) -> None:
        hosted.release()
        if isinstance(hosted, ServedObject):
            with self._guard:
                self._retired = [one for one in self._retired if not one.ended()]
                self._retired.append(hosted)

    def __len__(self) -> int:
        """How many objects are held."""
        return len(self._held)

    def serve(self, channel: Any, *, opening: tuple[str, Mapping[str, Any]] | None = None) -> None:
        """Answer the protocol on ``channel`` until the launcher detaches or closes.

        Every object still held when serving ends is released.

        Args:
            channel: The application's end of the channel (AttachedApp.attach).
            opening: An initial subject and its launch identity. It is held and
                projected before commands, using the same opaque object contract.
        """
        self._emit = channel.emit
        try:
            if opening is not None:
                object_id, projection = self.open(*opening, channel.emit)
                channel.emit({"object": object_id, "projection": projection})
            for command in channel.commands():
                name = str(command.get("command") or "")
                if name == "close":
                    break
                self._answer(channel, name, command)
                # Idle until the next command: no database session is held for
                # the wait (#1309). The runtime's owner ends them; no object
                # or application manages a session.
                _release_idle_sessions()
            else:
                _logger.info("hosted objects: the launcher detached holding %d object(s).",
                             len(self._held))
        finally:
            self._emit = None
            # Releasing an aggregate may also release its held children.
            while True:
                with self._guard:
                    if not self._held:
                        break
                    _object_id, hosted = self._held.popitem()
                self._release(hosted)
            for hosted in self._retired:
                hosted.wait()
            self._retired.clear()

    def _answer(self, channel: Any, name: str, command: dict[str, Any]) -> None:
        """Answer one command: the protocol's here, any other by its object."""
        request_id = command.get("request_id")
        object_id = str(command.get("object") or "")
        try:
            if name == "open":
                object_id, projection = self.open(str(command.get("subject") or ""),
                                                  command.get("identity") or {}, channel.emit)
                answer: dict[str, Any] = {"object": object_id, "projection": projection}
            elif name == "projection":
                answer = {"object": object_id, "projection": self.projection(object_id)}
            elif name == "release":
                self.release(object_id)
                answer = {"object": object_id, "released": True}
            elif name == "dispatch":
                payload = command.get("payload")
                if not isinstance(payload, dict) or not payload.get("command"):
                    raise ConfigError("An object dispatch carries one semantic command.")
                hosted = self._holding(object_id)
                if isinstance(hosted, ServedObject):
                    hosted.dispatch(payload)
                else:
                    hosted.answer(payload)
                return
            elif name and name not in _PROTOCOL:
                # The object's own command: it answers through its emit.
                self._holding(object_id).answer(command)
                return
            else:
                raise ConfigError(f"Hosted objects do not answer the command {name!r}.")
        except Exception as error:
            refusal = error if isinstance(error, AppError) else AppError(str(error))
            if refusal is not error:
                refusal.__cause__ = error
            # Answered to the launcher, so reported here (rule 79).
            refusal.report(f"hosted objects: {name}", source=__name__)
            answer = {"error": str(refusal), "command": name,
                      **({"object": object_id} if object_id else {})}
        if request_id is not None:
            answer["request_id"] = request_id
        channel.emit(answer)

    def _holding(self, object_id: str) -> Hosted:
        """The object held as ``object_id``.

        Raises:
            ConfigError: When no object is held under that id.
        """
        with self._guard:
            hosted = self._held.get(str(object_id or ""))
        if hosted is None:
            raise ConfigError(f"No object is held as {object_id!r}.")
        return hosted
