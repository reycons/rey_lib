"""The process-level safety net for failures nothing else caught.

An exception escaping every application handler used to be printed by the
interpreter to the process stream and never reached the run log, so the record
of why a process died lived somewhere different from the record of everything it
did. The bootstrap installs a boundary that routes those failures through the
Rey logger.

It is a net, not a handler: it records and then delegates, so tracebacks, exit
codes and interpreter behaviour are unchanged, and an error the application
handles never arrives here at all.
"""

from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import recorded_run  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _recorded_run(recorded_run) -> None:  # noqa: F811
    """Every test here crosses the launch boundary, which records a run."""

from rey_lib.errors import error_utils
from rey_lib.errors.error_utils import install_process_error_boundary


@pytest.fixture
def boundary(caplog: pytest.LogCaptureFixture):
    """Install the boundary on clean hooks and restore them afterwards.

    Clean means the interpreter's own hooks, not merely a cleared flag. Any
    test that builds an app context installs the boundary and the flag stops it
    installing twice -- but nothing puts the hooks back, so by the time this
    runs in a full suite `sys.excepthook` is already a boundary. Installing over
    that one chains them, and a single exception is then recorded twice, which
    is what `test_installing_twice_records_once` exists to detect. Restoring the
    interpreter hooks first makes every test here independent of what ran
    before it.
    """
    saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
    sys.unraisablehook = sys.__unraisablehook__
    error_utils._boundary_installed = False
    caplog.set_level(logging.ERROR)
    install_process_error_boundary()
    yield caplog
    sys.excepthook, threading.excepthook, sys.unraisablehook = saved
    error_utils._boundary_installed = False


def _thread_args(exc: BaseException) -> threading.ExceptHookArgs:
    """What threading.excepthook receives -- the real type, not a stand-in.

    The boundary delegates to whatever hook it replaced, and the interpreter's
    own `threading.__excepthook__` is written in C: it rejects anything that is
    not an ExceptHookArgs. A look-alike worked only while the hook underneath
    happened to be another Python function, which is a property of what ran
    before rather than of this test.
    """
    worker = threading.Thread(name="worker-1")
    return threading.ExceptHookArgs(
        (type(exc), exc, exc.__traceback__, worker),
    )


def test_the_boundary_replaces_the_interpreter_hooks(boundary) -> None:
    """Nothing records what nothing owns."""
    assert sys.excepthook is not sys.__excepthook__
    assert threading.excepthook is not threading.__excepthook__


def test_an_uncaught_main_thread_exception_is_recorded(boundary) -> None:
    """The case that previously reached only the process stream."""
    exc = RuntimeError("the database went away")
    sys.excepthook(type(exc), exc, exc.__traceback__)

    assert "the database went away" in boundary.text
    assert "Unhandled exception" in boundary.text


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_unhandled_thread_exception_is_recorded_with_its_thread(boundary) -> None:
    """A worker dying silently is how a run appears to hang rather than fail."""
    exc = ValueError("worker failed")
    threading.excepthook(_thread_args(exc))

    assert "worker failed" in boundary.text
    assert "worker-1" in boundary.text


def test_the_boundary_delegates_rather_than_swallowing(boundary) -> None:
    """It records, then the process fails exactly as it would have."""
    seen: list[str] = []
    sys.excepthook = lambda *_: seen.append("delegated")
    error_utils._boundary_installed = False
    install_process_error_boundary()

    exc = RuntimeError("boom")
    sys.excepthook(type(exc), exc, exc.__traceback__)

    assert seen == ["delegated"], "the replaced hook was not called"
    assert "boom" in boundary.text


def test_interrupt_and_exit_are_not_failures(boundary) -> None:
    """Logging these as errors would train readers to ignore the record."""
    for exc in (KeyboardInterrupt(), SystemExit(0)):
        sys.excepthook(type(exc), exc, exc.__traceback__)

    assert boundary.text == "", f"a clean exit was recorded as a failure: {boundary.text}"


def test_secrets_in_the_recorded_message_are_masked(boundary) -> None:
    """The message the boundary composes goes through the redactor.

    Only the composed message is covered. The attached traceback is the
    interpreter's rendering and is not redacted — the same trade handle_exception
    already makes, and the reason a fatal record is worth having at all. Removing
    exc_info would mask the secret and lose the traceback, which is the one thing
    a process-death record exists to carry.
    """
    exc = RuntimeError("connect failed password=hunter2")
    sys.excepthook(type(exc), exc, exc.__traceback__)

    composed = boundary.records[0].getMessage()
    assert "hunter2" not in composed, composed
    assert "[REDACTED]" in composed


def test_installing_twice_records_once(boundary) -> None:
    """A process that bootstraps twice must not log its failures twice."""
    install_process_error_boundary()
    install_process_error_boundary()

    exc = RuntimeError("single")
    sys.excepthook(type(exc), exc, exc.__traceback__)

    assert boundary.text.count("Unhandled exception") == 1


def test_the_bootstrap_installs_the_boundary(tmp_path, caplog) -> None:
    """The rule this exists to serve: no entry point installs its own."""
    from rey_lib.config.bootstrap import build_ctx_for_app

    saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    error_utils._boundary_installed = False
    try:
        supplied = SimpleNamespace(
            log_path=f"{tmp_path}/logs/app.{{operation}}.{{timestamp}}.log",
            log_level="INFO",
        )
        build_ctx_for_app(ctx=supplied, operation="boundary")
        assert sys.excepthook is not sys.__excepthook__
        assert threading.excepthook is not threading.__excepthook__
    finally:
        sys.excepthook, threading.excepthook, sys.unraisablehook = saved
        error_utils._boundary_installed = False
