"""The application channel's execution and communication (backlog console.workflow_map, 1042).

A real child process is launched through ``LaunchedApp.launch``; it attaches with
``AttachedApp.attach`` and its own Application Run id. Commands go in, events and
stdout come out, over a localhost channel opened with a one-time token -- AnyIO
underneath, reached only through ``rey_lib.run.async_runtime``.
"""

from __future__ import annotations

import sys
import textwrap
import time

import pytest

from rey_lib.errors.error_utils import StateError
from rey_lib.run import app_channel
from rey_lib.run.app_channel import CONNECTED, EXITED, STOPPED, AttachedApp, LaunchedApp

#: How long any one wait in these tests may take.
WAIT = 20.0

#: A child application: it owns run 77, attaches with its id, and answers what it is sent.
CHILD = textwrap.dedent("""
    import sys
    from rey_lib.run import AppExecute
    from rey_lib.run.app_channel import AttachedApp

    application_run = AppExecute(run_id=77, control=None, subject_type="app",
                                 subject_id="child")
    channel = AttachedApp.attach(application_run.run_id)
    print("child started", flush=True)
    channel.emit({"event": "ready"})
    for command in channel.commands():
        if command.get("command") == "echo":
            channel.emit({"event": "echoed", "value": command.get("value")})
        if command.get("command") in ("finish", "close"):
            break
    print("child finishing", flush=True)
    channel.close()
    sys.exit(3)
""")

#: A child that never finishes on its own.
SLEEPER = textwrap.dedent("""
    import time
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(5)
    channel.emit({"event": "ready"})
    time.sleep(600)
""")


def _launch(code: str):
    return LaunchedApp.launch([sys.executable, "-c", code])


def test_commands_go_in_and_events_and_stdout_come_out() -> None:
    app = _launch(CHILD)
    try:
        assert app.connected(timeout=WAIT)
        # The launcher learns the application's own Application Run id from its hello.
        assert (app.run_id, app.state) == (77, CONNECTED)

        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "echo", "value": "hello"})
        assert next(events) == {"event": "echoed", "value": "hello"}
        app.send({"command": "finish"})

        assert app.wait(timeout=WAIT) == 3
        assert app.state == EXITED
        assert "".join(app.output(timeout=WAIT)) == "child started\nchild finishing\n"
    finally:
        # Shown with a failure: how far the launch got, and when.
        print("trace:", app.trace)
        app.close()


def test_the_applications_stderr_arrives_as_output_too() -> None:
    # An application's log lines are on stderr; the launcher takes them with stdout,
    # as the Console's existing launcher does.
    app = _launch("import sys\nprint('to stdout', flush=True)\n"
                  "print('a log line', file=sys.stderr, flush=True)\n")
    try:
        assert app.wait(timeout=WAIT) == 0
        text = "".join(app.output(timeout=WAIT))
        assert "to stdout" in text and "a log line" in text
    finally:
        print("trace:", app.trace)
        app.close()


def test_stop_ends_an_application_that_is_still_running() -> None:
    app = _launch(SLEEPER)
    try:
        assert app.connected(timeout=WAIT)
        assert next(app.events(timeout=WAIT)) == {"event": "ready"}

        app.stop()

        assert app.wait(timeout=WAIT) is not None
        assert app.state == STOPPED
    finally:
        # Shown with a failure: how far the launch got, and when.
        print("trace:", app.trace)
        app.close()


def _sent(app) -> int:
    """How many commands the launcher sent, by its own trace."""
    return sum(1 for _, point in app.trace if point == "command sent")


def test_close_asks_a_running_application_to_close_and_it_ends_on_its_own() -> None:
    app = _launch(CHILD)
    assert app.connected(timeout=WAIT)
    assert next(app.events(timeout=WAIT)) == {"event": "ready"}

    app.close()

    # Asked once; it ended itself within the grace, so it was never stopped.
    assert _sent(app) == 1
    assert (app.exit_code, app.state) == (3, EXITED)
    # What it wrote before and while closing still arrives.
    assert "".join(app.output(timeout=WAIT)) == "child started\nchild finishing\n"


def test_close_does_not_ask_an_application_that_has_already_ended() -> None:
    app = _launch(CHILD)
    assert app.connected(timeout=WAIT)
    app.send({"command": "finish"})
    assert app.wait(timeout=WAIT) == 3

    app.close()

    assert _sent(app) == 1 and app.state == EXITED


def test_close_stops_an_application_that_never_attached() -> None:
    app = _launch("import time\ntime.sleep(600)\n")

    app.close()

    assert _sent(app) == 0
    assert app.run_id is None and app.state == STOPPED


def test_close_stops_an_application_that_ignores_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_channel, "_CLOSE_GRACE_SECONDS", 0.5)
    app = _launch(SLEEPER)
    assert app.connected(timeout=WAIT)

    app.close()

    # Asked, given its grace, then stopped.
    assert _sent(app) == 1
    assert app.state == STOPPED and app.exit_code is not None


def test_an_attachment_with_the_wrong_token_is_refused() -> None:
    # The child is handed a token that is not the launcher's.
    app = _launch(CHILD.replace(
        "from rey_lib.run import AppExecute",
        "import os\nos.environ['REY_APP_EXECUTE_TOKEN'] = 'wrong'\n"
        "from rey_lib.run import AppExecute",
    ))
    try:
        assert app.connected(timeout=WAIT) is False
        assert app.run_id is None
        # Refused, the child sees its channel close and ends on its own.
        assert app.wait(timeout=WAIT) == 3
    finally:
        # Shown with a failure: how far the launch got, and when.
        print("trace:", app.trace)
        app.close()


def test_attaching_outside_a_launch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REY_APP_EXECUTE_PORT", raising=False)
    monkeypatch.delenv("REY_APP_EXECUTE_TOKEN", raising=False)

    with pytest.raises(StateError, match="not launched through LaunchedApp.launch"):
        AttachedApp.attach(1)


#: A child that emits from several threads at once, then ends.
EMITTERS = textwrap.dedent("""
    import threading
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(9)
    def emit_from(thread):
        for index in range(50):
            channel.emit({"thread": thread, "index": index, "padding": "x" * 4096})
    threads = [threading.Thread(target=emit_from, args=(one,)) for one in range(4)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()
    channel.emit({"event": "done"})
    channel.close()
""")


def test_messages_emitted_from_several_threads_at_once_each_arrive_whole() -> None:
    app = _launch(EMITTERS)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        received = []
        while (message := next(events)) != {"event": "done"}:
            received.append((message["thread"], message["index"]))
        assert sorted(received) == [(thread, index) for thread in range(4) for index in range(50)]
        assert app.wait(timeout=WAIT) == 0
    finally:
        # Shown with a failure: how far the launch got, and when.
        print("trace:", app.trace)
        app.close()


#: A child that emits values JSON cannot carry as they are.
VALUES = textwrap.dedent("""
    from datetime import date, datetime
    from decimal import Decimal
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(8)
    channel.emit({"event": "values", "rows": [
        {"d": date(2026, 1, 2), "t": datetime(2026, 1, 2, 3, 4, 5), "n": Decimal("1.50")},
    ], "nested": {"k": (Decimal("2"), None)}})
    channel.close()
""")


def test_an_emitted_message_arrives_json_safe_and_otherwise_as_sent() -> None:
    app = _launch(VALUES)
    try:
        assert app.connected(timeout=WAIT)
        assert next(app.events(timeout=WAIT)) == {
            "event": "values",
            "rows": [{"d": "2026-01-02", "t": "2026-01-02T03:04:05", "n": "1.50"}],
            "nested": {"k": ["2", None]},
        }
        assert app.wait(timeout=WAIT) == 0
    finally:
        print("trace:", app.trace)
        app.close()


# -- another attachment to the same launcher (#1099) ----------------------------

def _launch_into(attachment, code: str = CHILD, *args: str):
    return LaunchedApp.launch([sys.executable, "-c", code, *args], attachment=attachment)


def _shared_output_says(capfd: pytest.CaptureFixture[str], text: str) -> str:
    """What the shared stdout/stderr carried, once it carries ``text`` (or WAIT has passed)."""
    said = ""
    deadline = time.monotonic() + WAIT
    while text not in said and time.monotonic() < deadline:
        out, err = capfd.readouterr()
        said += out + err
        time.sleep(0.05)
    return said


def test_an_application_launched_into_an_accepting_attachment_attaches_there(
    capfd: pytest.CaptureFixture[str],
) -> None:
    accepted = LaunchedApp.accepting()
    launched = _launch_into(accepted.attachment)
    try:
        # Launched into another's attachment, the launch opens none of its own.
        assert launched.attachment is None
        assert accepted.connected(timeout=WAIT)
        assert (accepted.run_id, accepted.state) == (77, CONNECTED)

        events = accepted.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        accepted.send({"command": "echo", "value": "hello"})
        assert next(events) == {"event": "echoed", "value": "hello"}
        accepted.send({"command": "finish"})

        # The process is the launch's; the channel, and its end, the accepting handle's.
        assert launched.wait(timeout=WAIT) == 3
        assert accepted.wait(timeout=WAIT) == 0 and accepted.state == EXITED
        assert list(accepted.output(timeout=WAIT)) == []
        # Its stdout is not piped to the launch: it is this process's own, shared.
        assert list(launched.output(timeout=WAIT)) == []
        assert "child started" in _shared_output_says(capfd, "child started")
    finally:
        print("trace:", accepted.trace, launched.trace)
        launched.close()
        accepted.close()


def test_an_attachment_is_spent_by_its_first_attempt_even_when_refused(
    capfd: pytest.CaptureFixture[str],
) -> None:
    accepted = LaunchedApp.accepting()
    wrong = _launch_into({**dict(accepted.attachment or {}), "token": "wrong"})
    try:
        assert accepted.connected(timeout=WAIT) is False
        assert (accepted.run_id, accepted.exit_code) == (None, -1)
        # Refused, the application sees its channel close and ends on its own.
        assert wrong.wait(timeout=WAIT) == 3

        # The right token, second, finds nothing listening. (A refused attach
        # does not end its process today, so the refusal is read, then it is stopped.)
        right = _launch_into(accepted.attachment)
        try:
            refusal = "All connection attempts failed"
            assert refusal in _shared_output_says(capfd, refusal)
        finally:
            right.close()
    finally:
        print("trace:", accepted.trace)
        wrong.close()
        accepted.close()


def test_an_attachment_no_application_uses_in_time_ends_refused() -> None:
    accepted = LaunchedApp.accepting(timeout=0.5)

    assert accepted.connected(timeout=WAIT) is False
    assert accepted.wait(timeout=WAIT) == -1
    assert list(accepted.events(timeout=WAIT)) == []
    accepted.close()


def test_close_asks_an_accepted_application_to_close_and_its_channel_ends() -> None:
    accepted = LaunchedApp.accepting()
    launched = _launch_into(accepted.attachment)
    try:
        assert accepted.connected(timeout=WAIT)

        accepted.close()

        assert _sent(accepted) == 1 and accepted.exit_code == 0
        assert launched.wait(timeout=WAIT) == 3
    finally:
        launched.close()


def test_close_of_an_attachment_never_used_ends_it() -> None:
    accepted = LaunchedApp.accepting()

    accepted.close()

    assert accepted.run_id is None and accepted.exit_code is not None
    # Whatever reads its events is told they are over, not left waiting --
    # and whatever waits for it to attach is told it never will, at once.
    assert list(accepted.events()) == []
    started = time.monotonic()
    assert accepted.connected(timeout=WAIT) is False
    assert time.monotonic() - started < 1.0


#: An attached application that asks for an attachment, launches its first
#: argument into it, reports when that ends, and serves until closed.
REPORTING = textwrap.dedent("""
    import sys
    from rey_lib.run.app_channel import AttachedApp, LaunchedApp

    channel = AttachedApp.attach(9)
    attachment = channel.request_attachment(timeout=20.0)
    launched = LaunchedApp.launch([sys.executable, "-c", sys.argv[1]], attachment=attachment)
    channel.report_ended(attachment, launched)
    for command in channel.commands():
        if command.get("command") == "close":
            break
    channel.close()
""")


def test_an_application_that_ends_before_attaching_is_reported_by_its_attachment() -> None:
    app = LaunchedApp.launch([sys.executable, "-c", REPORTING, "import sys\nsys.exit(4)\n"])
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        request_id = next(events)["request_attachment"]
        app.send({"attachment": {"port": 1, "token": "unused"}, "request_id": request_id})

        assert next(events) == {"attachment_ended": request_id, "exit_code": 4}
    finally:
        print("trace:", app.trace)
        app.close()


#: An application that asks its launcher for another attachment, then echoes.
REQUESTER = textwrap.dedent("""
    import sys
    from rey_lib.errors.error_utils import AppError
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(8)
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    try:
        channel.emit({"granted": channel.request_attachment(timeout=timeout)})
    except AppError as refusal:
        print("refused:", refusal, flush=True)
        channel.emit({"refused": str(refusal)})
    for command in channel.commands():
        if command.get("command") == "echo":
            channel.emit({"echoed": command.get("value")})
        if command.get("command") == "close":
            break
    channel.close()
""")


def test_a_requested_attachment_is_answered_and_commands_meanwhile_keep_their_order() -> None:
    app = _launch(REQUESTER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        request_id = next(events)["request_attachment"]

        app.send({"command": "echo", "value": "a"})
        app.send({"command": "echo", "value": "b"})
        app.send({"attachment": {"port": 5, "token": "x"}, "request_id": request_id})
        app.send({"command": "echo", "value": "c"})

        assert next(events) == {"granted": {"port": 5, "token": "x", "request_id": request_id}}
        assert [next(events) for _ in range(3)] == [
            {"echoed": "a"}, {"echoed": "b"}, {"echoed": "c"},
        ]
    finally:
        print("trace:", app.trace)
        app.close()


def test_a_request_not_answered_in_time_is_refused_and_the_channel_goes_on() -> None:
    app = LaunchedApp.launch([sys.executable, "-c", REQUESTER, "0.5"])
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert "request_attachment" in next(events)
        assert "within 0.5 s" in next(events)["refused"]

        app.send({"command": "echo", "value": "z"})
        assert next(events) == {"echoed": "z"}
    finally:
        print("trace:", app.trace)
        app.close()


def test_a_request_whose_launcher_closes_the_channel_is_refused(
    capfd: pytest.CaptureFixture[str],
) -> None:
    accepted = LaunchedApp.accepting()
    launched = _launch_into(accepted.attachment, REQUESTER)
    try:
        assert accepted.connected(timeout=WAIT)
        assert "request_attachment" in next(accepted.events(timeout=WAIT))

        accepted.stop()

        assert launched.wait(timeout=WAIT) == 0
        assert "closed the channel" in _shared_output_says(capfd, "closed the channel")
    finally:
        launched.close()
        accepted.close()


# -- lifetime: an attachment is a channel, never a lifetime of its own (#1099) ----

@pytest.mark.parametrize("ending", ["finishes", "is stopped"])
def test_an_attachment_ends_with_its_application_and_is_spent(ending: str) -> None:
    accepted = LaunchedApp.accepting()
    launched = _launch_into(accepted.attachment)
    try:
        assert accepted.connected(timeout=WAIT)
        if ending == "finishes":
            accepted.send({"command": "finish"})
        else:
            launched.stop()
        launched.wait(timeout=WAIT)

        assert accepted.wait(timeout=WAIT) == 0 and accepted.state == EXITED
        with pytest.raises(StateError, match="attachment is spent"):
            accepted.send({"command": "echo", "value": "late"})
    finally:
        launched.close()
        accepted.close()


#: An attached application (the Map's role) that asks for another attachment,
#: launches LOGGER (its first argument) into it, reports its end and detaches it --
#: exactly the Map's sequence -- then answers pings until it is told to close.
LAUNCHER = textwrap.dedent("""
    import sys
    from rey_lib.run.app_channel import AttachedApp, LaunchedApp

    channel = AttachedApp.attach(6)
    attachment = channel.request_attachment(timeout=20.0)
    launched = LaunchedApp.launch([sys.executable, "-c", sys.argv[1]], attachment=attachment)
    channel.report_ended(attachment, launched)
    launched.detach()
    channel.emit({"launched": True})
    for command in channel.commands():
        if command.get("command") == "ping":
            channel.emit({"pong": command.get("value")})
        if command.get("command") == "close":
            break
    channel.close()
""")

#: The application launched into that attachment: it logs and answers each echo.
LOGGER = textwrap.dedent("""
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(7)
    channel.emit({"event": "ready"})
    for command in channel.commands():
        if command.get("command") == "echo":
            print("logged", command.get("value"), flush=True)
            channel.emit({"echoed": command.get("value")})
        if command.get("command") == "close":
            break
    channel.close()
""")


def _launcher_with_a_file() -> tuple[LaunchedApp, Any, LaunchedApp, Any]:
    """The Map's role launched here, its File attached here: both handles and event streams."""
    accepted = LaunchedApp.accepting()
    launcher = LaunchedApp.launch([sys.executable, "-c", LAUNCHER, LOGGER])
    assert launcher.connected(timeout=WAIT)
    launcher_events = launcher.events(timeout=WAIT)
    request_id = next(launcher_events)["request_attachment"]
    launcher.send({"attachment": accepted.attachment, "request_id": request_id})
    assert next(launcher_events) == {"launched": True}
    assert accepted.connected(timeout=WAIT)
    events = accepted.events(timeout=WAIT)
    assert next(events) == {"event": "ready"}
    return launcher, launcher_events, accepted, events


def test_lifetime_the_map_exits_normally_and_the_file_lives_on() -> None:
    """HARD GATE (#1099): Map closes normally -- no grace, no signal -- and the File lives.

    The Map's role launches the File into an attachment, reports it, detaches
    it, and is told to close: it exits on its own at once. The File still
    answers on its channel and still writes its log.
    """
    launcher, _, accepted, events = _launcher_with_a_file()
    try:
        started = time.monotonic()
        launcher.close()

        # Ended by itself, asked once, well inside the close grace: never stopped.
        assert time.monotonic() - started < app_channel._CLOSE_GRACE_SECONDS / 2  # noqa: SLF001
        assert (launcher.exit_code, launcher.state) == (0, EXITED)
        for value in ("after", "and again"):
            accepted.send({"command": "echo", "value": value})
            assert next(events) == {"echoed": value}
    finally:
        print("trace:", launcher.trace, accepted.trace)
        accepted.close()
        launcher.close()


def test_lifetime_the_file_exits_and_the_map_stays_healthy() -> None:
    """(#1099) The File closes; the Map is told, goes on answering, then exits normally."""
    launcher, launcher_events, accepted, _ = _launcher_with_a_file()
    try:
        accepted.close()

        ended = next(launcher_events)
        assert "attachment_ended" in ended and ended["exit_code"] == 0
        launcher.send({"command": "ping", "value": 1})
        assert next(launcher_events) == {"pong": 1}

        started = time.monotonic()
        launcher.close()
        assert time.monotonic() - started < app_channel._CLOSE_GRACE_SECONDS / 2  # noqa: SLF001
        assert (launcher.exit_code, launcher.state) == (0, EXITED)
    finally:
        print("trace:", launcher.trace, accepted.trace)
        launcher.close()


def test_a_launcher_stopped_outright_leaves_the_file_alive() -> None:
    """(#1099) Even a launcher killed with SIGTERM takes nothing of the File with it."""
    launcher, _, accepted, events = _launcher_with_a_file()
    try:
        launcher.stop()
        launcher.wait(timeout=WAIT)

        accepted.send({"command": "echo", "value": "after"})
        assert next(events) == {"echoed": "after"}
    finally:
        accepted.close()
        launcher.close()


def test_a_detached_handoff_is_neither_signalled_nor_waited_for_on_close() -> None:
    launched = LaunchedApp.launch([sys.executable, "-c", "import time\ntime.sleep(30)\n"],
                                  attachment={"port": 1, "token": "unused"})
    handed_off = launched._handoff  # noqa: SLF001 -- the process itself, to prove it lives
    try:
        launched.detach()
        started = time.monotonic()

        launched.close()
        launched.stop()
        del launched

        assert time.monotonic() - started < 1.0
        assert handed_off.poll() is None
    finally:
        handed_off.kill()
        handed_off.wait(timeout=WAIT)


def test_only_a_handoff_can_be_detached() -> None:
    accepted = LaunchedApp.accepting()
    try:
        with pytest.raises(StateError, match="only an application launched into an attachment"):
            accepted.detach()
    finally:
        accepted.close()
