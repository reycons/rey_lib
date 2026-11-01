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

from rey_lib.ai.application import ApplicationAI
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
        return [{"application": application, "ai": self.declared}]

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
