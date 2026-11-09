"""An application's AI: the governed tasks it declares, run on ai_app's live AI (#1301).

The application-driven execution path (user rulings, #1301):

    application --task_key, text--> ApplicationAI.call --> its own AppTransports
        --> ai_app serve: task, edit, run --> AI.hydrate_task / AI.execute_task
        <-- {event} ... {projection} | {error} <-- relayed to the application's
            own channel under the asking request_id

What is governed: the application's ai_application definition -- its declared
tasks, by application_id, as control.application_installation_vw carries them --
and the installation's configuration (ai.ai_installation_vw). Both are read once,
when the application loads; a task not declared by the application and enabled
in the installation is not available to this application instance (ruling G2).
ai_app hydrates the task from the installation's configuration, so its engine,
tools and defaults are the configured ones (ruling G3); nothing is copied onto
any object and no configuration is stated here.

Communication is the existing common infrastructure, unchanged (rule 113,
application-to-application communication; recipe ai.operate_on_canonical_live_objects
steps 5 and 10): the application holds its own AppTransports, launches ai_app's
serve through it and reaches the live AI through that transport, as rey_bff's
runtime does. ai_app owns the AI and its AITasks; the calling application owns
its live object; this object owns neither. It carries the task's messages back
exactly as ai_app emitted them, reading none of them.
"""

from __future__ import annotations

import json

import threading

from typing import Any, Callable, Mapping, Sequence

from rey_lib.ai.errors import AIConfigurationError, AIRequestError, AIUnavailableError
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger
from rey_lib.run.app_channel import IO_REQUEST_SECONDS
from rey_lib.run.app_transports import AppTransport, AppTransports

__all__ = ["ApplicationAI", "Relay"]

logger = AppLogger(__name__)

#: Where one task's messages go: the calling application's own channel.
Relay = Callable[[dict[str, Any]], None]

#: ai_app's published serve, and the governed AI subject it opens: the
#: installation's one live AI.
_AI_APPLICATION = "ai_app"
_AI_OPERATION = "serve"
_AI_SUBJECT = "ai.settings"


class ApplicationAI:
    """The AI tasks one application may run, and its own transport to ai_app.

    Args:
        task_keys: The governed tasks available to this application instance,
            resolved at load (``at_load``).
        transports: This application's own AppTransports, which holds the
            ai_app it launches for as long as the application serves.
    """

    def __init__(self, task_keys: Sequence[str], transports: AppTransports,
                 application_id: int | None = None) -> None:
        self._task_keys = tuple(str(key) for key in task_keys)
        #: The application's own id (code.application), as its declaration row
        #: carries it: what an AI conversation it owns is recorded under (#1338).
        #: None where no declaration was read.
        self.application_id = None if application_id is None else int(application_id)
        self._transports = transports
        self._transport: AppTransport | None = None
        self._ref = ""
        self._guard = threading.Lock()
        # The object id ai_app holds its live AI under: its opening's first message,
        # {"object", "projection"} (HostedObjects.serve), and what the task command names.
        self._ai_object = ""
        self._opened = threading.Event()
        # Where each held task's messages go, by the object id ai_app holds it under.
        self._relays: dict[str, Relay] = {}

    @classmethod
    def at_load(cls, control: Any, installation: str, installation_id: int | None,
                application: str, *, transports: AppTransports | None = None) -> "ApplicationAI":
        """The tasks available to ``application`` in ``installation``, read once at load.

        A supplied ``transports`` is the invoking execution context's existing
        fabric, so its already-open subject and ai_app share routing. The
        invocation context owns its lifetime; ``close`` ends that invocation.

        The application's declaration (control.application_installation_vw.ai, by
        application_id) names the tasks it supports; the installation's
        configuration (ai.ai_installation_vw) says which are enabled there. A task
        is available only when both say so; a declaration disabled as a whole, or
        absent, makes none available.

        Raises:
            AIConfigurationError: When the installation's id is not resolved -- the
                declaration is read by it, and nothing is guessed.
        """
        if installation_id is None:
            raise AIConfigurationError(
                "An application's AI declaration is read by its installation's id; this "
                "runtime has not resolved one."
            )
        rows = control.get_application_installations(int(installation_id), application)
        declared = (rows[0].get("ai") if rows else None) or {}
        application_id = rows[0].get("application_id") if rows else None
        if declared.get("is_enabled") is False:
            return cls((), transports if transports is not None else AppTransports(),
                       application_id)
        enabled = {
            str(row.get("task_key") or "")
            for row in control.ai_configuration(installation)
            if row.get("is_enabled") is not False
        }
        keys = [
            str(task.get("task_key") or "")
            for task in (declared.get("tasks") or [])
            if task.get("is_enabled") is not False and str(task.get("task_key") or "") in enabled
        ]
        logger.debug("application %s has AI tasks %s in installation %s",
                     application, keys, installation)
        return cls(keys, transports if transports is not None else AppTransports(),
                   application_id)

    def task_keys(self) -> tuple[str, ...]:
        """The governed tasks this application may run, in declared order."""
        return self._task_keys

    def call(self, runtime: Any, task_key: str, text: str, relay: Relay,
             context: Mapping[str, Any] | None = None,
             origin: Mapping[str, Any] | None = None, *,
             subject_reference: Mapping[str, Any] | None = None,
             coordinates: Mapping[str, Any] | None = None) -> None:
        """Run one available task on ai_app's live AI with ``text`` as its input.

        The AI's own sequence over its hosted commands: ``task`` opens the governed
        task (correlated, so its refusal is this call's), ``edit`` gives it the
        text as its prompt -- and ``context`` beside it, where the application
        supplies one: what its live object already holds (a database object's own
        definition, #1301), which the task's tools answer from -- ``run`` executes
        it. ``origin`` is the calling application's own place -- its run, the record
        a new record is parented to, its nesting and correlation
        (``RunLog.parent_context``) -- carried to the task as its ``origin_id``
        (#1305): what AI records of the run, the package it was sent and the
        result, is recorded under exactly that, and under nothing else. Every
        message ai_app emits about that task -- its projection after the edit,
        each event as it happens, the projection as it ended, or the refusal --
        goes to ``relay`` as emitted.

        ``subject_reference`` names the already-live governed subject at task
        creation. ``coordinates`` are its transient transport context on this
        same execution fabric, sent only with run and never persisted as identity.
        Neither input opens a subject or selects its owner.

        Blocks only for the opening; call it off the thread reading commands, as
        any work that outlives a command runs.

        Raises:
            AIRequestError: When the task is not available to this application, or
                ai_app refuses to open it.
            AIUnavailableError: When ai_app cannot be launched or does not attach.
        """
        key = str(task_key or "")
        if key not in self._task_keys:
            raise AIRequestError(
                f"AI task {key!r} is not available to this application; available: "
                f"{', '.join(self._task_keys) or 'none'}."
            )
        if coordinates is not None and not subject_reference:
            raise AIRequestError("Hosted coordinates require a governed subject reference.")
        transport = self._ai_app(runtime)
        request: dict[str, Any] = {"command": "task", "task_key": key,
                                   "object": self._ai_object}
        if subject_reference is not None:
            request["subject_reference"] = dict(subject_reference)
        if origin is not None:
            # The task's origin is a string fact (AITask.origin_id): the place, as text.
            request["origin_id"] = json.dumps(dict(origin), default=str)
        opened = transport.request(request)
        if "error" in opened:
            raise AIRequestError(str(opened["error"]))
        object_id = str((opened.get("opened") or {}).get("object") or "")
        if not object_id:
            raise AIRequestError("ai_app opened the task under no object.")
        with self._guard:
            self._relays[object_id] = relay
        relay(dict(opened))
        changes: dict[str, Any] = {"prompt": text}
        if context is not None:
            changes["context"] = dict(context)
        transport.send({"command": "edit", "object": object_id, "changes": changes})
        transport.send({"command": "run", "object": object_id,
                        **({"coordinates": dict(coordinates)} if coordinates is not None else {})})

    def ended(self) -> bool:
        """Whether this invocation's already-open AI application has ended."""
        with self._guard:
            return self._transport is not None and self._transport.ended()

    def close(self) -> None:
        """The application stops serving: the ai_app it launched ends with it."""
        with self._guard:
            self._transport = None
            self._ref = ""
            self._ai_object = ""
            self._opened.clear()
            self._relays.clear()
        self._transports.close_all()

    def _ai_app(self, runtime: Any) -> AppTransport:
        """This application's running ai_app, launched through its own transports if new.

        One instance for as long as this application serves (rey_bff's
        ``held_operation`` is the precedent); one that has ended is launched again.

        Raises:
            AIUnavailableError: When ai_app cannot be launched, does not attach, or
                opens no AI within the IO bound.
        """
        with self._guard:
            if self._transport is not None and not self._transport.ended():
                return self._transport
            self._ai_object = ""
            self._opened.clear()
            try:
                ref, transport, _release = self._transports.launch(
                    runtime, _AI_APPLICATION, _AI_OPERATION, {"subject": _AI_SUBJECT},
                    self._pushed)
            except AppError as refused:
                raise AIUnavailableError(f"ai_app could not be launched: {refused}") from refused
            if not transport.connected(timeout=IO_REQUEST_SECONDS):
                self._transports.close(ref)
                raise AIUnavailableError("ai_app did not attach.")
            if not self._opened.wait(timeout=IO_REQUEST_SECONDS):
                self._transports.close(ref)
                raise AIUnavailableError("ai_app opened no AI.")
            self._transport, self._ref = transport, ref
            return transport

    def _pushed(self, message: dict[str, Any]) -> None:
        """One message ai_app pushed: the AI's opening once, else to its task's relay, if any.

        The first ``{"object", "projection"}`` is the live AI's opening, as
        HostedObjects.serve announces it: its object id is kept for the task command.
        Every later message goes to the relay of the task object it carries.
        """
        if not isinstance(message, Mapping):
            return
        object_id = str(message.get("object") or "")
        if not self._opened.is_set():
            if object_id and "projection" in message:
                self._ai_object = object_id
                self._opened.set()
            return
        with self._guard:
            relay = self._relays.get(object_id)
        if relay is not None:
            relay(dict(message))
