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

from typing import Any, Callable, Mapping, Protocol

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import AppLogger

__all__ = ["Emit", "Hosted", "HostedObjects"]

_logger = AppLogger(__name__)

#: Sends one message about one hosted object; the object's id is added to it.
Emit = Callable[[dict[str, Any]], None]

#: The commands HostedObjects answers itself; any other goes to the object.
_PROTOCOL = frozenset({"open", "projection", "release", "close"})


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


class HostedObjects:
    """One running application's hosted objects, by opaque id.

    Args:
        construct: How the application constructs a subject's hosted object from
            its identity, given the emit its messages go out through. Refuses
            (``AppError``) a subject or identity it cannot serve.
    """

    def __init__(self, construct: Construct) -> None:
        self._construct = construct
        self._held: dict[str, Hosted] = {}

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
                                 lambda message: emit({**message, "object": object_id}))
        projection = hosted.projection()
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
        hosted = self._holding(object_id)
        del self._held[object_id]
        hosted.release()

    def __len__(self) -> int:
        """How many objects are held."""
        return len(self._held)

    def serve(self, channel: Any) -> None:
        """Answer the protocol on ``channel`` until the launcher detaches or closes.

        Every object still held when serving ends is released.

        Args:
            channel: The application's end of the channel (AttachedApp.attach).
        """
        try:
            for command in channel.commands():
                name = str(command.get("command") or "")
                if name == "close":
                    break
                self._answer(channel, name, command)
            else:
                _logger.info("hosted objects: the launcher detached holding %d object(s).",
                             len(self._held))
        finally:
            for object_id in list(self._held):
                self.release(object_id)

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
            elif name and name not in _PROTOCOL:
                # The object's own command: it answers through its emit.
                self._holding(object_id).answer(command)
                return
            else:
                raise ConfigError(f"Hosted objects do not answer the command {name!r}.")
        except AppError as refusal:
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
        hosted = self._held.get(str(object_id or ""))
        if hosted is None:
            raise ConfigError(f"No object is held as {object_id!r}.")
        return hosted
