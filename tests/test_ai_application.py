"""An application's AI (#1301): its tasks resolved at load, and one call on ai_app.

The transports are stand-ins with the surface ApplicationAI uses -- launch, a
transport's request / send / connected / ended, close -- so these tests launch
nothing. They prove the object reads the two governed declarations once, calls
ai_app in the AI's own sequence over its own transports, and relays exactly what
ai_app says about the task, reading none of it.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from rey_lib.ai.application import AIAcceptance, ApplicationAI
from rey_lib.ai.errors import AIConfigurationError, AIRequestError, AIUnavailableError
from rey_lib.errors.error_utils import ConfigError


class _Control:
    """A stand-in Control: the application's declaration and the installation's tasks."""

    def __init__(self, declared: dict[str, Any] | None,
                 installation_tasks: list[dict[str, Any]]) -> None:
        self.declared = declared
        self.installation_tasks = installation_tasks
        self.asked: list[tuple[str, Any]] = []

    def get_application_installations(self, installation_id: int,
                                      application: str) -> list[dict[str, Any]]:
        self.asked.append(("declaration", (installation_id, application)))
        return [{"application": application, "application_id": 7, "ai": self.declared}]

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        self.asked.append(("configuration", installation))
        return list(self.installation_tasks)


def _installation(*keys: str, disabled: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [{"task_key": key, "is_enabled": key not in disabled} for key in keys]


def _declaration(*keys: str, is_enabled: bool = True,
                 disabled: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"is_enabled": is_enabled,
            "tasks": [{"task_key": key, "is_enabled": key not in disabled} for key in keys]}


class TestTheTasksAreResolvedAtLoad:
    def test_a_task_is_available_when_declared_and_enabled_in_the_installation(self) -> None:
        control = _Control(_declaration("comment_code", "generate_sql", "create_select"),
                           _installation("comment_code", "generate_sql", "autocomplete"))

        ai = ApplicationAI.at_load(control, "local", 3, "rey_db_admin")

        assert ai.task_keys() == ("comment_code", "generate_sql")
        assert control.asked == [("declaration", (3, "rey_db_admin")), ("configuration", "local")]

    def test_the_application_id_is_kept_from_its_declaration_row(self) -> None:
        """What an AI conversation the application owns is recorded under (#1338)."""
        control = _Control(_declaration("generate_sql"), _installation("generate_sql"))

        assert ApplicationAI.at_load(control, "local", 3, "rey_db_admin").application_id == 7

    def test_a_task_disabled_on_either_side_is_not_available(self) -> None:
        control = _Control(
            _declaration("comment_code", "generate_sql", disabled=("generate_sql",)),
            _installation("comment_code", "generate_sql", disabled=("comment_code",)))

        assert ApplicationAI.at_load(control, "local", 3, "rey_db_admin").task_keys() == ()

    def test_a_declaration_disabled_as_a_whole_or_absent_makes_none_available(self) -> None:
        installation = _installation("comment_code")

        disabled = _Control(_declaration("comment_code", is_enabled=False), installation)
        absent = _Control(None, installation)

        assert ApplicationAI.at_load(disabled, "local", 3, "rey_db_admin").task_keys() == ()
        assert ApplicationAI.at_load(absent, "local", 3, "rey_db_admin").task_keys() == ()
        # Nothing to intersect: the installation's configuration is not read.
        assert ("configuration", "local") not in disabled.asked

    def test_an_unresolved_installation_id_is_refused_not_guessed(self) -> None:
        control = _Control(_declaration("comment_code"), _installation("comment_code"))

        with pytest.raises(AIConfigurationError, match="installation's id"):
            ApplicationAI.at_load(control, "local", None, "rey_db_admin")
        assert control.asked == []


class _Transport:
    """A stand-in AppTransport to ai_app: what was sent, and what it answers."""

    def __init__(self, opened: dict[str, Any] | None = None, attaches: bool = True) -> None:
        self.opened = opened if opened is not None else {"opened": {"object": "t1"}}
        self.attaches = attaches
        self.sent: list[dict[str, Any]] = []
        self.requested: list[dict[str, Any]] = []
        self.has_ended = False

    def connected(self, timeout: float | None = None) -> bool:
        return self.attaches

    def ended(self) -> bool:
        return self.has_ended

    def request(self, command: dict[str, Any], timeout: float = 0.0) -> dict[str, Any]:
        self.requested.append(dict(command))
        return dict(self.opened, request_id="made-here")

    def send(self, command: dict[str, Any]) -> None:
        self.sent.append(dict(command))


class _Transports:
    """A stand-in AppTransports: records each launch and keeps the listener it was given."""

    def __init__(self, transport: _Transport | None = None,
                 refuses: Exception | None = None) -> None:
        self.transport = transport or _Transport()
        self.refuses = refuses
        self.launched: list[tuple[Any, str, str, dict[str, Any]]] = []
        self.listener: Callable[[dict[str, Any]], None] | None = None
        self.closed: list[str] = []
        self.closed_all = 0

    def launch(self, runtime: Any, application: str, operation: str, inputs: dict[str, Any],
               listener: Callable[[dict[str, Any]], None]) -> tuple[str, _Transport, Any]:
        if self.refuses is not None:
            raise self.refuses
        self.launched.append((runtime, application, operation, dict(inputs)))
        self.listener = listener
        if self.transport.attaches:
            # ai_app's first message: its live AI's opening (HostedObjects.serve).
            listener({"object": "ai", "projection": {"the": "AI itself"}})
        return f"ref{len(self.launched)}", self.transport, lambda: None

    def close(self, ref: str) -> None:
        self.closed.append(ref)

    def close_all(self) -> None:
        self.closed_all += 1


class TestACallOnAiApp:
    def test_the_call_launches_ai_app_once_and_sends_task_edit_run(self) -> None:
        transports = _Transports()
        ai = ApplicationAI(("comment_code", "generate_sql"), transports)
        runtime = object()
        relayed: list[dict[str, Any]] = []

        ai.call(runtime, "comment_code", "SELECT 1", relayed.append)
        ai.call(runtime, "generate_sql", "/* AI: count */", relayed.append)

        # One ai_app for this application, launched through its own transports.
        assert transports.launched == [(runtime, "ai_app", "serve", {"subject": "ai.settings"})]
        # The task command names the live AI's object, as its opening announced it.
        assert transports.transport.requested == [
            {"command": "task", "task_key": "comment_code", "object": "ai"},
            {"command": "task", "task_key": "generate_sql", "object": "ai"},
        ]
        assert transports.transport.sent == [
            {"command": "edit", "object": "t1", "changes": {"prompt": "SELECT 1"}},
            {"command": "run", "object": "t1"},
            {"command": "edit", "object": "t1", "changes": {"prompt": "/* AI: count */"}},
            {"command": "run", "object": "t1"},
        ]
        # The opening's answer is the first thing relayed, as ai_app answered it.
        assert relayed[0] == {"opened": {"object": "t1"}, "request_id": "made-here"}

    def test_a_context_the_application_supplies_travels_with_the_prompt(self) -> None:
        """#1301: what the live object already holds goes beside the prompt, in the
        same edit, as given; no lookup and no second transport."""
        transports = _Transports()
        ai = ApplicationAI(("generate_sql",), transports)
        held = {"identity": {"schema": "control", "name": "batch"},
                "columns": [{"name": "batch_id", "type": "bigint", "nullable": False}]}

        ai.call(object(), "generate_sql", "how many batches are there", lambda m: None,
                context=held)

        assert transports.transport.sent[0] == {
            "command": "edit", "object": "t1",
            "changes": {"prompt": "how many batches are there", "context": held},
        }
        assert transports.transport.sent[1] == {"command": "run", "object": "t1"}

    def test_the_callers_place_travels_as_the_tasks_origin(self) -> None:
        """#1305: the origin -- the caller's run, record, nesting, correlation -- goes
        in the task request as origin_id, text, so AI records under it."""
        import json

        transports = _Transports()
        ai = ApplicationAI(("log_interpretation",), transports)
        origin = {"run_id": 3981, "parent_run_log_id": 2849915, "nest_level": 4,
                  "correlation_id": "c1", "app": "workflow_app"}

        ai.call(object(), "log_interpretation", "{}", lambda m: None, origin=origin)

        opened = transports.transport.requested[-1]
        assert opened["command"] == "task"
        assert json.loads(opened["origin_id"]) == origin
        # Without one, the request names no origin at all.
        ai.call(object(), "log_interpretation", "{}", lambda m: None)
        assert "origin_id" not in transports.transport.requested[-1]

    def test_what_ai_app_says_about_the_task_is_relayed_unread(self) -> None:
        transports = _Transports()
        ai = ApplicationAI(("comment_code",), transports)
        relayed: list[dict[str, Any]] = []
        ai.call(object(), "comment_code", "SELECT 1", relayed.append)
        assert transports.listener is not None

        transports.listener({"event": {"kind": "started"}, "object": "t1"})
        transports.listener({"stdout": "ai_app log line"})
        transports.listener({"object": "ai", "projection": {"the": "AI again"}})
        transports.listener({"event": {"kind": "other task"}, "object": "t2"})
        transports.listener({"projection": {"lifecycle": "ended"}, "object": "t1"})
        transports.listener({"error": "refused", "command": "run", "object": "t1"})

        assert relayed[1:] == [
            {"event": {"kind": "started"}, "object": "t1"},
            {"projection": {"lifecycle": "ended"}, "object": "t1"},
            {"error": "refused", "command": "run", "object": "t1"},
        ]

    def test_a_task_not_available_to_this_application_is_refused_before_anything(self) -> None:
        transports = _Transports()
        ai = ApplicationAI(("comment_code",), transports)

        with pytest.raises(AIRequestError, match="not available to this application"):
            ai.call(object(), "create_select", "x", lambda message: None)
        assert transports.launched == []

    def test_ai_apps_refusal_to_open_the_task_is_the_calls_refusal(self) -> None:
        transports = _Transports(
            _Transport(opened={"error": "AI task 'comment_code' is disabled."}))
        ai = ApplicationAI(("comment_code",), transports)

        with pytest.raises(AIRequestError, match="is disabled"):
            ai.call(object(), "comment_code", "x", lambda message: None)
        assert transports.transport.sent == []

    def test_an_ai_app_that_cannot_be_launched_or_does_not_attach_is_unavailable(self) -> None:
        refusing = _Transports(refuses=ConfigError("application 'ai_app' is not configured."))
        with pytest.raises(AIUnavailableError, match="not configured"):
            ApplicationAI(("comment_code",), refusing).call(object(), "comment_code", "x",
                                                             lambda message: None)

        detached = _Transports(_Transport(attaches=False))
        with pytest.raises(AIUnavailableError, match="did not attach"):
            ApplicationAI(("comment_code",), detached).call(object(), "comment_code", "x",
                                                             lambda message: None)
        assert detached.closed == ["ref1"]

    def test_an_ended_ai_app_is_launched_again_and_close_ends_them_all(self) -> None:
        transports = _Transports()
        ai = ApplicationAI(("comment_code",), transports)
        ai.call(object(), "comment_code", "x", lambda message: None)
        transports.transport.has_ended = True

        ai.call(object(), "comment_code", "y", lambda message: None)
        assert len(transports.launched) == 2

        ai.close()
        assert transports.closed_all == 1


def test_hosted_subject_and_coordinates_share_the_existing_call_without_persistence():
    transports = _Transports()
    ai = ApplicationAI(('code_investigation',), transports)
    subject = {'kind':'repository_estate','installation':'local'}
    coordinates = {'connection':'existing-estate','object':'held-estate'}
    ai.call(object(),'code_investigation','Trace across applications',lambda msg:None,
            subject_reference=subject,coordinates=coordinates)
    assert transports.transport.requested[0]['subject_reference'] == subject
    assert 'coordinates' not in transports.transport.requested[0]
    assert transports.transport.sent[-1] == {'command':'run','object':'t1','coordinates':coordinates}
    assert 'coordinates' not in transports.transport.sent[0]['changes']
    with pytest.raises(AIRequestError):
        ai.call(object(),'code_investigation','Question',lambda msg:None,coordinates=coordinates)


def test_the_application_answers_what_its_ai_app_asks_through_its_own_transports() -> None:
    """#1325: the launching application's answer is held by the transports it launched
    ai_app through; the ref is the connection its coordinates name."""
    class _Answering(_Transports):
        def __init__(self) -> None:
            super().__init__()
            self.answerers: list[Any] = []

        def answer_requests(self, answer: Any) -> str:
            self.answerers.append(answer)
            return "holder-ref"

    def answer(request: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
        return {"result": None}

    transports = _Answering()
    ai = ApplicationAI(("build_workflow",), transports)

    assert ai.answer_requests(answer) == "holder-ref"
    assert transports.answerers == [answer]
    assert transports.launched == []


# -- the reader chooses the engine; ai_app chooses the task (#1341) -------------------

def _with_engines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Installation rows as ai.ai_installation_vw carries them: its engines on each."""
    engines = [{"profile_key": "qwen3-coder-next:latest", "is_enabled": True},
               {"profile_key": "claude_subscription", "is_enabled": True},
               {"profile_key": "local_reasoning", "is_enabled": False},
               {"profile_key": "local_precision", "is_enabled": True}]
    return [{**row, "engines": engines, "default_profile_key": "qwen3-coder-next:latest"}
            for row in rows]


def test_the_installations_enabled_engines_are_offered_default_first() -> None:
    control = _Control(_declaration("comment_code"), _with_engines(_installation("comment_code")))

    ai = ApplicationAI.at_load(control, "local", 3, "rey_db_admin")

    assert ai.engines() == ("qwen3-coder-next:latest", "claude_subscription", "local_precision")


def test_with_no_task_named_ai_app_is_given_the_applications_tasks_to_choose_from() -> None:
    transports = _Transports()
    ai = ApplicationAI(("comment_code", "generate_sql"), transports,
                       engines=("qwen3-coder-next:latest", "claude_subscription"))

    ai.call(object(), "", "add comments", lambda message: None, engine="claude_subscription")
    ai.call(object(), "", "add comments", lambda message: None)

    assert transports.transport.requested == [
        {"command": "task", "candidates": ["comment_code", "generate_sql"], "object": "ai",
         "profile_key": "claude_subscription"},
        {"command": "task", "candidates": ["comment_code", "generate_sql"], "object": "ai"},
    ]


def test_the_application_and_its_subject_travel_with_the_task_without_coordinates() -> None:
    """#1338: the AI's conversation is the asking application's, about its subject."""
    transports = _Transports()
    ai = ApplicationAI(("generate_sql",), transports, application_id=7)

    ai.call(object(), "generate_sql", "count", lambda message: None,
            subject_reference={"kind": "workflow", "workflow_id": 8})

    # And the conversation projected with the task, so an attached seat presents it.
    assert transports.transport.requested == [
        {"command": "task", "task_key": "generate_sql", "object": "ai",
         "application_id": 7, "present_conversation": True,
         "subject_reference": {"kind": "workflow", "workflow_id": 8}}]
    assert transports.transport.sent[-1] == {"command": "run", "object": "t1"}


def test_a_named_task_runs_on_the_engine_chosen_for_the_session() -> None:
    transports = _Transports()
    ai = ApplicationAI(("generate_sql",), transports, engines=("local_precision",))

    ai.call(object(), "generate_sql", "count", lambda message: None, engine="local_precision")

    assert transports.transport.requested == [
        {"command": "task", "task_key": "generate_sql", "object": "ai",
         "profile_key": "local_precision"}]


@pytest.mark.parametrize("task_keys, engine, refusal", [
    (("generate_sql",), "gpt-anything", "is not one of this installation's"),
    ((), None, "has no AI tasks available"),
], ids=["unknown-engine", "no-tasks"])
def test_a_choice_outside_the_installation_is_refused_before_ai_app_launches(
        task_keys: tuple[str, ...], engine: Any, refusal: str) -> None:
    transports = _Transports()
    ai = ApplicationAI(task_keys, transports, engines=("local_precision",))

    with pytest.raises(AIRequestError, match=refusal):
        ai.call(object(), "", "count", lambda message: None, engine=engine)
    assert transports.launched == []



# -- where each task's answer goes, and what it may be (AI artifact envelope) ----------

def _destined() -> ApplicationAI:
    """A Loader's AI as its governed declaration states it: two SQL tasks applied to
    the Source's statement, a review shown, and a task naming an undeclared place."""
    declaration = {"is_enabled": True,
                   "destinations": {"source.statement": ["text/x-sql"],
                                    "source.picture": ["image/png"]},
                   "tasks": [
                       {"task_key": "comment_code", "destination_key": "source.statement"},
                       {"task_key": "generate_sql", "destination_key": "source.statement"},
                       {"task_key": "review_load", "destination_key": None},
                       {"task_key": "draw_it", "destination_key": "source.picture"},
                       {"task_key": "misplaced", "destination_key": "nowhere"}]}
    control = _Control(declaration, _installation(
        "comment_code", "generate_sql", "review_load", "draw_it", "misplaced"))
    ai = ApplicationAI.at_load(control, "local", 3, "data_engine")
    ai._transports = _Transports()  # noqa: SLF001
    return ai


class TestDestinations:
    def test_each_tasks_destination_is_its_declarations(self) -> None:
        ai = _destined()
        assert (ai.destination_of("comment_code"), ai.destination_of("review_load"),
                ai.destination_of("unknown")) == ("source.statement", None, None)

    def test_a_destined_task_may_answer_only_what_its_destination_accepts(self) -> None:
        ai = _destined()
        assert ai.permitted_types("comment_code") == ("sql",)
        assert ai.permitted_types("review_load") == ()

    @pytest.mark.parametrize("task_key, refusal", [
        ("misplaced", "declares no media type"),
        ("draw_it", "no artifact an AI can produce"),
    ], ids=["undeclared-destination", "no-producible-type"])
    def test_an_impossible_destination_is_refused_before_ai_app_is_asked(
            self, task_key: str, refusal: str) -> None:
        ai = _destined()
        with pytest.raises(AIRequestError, match=refusal):
            ai.call(object(), task_key, "go", lambda message: None)
        assert ai._transports.launched == []  # noqa: SLF001

    def test_a_named_task_carries_its_permitted_types(self) -> None:
        ai = _destined()
        ai.call(object(), "comment_code", "comment it", lambda message: None)
        assert ai._transports.transport.requested[0]["artifact_types"] == {  # noqa: SLF001
            "comment_code": ["sql"]}

    def test_a_presentation_only_task_carries_none(self) -> None:
        ai = _destined()
        ai.call(object(), "review_load", "review it", lambda message: None)
        assert "artifact_types" not in ai._transports.transport.requested[0]  # noqa: SLF001


class TestAcceptance:
    """Delivery, never execution: the answer stays the completed task's either way."""

    def test_an_artifact_of_an_accepted_type_is_accepted_with_its_text(self) -> None:
        accepted = _destined().acceptance(
            "source.statement", {"text": "SELECT 1", "media_type": "text/x-sql"})
        assert (accepted.accepted, accepted.text) == (True, "SELECT 1")

    def test_empty_content_of_an_accepted_type_is_still_accepted(self) -> None:
        accepted = _destined().acceptance(
            "source.statement", {"text": "", "media_type": "text/x-sql"})
        assert (accepted.accepted, accepted.text) == (True, "")

    def test_markdown_is_refused_for_the_statement_saying_why(self) -> None:
        refused = _destined().acceptance(
            "source.statement", {"text": "# Notes", "media_type": "text/markdown"})
        assert refused.accepted is False
        assert refused.reason == (
            "text/markdown is not accepted for source.statement (accepts text/x-sql).")

    @pytest.mark.parametrize("destination", ["nowhere", ""])
    def test_an_undeclared_destination_always_refuses(self, destination: str) -> None:
        refused = _destined().acceptance(
            destination, {"text": "SELECT 1", "media_type": "text/x-sql"})
        assert refused.accepted is False and "accepts no AI artifact" in refused.reason

    def test_no_artifact_is_refused(self) -> None:
        assert _destined().acceptance("source.statement", None).accepted is False


# -- the AI's conversation about this application's subject, read (#1343 step 4) ----

def test_the_conversation_is_read_from_this_applications_ai_app_and_no_task_opens() -> None:
    talk = {"conversation_id": 8, "messages": [{"role": "user", "content": "add comments"}]}
    transports = _Transports(_Transport(opened={"conversation": talk}))
    ai = ApplicationAI(("comment_code",), transports, application_id=15)
    subject = {"kind": "file_manifest", "file_manifest_id": 1160}

    assert ai.conversation(object(), subject) == talk
    assert transports.transport.requested == [
        {"command": "conversation", "object": "ai", "application_id": 15,
         "subject_reference": subject}]
    assert transports.transport.sent == []      # no task opened, edited or run


@pytest.mark.parametrize("application_id, subject, refusal", [
    (None, {"kind": "workflow", "workflow_id": 8}, "no application id"),
    (15, {}, "named subject"),
], ids=["no-application-id", "no-subject"])
def test_a_conversation_is_read_only_for_an_owning_application_and_a_subject(
        application_id: Any, subject: Any, refusal: str) -> None:
    transports = _Transports()
    ai = ApplicationAI(("comment_code",), transports, application_id=application_id)

    with pytest.raises(AIRequestError, match=refusal):
        ai.conversation(object(), subject)
    assert transports.launched == []


# -- the one AI lifecycle every application uses (#1369) -----------------------------

#: What the subject's live object holds, as its context.
_HELD = {"definition": "the object's own"}


class _Lifecycle(ApplicationAI):
    """ApplicationAI with only ai_app's side standing in: what each call was given,
    and the messages ai_app would emit about the task. ``answer``, ``seat``,
    ``origin`` and ``run_to_end`` are the real ones."""

    def __init__(self, ends: str = "completed", answer: Any = None,
                 refuses: Exception | None = None, chose: str = "",
                 tasks: tuple[str, ...] = ("comment_code", "analyze_data")) -> None:
        super().__init__(tasks, None,  # type: ignore[arg-type]
                         engines=("local", "remote"),
                         destinations={"query.statement": ["text/x-sql"]},
                         task_destinations={"comment_code": "query.statement"})
        self.ends = ends
        self.answer_given = answer if answer is not None else {
            "text": "the commented statement", "media_type": "text/x-sql"}
        self.refuses = refuses
        self.chose = chose
        self.calls: list[dict[str, Any]] = []
        self.silent = False

    def call(self, runtime: Any, task_key: str, text: str, relay: Any,
             context: Any = None, origin: Any = None, *, subject_reference: Any = None,
             coordinates: Any = None, engine: Any = None) -> None:
        self.calls.append({"task_key": task_key, "text": text, "context": context,
                           "origin": origin, "subject_reference": subject_reference,
                           "coordinates": coordinates, "engine": engine})
        if self.refuses is not None:
            raise self.refuses
        if self.silent:
            return
        relay({"opened": {"object": "t1"}})
        relay({"projection": {"status": "running"}, "object": "t1"})
        relay({"projection": {"status": self.ends, "run_id": 42, "error": "",
                              "answer": self.answer_given if self.ends == "completed" else None,
                              **({"task_key": self.chose} if self.chose else {})},
               "object": "t1"})

    def conversation(self, runtime: Any, subject_reference: Any) -> dict[str, Any]:
        return {"subject": dict(subject_reference)}


class _Subject:
    """An application's AI subject: what it supplies, and what it was asked to accept."""

    def __init__(self, reference: Any = None, refuses_input: bool = False,
                 applies: bool = True) -> None:
        self.reference = reference if reference is not None else {"kind": "views", "name": "v"}
        self.refuses_input = refuses_input
        self.applies = applies
        self.accepted: list[tuple[str, str]] = []

    def subject_reference(self) -> Any:
        return self.reference

    def context(self) -> dict[str, Any]:
        return dict(_HELD)

    def default_input(self, task_key: str) -> str:
        if self.refuses_input:
            raise ConfigError("Enter what you want.")
        return f"the statement, for {task_key}"

    def accept(self, destination: str, text: str) -> AIAcceptance:
        self.accepted.append((destination, text))
        return AIAcceptance(True, text=text) if self.applies else AIAcceptance(
            False, reason="not here")


class _Place:
    def __init__(self, parent: Any = 9) -> None:
        self.parent = parent

    def parent_context(self) -> dict[str, Any]:
        return {"run_id": 1, "parent_run_log_id": self.parent}


@pytest.fixture
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConfigError, "report", lambda self, *_a, **_k: None)
    monkeypatch.setattr(AIRequestError, "report", lambda self, *_a, **_k: None)


def _asked(ai: _Lifecycle, command: dict[str, Any], subject: _Subject | None = None,
           run_log: Any = None, coordinates: Any = None) -> tuple[list[dict[str, Any]], int]:
    """``answer`` once: what it emitted, and how many times it published."""
    emitted: list[dict[str, Any]] = []
    published = [0]

    def publish() -> None:
        published[0] += 1

    ai.answer(object(), {"request_id": "r1", **command}, emitted.append,
              subject or _Subject(), publish, run_log, coordinates)
    return emitted, published[0]


def test_the_seat_carries_tasks_and_engines_and_no_disposition_before_any_run() -> None:
    assert _Lifecycle().seat() == {"ai_tasks": ["comment_code", "analyze_data"],
                                   "ai_engines": ["local", "remote"]}
    assert ApplicationAI((), None).seat() == {}  # type: ignore[arg-type]


def test_an_accepted_answer_is_offered_to_the_subject_and_recorded_applied() -> None:
    ai, subject = _Lifecycle(), _Subject()
    emitted, published = _asked(ai, {"command": "ai", "task_key": "comment_code",
                                     "text": "add comments", "engine": "remote"}, subject,
                                _Place(), {"connection": "holder"})

    # Every message relayed as ai_app emitted it, under the asking request.
    assert [one["request_id"] for one in emitted] == ["r1"] * 3
    assert emitted[-1]["ai"]["projection"]["status"] == "completed"
    assert ai.calls == [{"task_key": "comment_code", "text": "add comments",
                         "context": _HELD,
                         "origin": {"run_id": 1, "parent_run_log_id": 9},
                         "subject_reference": {"kind": "views", "name": "v"},
                         "coordinates": {"connection": "holder"}, "engine": "remote"}]
    assert subject.accepted == [("query.statement", "the commented statement")]
    assert ai.seat()["ai_disposition"] == {"task_key": "comment_code", "run_id": 42,
                                           "applied": True, "reason": None}
    assert published == 1


def test_without_text_the_subjects_default_input_is_the_tasks() -> None:
    ai = _Lifecycle()
    _asked(ai, {"command": "ai", "task_key": "comment_code"})

    assert ai.calls[0]["text"] == "the statement, for comment_code"


def test_an_answer_the_declaration_refuses_is_never_offered_and_is_declined() -> None:
    ai = _Lifecycle(answer={"text": "# notes", "media_type": "text/markdown"})
    subject = _Subject()
    _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "x"}, subject)

    assert subject.accepted == []
    assert ai.seat()["ai_disposition"] == {
        "task_key": "comment_code", "run_id": 42, "applied": False,
        "reason": "text/markdown is not accepted for query.statement (accepts text/x-sql)."}


def test_the_subject_may_decline_an_accepted_answer_with_its_reason() -> None:
    ai = _Lifecycle()
    _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "x"},
           _Subject(applies=False))

    assert ai.seat()["ai_disposition"]["applied"] is False
    assert ai.seat()["ai_disposition"]["reason"] == "not here"


@pytest.mark.parametrize("ends", ["failed", "cancelled"])
def test_a_task_that_does_not_complete_records_nothing(ends: str) -> None:
    ai, subject = _Lifecycle(ends=ends), _Subject()
    _, published = _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "x"},
                          subject)

    assert subject.accepted == [] and "ai_disposition" not in ai.seat() and published == 0


def test_a_task_with_no_destination_records_nothing() -> None:
    ai, subject = _Lifecycle(), _Subject()
    _asked(ai, {"command": "ai", "task_key": "analyze_data", "text": "how many"}, subject)

    assert subject.accepted == [] and "ai_disposition" not in ai.seat()


def test_the_task_the_ai_chose_is_the_one_disposed_of() -> None:
    ai = _Lifecycle(chose="comment_code")
    _asked(ai, {"command": "ai", "text": "x"})

    assert ai.calls[0]["task_key"] == ""
    assert ai.seat()["ai_disposition"]["task_key"] == "comment_code"


def test_a_new_run_clears_the_previous_disposition_first() -> None:
    ai = _Lifecycle()
    _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "x"})
    ai.ends = "failed"

    _, published = _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "y"})

    # Cleared and published before the run, so it is never read as this run's.
    assert "ai_disposition" not in ai.seat() and published == 1


def test_a_refusal_is_answered_on_its_request_and_nothing_runs(_quiet: None) -> None:
    ai = _Lifecycle()
    emitted, published = _asked(ai, {"command": "ai", "task_key": "comment_code"},
                                _Subject(refuses_input=True))

    assert emitted == [{"error": "Enter what you want.", "command": "ai", "request_id": "r1"}]
    assert ai.calls == [] and published == 0


def test_a_refused_call_is_answered_on_its_request(_quiet: None) -> None:
    ai = _Lifecycle(refuses=AIRequestError("AI task 'x' is not available."))
    emitted, _ = _asked(ai, {"command": "ai", "task_key": "x", "text": "y"})

    assert emitted == [{"error": "AI task 'x' is not available.", "command": "ai",
                        "request_id": "r1"}]


def test_a_subject_with_no_reference_is_run_without_one_or_a_route_back() -> None:
    ai = _Lifecycle()
    _asked(ai, {"command": "ai", "task_key": "comment_code", "text": "x"},
           _Subject(reference={}), coordinates={"connection": "holder"})

    assert ai.calls[0]["subject_reference"] is None and ai.calls[0]["coordinates"] is None


def test_the_conversation_is_read_for_the_subject(_quiet: None) -> None:
    ai = _Lifecycle()
    emitted, _ = _asked(ai, {"command": "ai_conversation"})
    assert emitted == [{"ai_conversation": {"subject": {"kind": "views", "name": "v"}},
                        "request_id": "r1"}]

    refused, _ = _asked(ai, {"command": "ai_conversation"}, _Subject(reference={}))
    assert refused[0]["command"] == "ai_conversation"
    assert "no AI conversation" in refused[0]["error"]


@pytest.mark.parametrize("parent, origin", [
    ("none", None),
    (9, {"run_id": 1, "parent_run_log_id": 9}),
    (None, None),
], ids=["no-run", "nested", "top-level"])
def test_the_origin_is_the_runs_place_and_never_a_made_up_parent(parent: Any,
                                                                 origin: Any) -> None:
    run_log = None if parent == "none" else _Place(parent)
    assert ApplicationAI.origin(run_log) == origin


def test_running_to_the_end_returns_the_ended_projection_and_observes_each_message() -> None:
    ai, seen = _Lifecycle(), []
    ended = ai.run_to_end(object(), "comment_code", "x", observe=seen.append)

    assert ended is not None and ended["status"] == "completed" and ended["run_id"] == 42
    assert len(seen) == 3


def test_running_to_the_end_answers_a_refusal_as_failed() -> None:
    class _Refused(_Lifecycle):
        def call(self, runtime: Any, task_key: str, text: str, relay: Any,
                 context: Any = None, origin: Any = None, **_: Any) -> None:
            relay({"error": "the engine refused"})

    assert _Refused().run_to_end(object(), "comment_code", "x") == {
        "status": "failed", "error": "the engine refused"}


def test_running_to_the_end_gives_up_when_the_wait_passes_or_an_application_stops() -> None:
    ai = _Lifecycle()
    ai.silent = True

    assert ai.run_to_end(object(), "comment_code", "x", wait_seconds=0.05) is None
    assert ai.run_to_end(object(), "comment_code", "x", alive=lambda: False) is None
