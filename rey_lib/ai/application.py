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
import time

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from rey_lib.ai.errors import AIConfigurationError, AIRequestError, AIUnavailableError
from rey_lib.artifacts.envelope import permitted_artifact_types
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger
from rey_lib.run.app_channel import IO_REQUEST_SECONDS
from rey_lib.run.app_transports import Answerer, AppTransport, AppTransports

__all__ = ["AIAcceptance", "ApplicationAI", "ApplicationAISubject", "Relay"]

logger = AppLogger(__name__)

#: Where one task's messages go: the calling application's own channel.
Relay = Callable[[dict[str, Any]], None]

#: How often a wait for a task's ending asks whether the applications it depends on
#: are still running (``ApplicationAI.run_to_end``).
_ALIVE_POLL_SECONDS = 1.0

#: ai_app's published serve, and the governed AI subject it opens: the
#: installation's one live AI.
_AI_APPLICATION = "ai_app"
_AI_OPERATION = "serve"
_AI_SUBJECT = "ai.settings"


def _engines(configured: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The installation's enabled engines by profile_key, its default first (#1341).

    As its AI configuration states them: every row carries the installation's
    engines and its default profile.
    """
    first = configured[0] if configured else {}
    keys = sorted(
        str(one.get("profile_key") or "") for one in (first.get("engines") or [])
        if isinstance(one, Mapping) and one.get("is_enabled") is not False
        and str(one.get("profile_key") or "")
    )
    default = str(first.get("default_profile_key") or "")
    return tuple(([default] if default in keys else []) + [one for one in keys if one != default])


@dataclass(frozen=True)
class AIAcceptance:
    """Whether one AI artifact may be given to one destination of the application.

    Delivery, never execution: a refused artifact is still the completed task's
    answer, and the task's status is the AI's. ``text`` is the artifact to apply
    when accepted; ``reason`` says why it is not.
    """

    accepted: bool
    text: str = ""
    reason: str = ""


class ApplicationAISubject(Protocol):
    """What an application supplies for its AI: its subject, its context, its input
    and its handling of accepted output (#1369).

    The application implements it over its own live object. It owns none of the AI
    lifecycle: ``ApplicationAI.answer`` runs that, asking the application only these.
    """

    def subject_reference(self) -> Optional[Mapping[str, Any]]:
        """The governed subject the AI's task and conversation are about; None for none."""

    def context(self) -> Optional[Mapping[str, Any]]:
        """What the live object holds now, given beside the task's input; None for none."""

    def default_input(self, task_key: str) -> str:
        """The task's input when the command gives no text.

        Raises:
            AppError: When the application has no input to give ``task_key``:
                refused before anything launches.
        """

    def accept(self, destination: str, text: str) -> AIAcceptance:
        """Apply an accepted artifact to ``destination``, or say why not.

        Called only for an artifact the governed declaration accepts for
        ``destination``; whether and how it is applied is the application's.
        """


class ApplicationAI:
    """The AI tasks one application may run, and its own transport to ai_app.

    Args:
        task_keys: The governed tasks available to this application instance,
            resolved at load (``at_load``).
        transports: This application's own AppTransports, which holds the
            ai_app it launches for as long as the application serves.
    """

    def __init__(self, task_keys: Sequence[str], transports: AppTransports,
                 application_id: int | None = None, engines: Sequence[str] = (),
                 destinations: Mapping[str, Sequence[str]] | None = None,
                 task_destinations: Mapping[str, str | None] | None = None) -> None:
        self._task_keys = tuple(str(key) for key in task_keys)
        #: Where this application applies each task's answer, as its declaration
        #: states it: a destination_key, or None -- presentation only.
        self._task_destinations = {
            str(key): (str(value) if value else None)
            for key, value in (task_destinations or {}).items()
        }
        #: What each of this application's destinations accepts from its AI, by
        #: destination_key, as its governed declaration states it: the media types.
        self._destinations = {
            str(key): frozenset(str(one) for one in (types or ()))
            for key, types in (destinations or {}).items()
        }
        #: The installation's enabled engines by profile_key, its default first (#1341):
        #: what the reader may choose to run a session's AI on.
        self._engines = tuple(str(key) for key in engines)
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
        # Where each held task's messages go, by the object id ai_app holds it under.
        self._relays: dict[str, Relay] = {}
        # The application's decision about the last completed task's output, where it
        # made one: {task_key, run_id, applied, reason}. Never the task's status,
        # which is the AITask's own.
        self._disposition: dict[str, Any] | None = None

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
        configured = control.ai_configuration(installation)
        enabled = {
            str(row.get("task_key") or "")
            for row in configured
            if row.get("is_enabled") is not False
        }
        keys = [
            str(task.get("task_key") or "")
            for task in (declared.get("tasks") or [])
            if task.get("is_enabled") is not False and str(task.get("task_key") or "") in enabled
        ]
        logger.debug("application %s has AI tasks %s in installation %s",
                     application, keys, installation)
        destinations = {
            str(task.get("task_key") or ""): task.get("destination_key")
            for task in (declared.get("tasks") or [])
            if str(task.get("task_key") or "") in keys
        }
        return cls(keys, transports if transports is not None else AppTransports(),
                   application_id, _engines(configured), declared.get("destinations") or {},
                   destinations)

    def task_keys(self) -> tuple[str, ...]:
        """The governed tasks this application may run, in declared order."""
        return self._task_keys

    def destination_of(self, task_key: str) -> str | None:
        """Where this application applies ``task_key``'s answer; None is presentation only.

        Its governed declaration, and nothing else: which task ran never grants
        anything by itself.
        """
        return self._task_destinations.get(str(task_key or ""))

    def permitted_types(self, task_key: str) -> tuple[str, ...]:
        """The artifact types ``task_key``'s answer may be, from its destination.

        Empty for a presentation-only task: every type rey_lib defines stays open.

        Raises:
            AIRequestError: When the task names a destination this application
                declares no media type for, or none an AI artifact can be --
                refused before any model runs.
        """
        destination = self.destination_of(task_key)
        if destination is None:
            return ()
        accepts = self._destinations.get(destination)
        if not accepts:
            raise AIRequestError(
                f"AI task {task_key!r} answers to {destination}, which this application "
                "declares no media type for.")
        kinds = permitted_artifact_types(accepts)
        if not kinds:
            raise AIRequestError(
                f"AI task {task_key!r} answers to {destination}, which accepts "
                f"{', '.join(sorted(accepts))}: no artifact an AI can produce.")
        return kinds

    def acceptance(self, destination: str, answer: Any) -> AIAcceptance:
        """Whether a completed task's artifact may be given to ``destination``.

        The answer is the artifact the AI produced and validated -- its media type
        the one its model declared. It is accepted when this application's
        governed declaration says ``destination`` accepts that media type; a
        destination declared nowhere accepts nothing. Its content is not judged
        here: empty may be what its contract asked for. Nothing is applied here --
        the owning application alone applies an accepted artifact -- and a refusal
        changes nothing about the task, which completed.
        """
        accepts = self._destinations.get(str(destination or ""))
        if not accepts:
            return AIAcceptance(False, reason=(
                f"{destination or '(no destination)'} accepts no AI artifact: this "
                "application declares no media type for it."))
        if not isinstance(answer, Mapping):
            return AIAcceptance(False, reason="The task's answer is no artifact.")
        media_type = str(answer.get("media_type") or "")
        if media_type not in accepts:
            return AIAcceptance(False, reason=(
                f"{media_type or 'an undeclared type'} is not accepted for {destination} "
                f"(accepts {', '.join(sorted(accepts))})."))
        text = answer.get("text")
        if not isinstance(text, str):
            value = answer.get("value")
            text = "" if value is None else json.dumps(value, default=str)
        return AIAcceptance(True, text=text)

    def engines(self) -> tuple[str, ...]:
        """The engines a session's AI may run on, by profile_key, the default first."""
        return self._engines

    def conversation(self, runtime: Any,
                     subject_reference: Mapping[str, Any]) -> dict[str, Any]:
        """The AI's conversation about this application's subject, as ai_app presents it.

        Read over this application's own ai_app (the one ``call`` uses, launched if
        it is not yet running): the conversation ai_app holds or restores for this
        application and subject. No task is opened and no model runs -- what an AI
        seat attached to this application shows before anything is asked.

        Raises:
            AIRequestError: When this application has no id to own a conversation by,
                no subject is named, or ai_app refuses.
            AIUnavailableError: When ai_app cannot be launched or does not attach.
        """
        if self.application_id is None:
            raise AIRequestError("This application's AI declaration carries no application "
                                 "id; it owns no AI conversation to read.")
        if not subject_reference:
            raise AIRequestError("An AI conversation is read for a named subject.")
        answered = self._ai_app(runtime).request({
            "command": "conversation", "object": self._ai_object,
            "application_id": self.application_id,
            "subject_reference": dict(subject_reference)})
        if "error" in answered:
            raise AIRequestError(str(answered["error"]))
        return dict(answered.get("conversation") or {})

    def call(self, runtime: Any, task_key: str, text: str, relay: Relay,
             context: Mapping[str, Any] | None = None,
             origin: Mapping[str, Any] | None = None, *,
             subject_reference: Mapping[str, Any] | None = None,
             coordinates: Mapping[str, Any] | None = None,
             engine: str | None = None) -> None:
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
        if key and key not in self._task_keys:
            raise AIRequestError(
                f"AI task {key!r} is not available to this application; available: "
                f"{', '.join(self._task_keys) or 'none'}."
            )
        if not key and not self._task_keys:
            raise AIRequestError("This application has no AI tasks available.")
        chosen = str(engine or "")
        if chosen and chosen not in self._engines:
            raise AIRequestError(
                f"AI engine {chosen!r} is not one of this installation's: "
                f"{', '.join(self._engines) or 'none'}."
            )
        if coordinates is not None and not subject_reference:
            raise AIRequestError("Hosted coordinates require a governed subject reference.")
        # WHAT EACH TASK'S ANSWER MAY BE, resolved before any model runs: the task
        # named, or every candidate select_task may choose. A refusal here stops
        # the call before ai_app is asked anything.
        permitted = {one: list(kinds) for one in ((key,) if key else self._task_keys)
                     if (kinds := self.permitted_types(one))}
        transport = self._ai_app(runtime)
        # A task named runs as named; with none, ai_app chooses among this
        # application's available tasks (#1341). The engine chosen for the session,
        # where one is, runs both the choice and the task.
        request: dict[str, Any] = (
            {"command": "task", "task_key": key, "object": self._ai_object} if key
            else {"command": "task", "candidates": list(self._task_keys),
                  "object": self._ai_object})
        if chosen:
            request["profile_key"] = chosen
        if permitted:
            # By task: ai_app constrains the task that runs, never select_task.
            request["artifact_types"] = permitted
        if self.application_id is not None:
            # Who is asking (#1338): the AI's conversation about the subject is the
            # one this application owns -- projected with the task, so any AI seat
            # attached to this application presents that conversation, the AI's own.
            request["application_id"] = self.application_id
            request["present_conversation"] = True
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

    def seat(self) -> dict[str, Any]:
        """This application's AI facts, for its projection (#1369).

        ``ai_tasks`` and ``ai_engines`` where it has tasks, and ``ai_disposition``
        where it decided about a completed task's output. Nothing where neither.
        """
        seat: dict[str, Any] = {}
        if self._task_keys:
            seat["ai_tasks"] = list(self._task_keys)
            # The engines the reader may choose to run this session's AI on, the
            # installation's default first (#1341).
            seat["ai_engines"] = list(self._engines)
        with self._guard:
            if self._disposition is not None:
                seat["ai_disposition"] = dict(self._disposition)
        return seat

    def answer(self, runtime: Any, command: Mapping[str, Any], emit: Relay,
               subject: ApplicationAISubject, publish: Callable[[], None],
               run_log: Any = None, coordinates: Mapping[str, Any] | None = None) -> None:
        """Answer one ``ai`` or ``ai_conversation`` command the application received.

        ``ai``: the task's input is the command's ``text``, else the subject's
        ``default_input``; beside it go the subject's ``context``, its
        ``subject_reference`` -- with ``coordinates``, the route back to it, where it
        names one -- and this run's ``origin``. The previous disposition is cleared
        first, so it is never read as this run's. Every message ai_app emits about
        the task goes to ``emit`` as ``{"ai": <the message>, "request_id"}``. When the
        task completes with an answer its governed declaration gives a destination,
        the answer is offered to the subject (``accept``) only if ``acceptance``
        accepts it, the application's decision is recorded as ``ai_disposition`` and
        ``publish`` is called. A task with no destination, or one that ends
        otherwise, records nothing: its status is the AITask's.

        ``ai_conversation``: the AI's conversation about the subject, read
        (``conversation``), as ``{"ai_conversation", "request_id"}``.

        A refusal is reported once and answered ``{"error", "command",
        "request_id"}``. Blocks only for the opening; call it off the thread reading
        commands.
        """
        name = str(command.get("command") or "")
        request_id = command.get("request_id")
        try:
            if name == "ai_conversation":
                reference = subject.subject_reference()
                if not reference:
                    raise AIRequestError("This application has no AI conversation to read.")
                emit({"ai_conversation": self.conversation(runtime, reference),
                      "request_id": request_id})
                return
            if name != "ai":
                raise AIRequestError(f"An application's AI answers ai and ai_conversation, "
                                     f"not {name!r}.")
            task_key = str(command.get("task_key") or "")
            text = str(command.get("text") or "") or subject.default_input(task_key)
            reference = subject.subject_reference()
            with self._guard:
                cleared, self._disposition = self._disposition is not None, None
            if cleared:
                publish()

            def relay(message: dict[str, Any]) -> None:
                emit({"ai": message, "request_id": request_id})
                self._disposed(message, task_key, subject, publish)

            # The engine the reader chose for the session, where one is; with no task
            # named, the application's AI chooses the task (#1341).
            self.call(runtime, task_key, text, relay, context=subject.context(),
                      origin=self.origin(run_log),
                      engine=str(command.get("engine") or "") or None,
                      **({"subject_reference": dict(reference)} if reference else {}),
                      **({"coordinates": dict(coordinates)}
                         if reference and coordinates is not None else {}))
        except AppError as refusal:
            # Answered to the application's launcher, so reported here (rule 79).
            refusal.report(f"ApplicationAI: {name}", source=__name__)
            emit({"error": str(refusal), "command": name, "request_id": request_id})

    def _disposed(self, message: Mapping[str, Any], task_key: str,
                  subject: ApplicationAISubject, publish: Callable[[], None]) -> None:
        """Record the application's decision about a completed task's output, if any.

        Only the task's own completed projection is acted on; the task that ran is
        the one it names (the one asked, or the one chosen, #1341). A task with no
        destination records nothing.
        """
        projection = message.get("projection")
        if not isinstance(projection, Mapping) or projection.get("status") != "completed":
            return
        ran = str(projection.get("task_key") or task_key)
        destination = self.destination_of(ran)
        if destination is None:
            return
        delivered = self.acceptance(destination, projection.get("answer"))
        if delivered.accepted:
            delivered = subject.accept(destination, delivered.text)
        with self._guard:
            self._disposition = {"task_key": ran, "run_id": projection.get("run_id"),
                                 "applied": delivered.accepted,
                                 "reason": None if delivered.accepted else delivered.reason}
        publish()

    @staticmethod
    def origin(run_log: Any) -> dict[str, Any] | None:
        """This run's place for the task's evidence (#1305), or None where it has none.

        A run at its top level has no record to parent the task's evidence to, and an
        origin naming none is refused before the task runs; then no origin is sent,
        and ai_app records the evidence under its own run (#1323). No parent is ever
        made up.
        """
        if run_log is None:
            return None
        place = run_log.parent_context()
        if place.get("run_id") in (None, "") or place.get("parent_run_log_id") in (None, ""):
            return None
        return place

    def run_to_end(self, runtime: Any, task_key: str, text: str, *,
                   context: Mapping[str, Any] | None = None,
                   origin: Mapping[str, Any] | None = None,
                   subject_reference: Mapping[str, Any] | None = None,
                   coordinates: Mapping[str, Any] | None = None,
                   wait_seconds: float | None = None,
                   observe: Relay | None = None,
                   alive: Callable[[], bool] | None = None) -> dict[str, Any] | None:
        """Run one task (``call``) and wait for it to end; how it ended.

        Returns the task's own ended projection, or ``{"status": "failed", "error"}``
        for a refusal ai_app answered. Each message also goes to ``observe``, where
        given. Returns None when no ending was heard: ``wait_seconds`` passed, or --
        where ``alive`` is given -- this AI's application or one ``alive`` answers for
        stopped running.

        Raises:
            AIRequestError: As ``call`` refuses.
            AIUnavailableError: When ai_app cannot be launched or does not attach.
        """
        ended = threading.Event()
        outcome: dict[str, Any] = {}

        def relay(message: dict[str, Any]) -> None:
            if "error" in message and "projection" not in message:
                outcome.update(status="failed", error=str(message.get("error") or ""))
                ended.set()
            else:
                projection = message.get("projection")
                if isinstance(projection, Mapping) and \
                        str(projection.get("status") or "") not in ("", "idle", "running"):
                    outcome.update(projection)
                    ended.set()
            if observe is not None:
                observe(message)

        self.call(runtime, task_key, text, relay, context=context, origin=origin,
                  **({"subject_reference": subject_reference}
                     if subject_reference is not None else {}),
                  **({"coordinates": coordinates} if coordinates is not None else {}))
        deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
        while not ended.is_set():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            poll = left if alive is None else min(_ALIVE_POLL_SECONDS, left or _ALIVE_POLL_SECONDS)
            if ended.wait(poll):
                break
            if alive is not None and (self.ended() or not alive()):
                return None
        return dict(outcome)

    def answer_requests(self, answer: Answerer) -> str:
        """Answer, as this application, what the ai_app it launches asks of its live object.

        The application holds its live object and launches ai_app through this
        ApplicationAI's own transports (#1325): a task's granted hosted tool reaches
        that object by routing to the ref answered here -- the ``connection`` of the
        coordinates the application passes to :meth:`call`. Each request is given to
        ``answer`` with its sender as routing establishes it. Nothing else is
        launched, attached or looked up.
        """
        return self._transports.answer_requests(answer)

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
            try:
                ref, transport = self._transports.application(
                    runtime, _AI_APPLICATION, _AI_OPERATION)
                transport.subscribe(self._pushed)
            except AppError as refused:
                raise AIUnavailableError(f"ai_app could not be launched: {refused}") from refused
            answer = transport.request({"command": "open", "subject": _AI_SUBJECT,
                                        "identity": {"kind": "ai"}})
            if answer.get("error") or not answer.get("object") or not isinstance(answer.get("projection"), dict):
                self._transports.close(ref)
                raise AIUnavailableError(str(answer.get("error") or "ai_app opened no AI."))
            self._ai_object = str(answer["object"])
            self._transport, self._ref = transport, ref
            return transport

    def _pushed(self, message: dict[str, Any]) -> None:
        """Relay only messages of a task this caller opened.

        The AI's own identity comes exclusively from its correlated open answer.
        Unrelated objects on the retained application's channel cannot select it.
        """
        if not isinstance(message, Mapping):
            return
        object_id = str(message.get("object") or "")
        with self._guard:
            relay = self._relays.get(object_id)
        if relay is not None:
            relay(dict(message))
