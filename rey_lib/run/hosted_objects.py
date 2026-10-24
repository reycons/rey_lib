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

    {"command": "open", "subject", "identity", "request_id"}
        -> {"object": <id>, "projection": ..., "request_id"}
    {"command": "projection", "object", "request_id"?}
        -> {"object": <id>, "projection": ..., "request_id"?}
    {"command": "release", "object", "request_id"?}
        -> {"object": <id>, "released": true, "request_id"?}
    {"command": "close"}            ends serving

Every answer names its object, so one transport carries many objects' messages.
A refusal is answered ``{"error", "command", "request_id", "object"?}``; serving
goes on. Releasing an object ends the object, never the application.

Nothing here knows any subject: the application supplies how a subject's object
is constructed from its identity, and how an object it holds is projected.
"""

from __future__ import annotations

import uuid

from typing import Any, Callable, Mapping

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import AppLogger

__all__ = ["HostedObjects"]

_logger = AppLogger(__name__)

#: Constructs a subject's object from its identity.
Construct = Callable[[str, Mapping[str, Any]], Any]
#: Projects one held object, opened as ``subject``.
Project = Callable[[str, Any], dict[str, Any]]


class HostedObjects:
    """One running application's hosted objects, by opaque id.

    Args:
        construct: How the application constructs a subject's object from its
            identity. Refuses (``AppError``) a subject or identity it cannot serve.
        project: How the application projects an object it holds, opened as a
            subject.
    """

    def __init__(self, construct: Construct, project: Project) -> None:
        self._construct = construct
        self._project = project
        self._held: dict[str, tuple[str, Any]] = {}

    def open(self, subject: str, identity: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Construct and hold ``subject``'s object; its new opaque id and its projection.

        Raises:
            ConfigError: When no subject is named.
            AppError: When the application cannot construct or project it.
        """
        if not str(subject or "").strip():
            raise ConfigError("An object is opened for a named subject; none was named.")
        held = self._construct(subject, dict(identity or {}))
        projection = self._project(subject, held)
        object_id = uuid.uuid4().hex
        self._held[object_id] = (subject, held)
        return object_id, projection

    def projection(self, object_id: str) -> dict[str, Any]:
        """The current projection of the object held as ``object_id``.

        Raises:
            ConfigError: When no object is held under that id.
        """
        subject, held = self._holding(object_id)
        return self._project(subject, held)

    def release(self, object_id: str) -> None:
        """Stop holding the object ``object_id``: the object ends, the application does not.

        Raises:
            ConfigError: When no object is held under that id.
        """
        self._holding(object_id)
        del self._held[object_id]

    def __len__(self) -> int:
        """How many objects are held."""
        return len(self._held)

    def serve(self, channel: Any) -> None:
        """Answer the service protocol on ``channel`` until the launcher detaches or closes.

        Args:
            channel: The application's end of the channel (AttachedApp.attach).
        """
        for command in channel.commands():
            name = str(command.get("command") or "")
            if name == "close":
                break
            request_id = command.get("request_id")
            object_id = str(command.get("object") or "")
            try:
                if name == "open":
                    object_id, projection = self.open(str(command.get("subject") or ""),
                                                      command.get("identity") or {})
                    answer: dict[str, Any] = {"object": object_id, "projection": projection}
                elif name == "projection":
                    answer = {"object": object_id, "projection": self.projection(object_id)}
                elif name == "release":
                    self.release(object_id)
                    answer = {"object": object_id, "released": True}
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
        else:
            _logger.info("hosted objects: the launcher detached holding %d object(s).",
                         len(self._held))

    def _holding(self, object_id: str) -> tuple[str, Any]:
        """The subject and object held as ``object_id``.

        Raises:
            ConfigError: When no object is held under that id.
        """
        held = self._held.get(str(object_id or ""))
        if held is None:
            raise ConfigError(f"No object is held as {object_id!r}.")
        return held
