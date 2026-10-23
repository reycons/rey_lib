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


def test_a_launch_whose_process_ends_before_attaching_is_over() -> None:
    # Ends at once, on an error it can only print (#1175, #1171).
    app = _launch("import sys\nprint('ConfigError: no control', file=sys.stderr)\nsys.exit(1)\n")
    try:
        assert app.connected(timeout=WAIT) is False
        assert app.wait(timeout=WAIT) == 1
        # Its events end: no channel will ever carry any.
        assert list(app.events(timeout=WAIT)) == []
        assert "ConfigError: no control" in "".join(app.output(timeout=WAIT))
        assert app.run_id is None and app.state == EXITED
    finally:
        print("trace:", app.trace)
    started = time.monotonic()
    app.close()
    assert time.monotonic() - started < WAIT


def test_a_refused_launch_ends_its_events_while_its_process_runs() -> None:
    app = _launch(SLEEPER.replace(
        "import time",
        "import os, time\nos.environ['REY_APP_EXECUTE_TOKEN'] = 'wrong'",
    ))
    try:
        assert app.connected(timeout=WAIT) is False
        # Refused: no events will come, whether or not the process lives on.
        assert list(app.events(timeout=WAIT)) == []
    finally:
        print("trace:", app.trace)
        app.close()
    assert app.exit_code is not None


def test_closing_a_launch_ends_its_events_whatever_it_reached() -> None:
    app = _launch("import time\ntime.sleep(600)\n")

    app.close()

    assert list(app.events(timeout=WAIT)) == []
    assert app.connected(timeout=0) is False


#: A served application whose entry point declared ABORTED for SIGTERM (#1182).
COOPERATIVE = textwrap.dedent("""
    from rey_lib.config import bootstrap
    from rey_lib.run.app_channel import AttachedApp

    bootstrap._terminate_as("ABORTED")
    channel = AttachedApp.attach(8)
    channel.emit({"event": "ready"})
    try:
        for command in channel.commands():
            pass
    finally:
        print("finalized", flush=True)
        channel.close()
""")


def test_a_stopped_application_with_a_policy_finalizes_and_exits_143() -> None:
    app = _launch(COOPERATIVE)
    try:
        assert app.connected(timeout=WAIT)
        assert next(app.events(timeout=WAIT)) == {"event": "ready"}

        app.stop()

        assert app.wait(timeout=WAIT) == 143
        assert "finalized" in "".join(app.output(timeout=WAIT))
    finally:
        print("trace:", app.trace)
        app.close()


def test_close_kills_an_application_that_outlives_its_termination_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_channel, "_CLOSE_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(app_channel, "_KILL_GRACE_SECONDS", 0.5)
    # Ignores the close command and SIGTERM alike: only a kill ends it.
    app = _launch(SLEEPER.replace(
        "import time", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)"))
    assert app.connected(timeout=WAIT)
    started = time.monotonic()

    app.close()

    assert time.monotonic() - started < WAIT / 2
    assert app.exit_code == -9 and app.state == STOPPED
    assert any("killed" in point for _, point in app.trace)


def test_a_failed_attach_leaves_nothing_keeping_the_process_alive() -> None:
    # Nobody listens on port 1: the attach fails, and the process must still end (#1167).
    code = textwrap.dedent("""
        import os
        os.environ["REY_APP_EXECUTE_PORT"] = "1"
        os.environ["REY_APP_EXECUTE_TOKEN"] = "t"
        from rey_lib.run.app_channel import AttachedApp
        try:
            AttachedApp.attach(7)
        except OSError:
            print("attach failed", flush=True)
    """)
    import subprocess

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=WAIT)

    assert done.returncode == 0
    assert "attach failed" in done.stdout


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


# -- a request from an application, routed by its execution context (#1196) ----

#: A child that asks another application, through its own channel, when told to;
#: it keeps answering its own commands meanwhile.
ASKER = textwrap.dedent("""
    import threading
    from rey_lib.errors.error_utils import AppError
    from rey_lib.run.app_channel import AttachedApp
    from rey_lib.run.async_runtime import CLOSED_ERRORS

    channel = AttachedApp.attach(11)

    def ask(destination, timeout):
        try:
            answer = channel.request(destination, {"command": "projection"}, timeout=timeout)
            channel.emit({"event": "asked", "answer": answer})
        except AppError as refused:
            print("refused:", refused, flush=True)
            try:
                channel.emit({"event": "refused", "error": str(refused)})
            except CLOSED_ERRORS:  # the channel itself may be what closed
                pass

    channel.emit({"event": "ready"})
    for command in channel.commands():
        name = command.get("command")
        if name == "ask":
            threading.Thread(target=ask, args=(command["destination"],
                                               command.get("timeout", 20.0))).start()
        if name == "echo":
            channel.emit({"event": "echoed", "value": command.get("value")})
        if name in ("finish", "close"):
            break
    channel.close()
""")


def _routed(events) -> dict:
    """The next message the application emitted that asks to be routed."""
    while "route" not in (message := next(events)):
        pass
    return message


def test_a_request_goes_out_on_the_applications_own_channel_and_its_answer_comes_back() -> None:
    app = _launch(ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask", "destination": "transport-b"})

        routed = _routed(events)
        # The envelope names the destination and the payload; the id is the asker's.
        assert routed["route"] == {"destination": "transport-b",
                                   "payload": {"command": "projection"}}
        request_id = routed["request_id"]
        assert request_id

        # A command still reaches the application's own loop while the request waits.
        app.send({"command": "echo", "value": "meanwhile"})
        assert next(events) == {"event": "echoed", "value": "meanwhile"}

        answer = {"answer": {"projection": {"panel_type": "file"}, "request_id": request_id},
                  "request_id": request_id}
        app.send(answer)
        # The answer went to the waiting request, never to the command loop.
        assert next(events) == {"event": "asked", "answer": answer}
        app.send({"command": "finish"})
        assert app.wait(timeout=WAIT) == 0
    finally:
        print("trace:", app.trace)
        app.close()


def test_a_request_with_no_answer_in_time_is_refused_by_name() -> None:
    app = _launch(ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask", "destination": "transport-b", "timeout": 0.5})
        _routed(events)

        refused = next(events)
        assert refused["event"] == "refused"
        assert "'transport-b'" in refused["error"] and "0.5 s" in refused["error"]
        app.send({"command": "finish"})
        assert app.wait(timeout=WAIT) == 0
    finally:
        print("trace:", app.trace)
        app.close()


def test_a_request_still_waiting_when_the_channel_closes_is_refused() -> None:
    app = _launch(ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask", "destination": "transport-b"})
        _routed(events)

        # The launcher ends the conversation; the waiting request is told, not left hanging.
        app.close()
        assert "refused: The channel closed before 'transport-b' answered." in "".join(
            app.output(timeout=WAIT))
    finally:
        print("trace:", app.trace)
        app.close()


# -- a destination-free subject request (#1141 S3) ------------------------------

#: A child that asks for an operation on its bound subject when told to -- naming
#: no destination -- and keeps answering its own commands meanwhile.
SUBJECT_ASKER = textwrap.dedent("""
    import threading
    from rey_lib.errors.error_utils import AppError
    from rey_lib.run.app_channel import AttachedApp
    from rey_lib.run.async_runtime import CLOSED_ERRORS

    channel = AttachedApp.attach(12)

    def ask(timeout):
        try:
            answer = channel.subject_request(
                {"capability": "file.facts", "inputs": {}}, timeout=timeout)
            channel.emit({"event": "asked", "answer": answer})
        except AppError as refused:
            print("refused:", refused, flush=True)
            try:
                channel.emit({"event": "refused", "error": str(refused)})
            except CLOSED_ERRORS:
                pass

    channel.emit({"event": "ready"})
    for command in channel.commands():
        name = command.get("command")
        if name == "ask":
            threading.Thread(target=ask, args=(command.get("timeout", 20.0),)).start()
        if name == "echo":
            channel.emit({"event": "echoed", "value": command.get("value")})
        if name in ("finish", "close"):
            break
    channel.close()
""")


def _subject_requested(events) -> dict:
    """The next message the application emitted as a subject request."""
    while "subject_request" not in (message := next(events)):
        pass
    return message


def test_a_subject_request_names_no_destination_and_its_answer_comes_back() -> None:
    app = _launch(SUBJECT_ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask"})

        asked = _subject_requested(events)
        # The payload as given, a request_id minted by the asker, and nothing that
        # addresses another application.
        assert asked["subject_request"] == {"capability": "file.facts", "inputs": {}}
        assert set(asked) == {"subject_request", "request_id"} and asked["request_id"]

        app.send({"command": "echo", "value": "meanwhile"})
        assert next(events) == {"event": "echoed", "value": "meanwhile"}

        answer = {"answer": {"result": {"file_type": "csv"}}, "request_id": asked["request_id"]}
        app.send(answer)
        # The answer went to the waiting request, never to the command loop.
        assert next(events) == {"event": "asked", "answer": answer}
        app.send({"command": "finish"})
        assert app.wait(timeout=WAIT) == 0
    finally:
        app.close()


def test_a_subject_request_with_no_answer_in_time_is_refused() -> None:
    app = _launch(SUBJECT_ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask", "timeout": 0.5})
        _subject_requested(events)

        refused = next(events)
        assert refused["event"] == "refused"
        assert "bound subject" in refused["error"] and "0.5 s" in refused["error"]
        app.send({"command": "finish"})
        assert app.wait(timeout=WAIT) == 0
    finally:
        app.close()


def test_a_subject_request_still_waiting_when_the_channel_closes_is_refused() -> None:
    app = _launch(SUBJECT_ASKER)
    try:
        assert app.connected(timeout=WAIT)
        events = app.events(timeout=WAIT)
        assert next(events) == {"event": "ready"}
        app.send({"command": "ask"})
        _subject_requested(events)

        app.close()
        assert "refused: The channel closed before the bound subject answered." in "".join(
            app.output(timeout=WAIT))
    finally:
        app.close()
