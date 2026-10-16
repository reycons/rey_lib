"""AppExecute's execution and communication capability (backlog console.workflow_map).

A real child process is launched through ``AppExecute.launch``; it attaches with
``AppExecute.attach`` and its own Application Run id. Commands go in, events and
stdout come out, over a localhost channel opened with a one-time token -- AnyIO
underneath, reached only through ``rey_lib.run.async_runtime``.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from rey_lib.errors.error_utils import StateError
from rey_lib.run import AppExecute
from rey_lib.run.run import CONNECTED, EXITED, STOPPED

#: How long any one wait in these tests may take.
WAIT = 20.0

#: A child application: it owns run 77, attaches, and answers what it is sent.
CHILD = textwrap.dedent("""
    import sys
    from rey_lib.run import AppExecute

    application_run = AppExecute(run_id=77, control=None, subject_type="app",
                                 subject_id="child")
    channel = AppExecute.attach(application_run)
    print("child started", flush=True)
    channel.emit({"event": "ready"})
    for command in channel.commands():
        if command.get("command") == "echo":
            channel.emit({"event": "echoed", "value": command.get("value")})
        if command.get("command") == "finish":
            break
    print("child finishing", flush=True)
    channel.close()
    sys.exit(3)
""")

#: A child that never finishes on its own.
SLEEPER = textwrap.dedent("""
    import time
    from rey_lib.run import AppExecute

    channel = AppExecute.attach(AppExecute(run_id=5, control=None))
    channel.emit({"event": "ready"})
    time.sleep(600)
""")


def _launch(code: str):
    return AppExecute.launch([sys.executable, "-c", code])


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

    with pytest.raises(StateError, match="not launched through AppExecute.launch"):
        AppExecute.attach(AppExecute(run_id=1, control=None))
