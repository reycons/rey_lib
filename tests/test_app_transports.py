"""AppTransport / AppTransports: the common fabric applications communicate through (#1141).

A real child application is launched and carried by a transport; stand-in
applications show the fabric's own rules -- lifetime, request correlation and the
pre-existing route of one application's request to another (#1196). Moved from
rey_console with the fabric itself; nothing here knows a Console, a browser or a
subject.
"""

from __future__ import annotations

import queue
import sys
import textwrap
import threading
import time

from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.run.app_channel import LaunchedApp
from rey_lib.run.app_transports import AppTransport, AppTransports, TransportNotHeld

#: How long any one wait in these tests may take.
WAIT = 20.0

#: A child application: it owns run 100, attaches, and serves until told to close.
CHILD = textwrap.dedent("""
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(100)
    channel.emit({"projection": {"steps": [{"id": "s1", "state": "PENDING"}]}})
    for command in channel.commands():
        if command.get("command") == "direct":
            print("directing", flush=True)
            channel.emit({"projection": {"steps": [{"id": "s1", "state": "RUNNING"}]}})
            channel.emit({"directed": {"run_id": 101, "status": "SUCCEEDED"}})
        if command.get("command") == "close":
            break
    channel.close()
""")


def _pushed(transport: AppTransport) -> "queue.Queue[dict[str, Any]]":
    received: queue.Queue[dict[str, Any]] = queue.Queue()
    transport.subscribe(received.put)
    return received


#: Pushed messages already taken off a queue but not yet asked for, per queue.
_held: dict[int, list[dict[str, Any]]] = {}


def _next(received: "queue.Queue[dict[str, Any]]", key: str) -> dict[str, Any]:
    """The next pushed message carrying ``key``, in push order.

    Events and stdout arrive interleaved; a message carrying another key is kept
    for a later call, never discarded.
    """
    held = _held.setdefault(id(received), [])
    for index, message in enumerate(held):
        if key in message:
            return held.pop(index)
    while True:
        message = received.get(timeout=WAIT)
        if key in message:
            return message
        held.append(message)


def test_events_and_stdout_are_pushed_and_commands_relayed() -> None:
    transport = AppTransport(LaunchedApp.launch([sys.executable, "-c", CHILD]))
    received = _pushed(transport)
    transport.start()
    try:
        assert transport.connected(timeout=WAIT)
        assert transport.run_id == 100
        assert _next(received, "projection")["projection"]["steps"][0]["state"] == "PENDING"

        transport.send({"command": "direct", "step": "s1"})

        assert _next(received, "projection")["projection"]["steps"][0]["state"] == "RUNNING"
        assert _next(received, "directed") == {"directed": {"run_id": 101,
                                                            "status": "SUCCEEDED"}}
        assert _next(received, "stdout") == {"stdout": "directing\n"}
    finally:
        transport.close()


def test_an_unsubscribed_listener_is_told_nothing_more() -> None:
    transport = AppTransport(LaunchedApp.launch([sys.executable, "-c", CHILD]))
    kept = _pushed(transport)
    dropped: list[dict[str, Any]] = []
    stop = transport.subscribe(dropped.append)
    stop()
    transport.start()
    try:
        assert transport.connected(timeout=WAIT)
        _next(kept, "projection")
        assert dropped == []
    finally:
        transport.close()


def test_close_ends_the_application() -> None:
    transport = AppTransport(LaunchedApp.launch([sys.executable, "-c", CHILD]))
    transport.start()
    assert transport.connected(timeout=WAIT)

    transport.close()

    assert transport._app.exit_code == 0  # noqa: SLF001 -- the ended process's own code


def test_close_hands_the_shutdown_to_appexecute_and_releases_its_pumps() -> None:
    """The transport sequences nothing: one close on the launched handle, then its pumps."""
    called: list[str] = []

    class _Launched:
        run_id, exit_code = 100, None

        def events(self) -> Iterator[dict[str, Any]]:
            return iter(())

        def output(self) -> Iterator[str]:
            return iter(())

        def send(self, command: Any) -> None:
            called.append(f"send {command}")

        def wait(self, timeout: Any = None) -> None:
            called.append("wait")

        def close(self) -> None:
            called.append("close")

    transport = AppTransport(_Launched())
    transport.start()

    transport.close()

    assert called == ["close"]
    assert not any(pump.is_alive() for pump in transport._pumps)  # noqa: SLF001


#: An application that cannot start: it prints why, and ends before attaching.
UNSTARTABLE = textwrap.dedent("""
    import sys
    print("Traceback (most recent call last):", file=sys.stderr)
    print("ConfigError: ctx.control.procedure_map is not set", file=sys.stderr)
    sys.exit(1)
""")


def test_an_establish_whose_application_ends_before_attaching_fails_at_once_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1171: a configuration failure before attaching -- reported, not a silent wait.
    monkeypatch.setattr(LaunchedApp, "launch_operation", classmethod(
        lambda cls, _ctx, *_request: LaunchedApp.launch([sys.executable, "-c", UNSTARTABLE])))
    transports = AppTransports()
    started = time.monotonic()

    with pytest.raises(AppError) as refused:
        transports.establish(SimpleNamespace(), "workflow_app", "serve", {"workflow_id": 15},
                             timeout=WAIT)

    # Well inside the bound: the launch ended, nothing waited it out.
    assert time.monotonic() - started < WAIT / 2
    assert "exit code 1" in str(refused.value)
    assert "ConfigError: ctx.control.procedure_map is not set" in str(refused.value)
    # Closed, not held: nothing is left for a consumer to wait on.
    assert transports._held == {}


# -- a context that ends closes the applications it holds (#1189) -----------


class _Closing:
    """A stand-in launched application: it records being closed, and emits nothing."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_close_all_closes_every_transport_the_runtime_holds() -> None:
    transports = AppTransports()
    apps = [_Closing(), _Closing()]
    refs = [transports.open(app)[0] for app in apps]

    transports.close_all()

    assert [app.closed for app in apps] == [1, 1]
    for ref in refs:
        with pytest.raises(TransportNotHeld):
            transports.get(ref)


def test_the_runtimes_transports_close_together_not_one_after_another() -> None:
    """Each application keeps its own close; the collection does not add them up.

    Both closes wait for each other: closed one after another, the first would
    wait for a second that never starts.
    """
    both = threading.Barrier(2, timeout=5)

    class _WaitingForTheOther(_Closing):
        def close(self) -> None:
            both.wait()
            super().close()

    transports = AppTransports()
    apps = [_WaitingForTheOther(), _WaitingForTheOther()]
    for app in apps:
        transports.open(app)

    transports.close_all()

    assert [app.closed for app in apps] == [1, 1]


def test_nothing_can_reattach_to_a_transport_the_ended_runtime_is_closing() -> None:
    transports = AppTransports()
    seen: list[bool] = []

    class _Looking(_Closing):
        def close(self) -> None:
            try:
                transports.get(ref)
                seen.append(True)
            except TransportNotHeld:
                seen.append(False)
            super().close()

    ref, _ = transports.open(_Looking())

    transports.close_all()

    assert seen == [False]


def test_one_transport_failing_to_close_strands_none_of_the_others() -> None:
    class _Refusing(_Closing):
        def close(self) -> None:
            raise RuntimeError("would not close")

    transports = AppTransports()
    closing = _Closing()
    transports.open(_Refusing())
    transports.open(closing)

    with pytest.raises(RuntimeError, match="would not close"):
        transports.close_all()
    assert closing.closed == 1


# -- one application's request routed to another through AppTransports (#1196) --

class _Peer:
    """An application behind a transport: what it emits is pushed; what it is sent is kept.

    ``answer`` makes it reply to each command the way a hosted serve does -- with a
    message carrying the command's request_id.
    """

    def __init__(self, answer=None) -> None:
        self.run_id = 1
        self.exit_code = None
        self.sent: list[dict[str, Any]] = []
        self._emitted: queue.Queue[Any] = queue.Queue()
        self._answer = answer

    def events(self) -> Iterator[dict[str, Any]]:
        while (message := self._emitted.get()) is not None:
            yield message

    def output(self) -> Iterator[str]:
        return iter(())

    def connected(self, timeout: Any = None) -> bool:
        return True

    def emit(self, message: dict[str, Any]) -> None:
        self._emitted.put(message)

    def send(self, command: dict[str, Any]) -> None:
        self.sent.append(command)
        if self._answer is not None:
            self._emitted.put(self._answer(command))

    def end(self, code: int = 0) -> None:
        self.exit_code = code
        self._emitted.put(None)

    def close(self) -> None:
        self.end(0)


def _projection_answer(command: dict[str, Any]) -> dict[str, Any]:
    return {"projection": {"panel_type": "file"}, "request_id": command.get("request_id")}


def _until(condition, timeout: float = WAIT) -> None:
    """Wait for a condition another thread makes true."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _two(answer_b=_projection_answer):
    transports = AppTransports()
    a, b = _Peer(), _Peer(answer_b)
    ref_a, transport_a = transports.open(a)
    ref_b, transport_b = transports.open(b)
    return transports, (a, ref_a, transport_a), (b, ref_b, transport_b)


def test_a_routed_request_reaches_its_destination_and_the_answer_returns_under_its_own_id() -> None:
    _, (a, _, transport_a), (b, ref_b, transport_b) = _two()
    pushed_a, pushed_b = _pushed(transport_a), _pushed(transport_b)
    transport_a.start()
    transport_b.start()

    a.emit({"route": {"destination": ref_b, "payload": {"command": "projection"}},
            "request_id": "r-1"})

    _until(lambda: a.sent)
    # B was sent the payload under A's own request_id ...
    assert b.sent == [{"command": "projection", "request_id": "r-1"}]
    # ... and A was sent B's answer, as B emitted it, under that same id.
    assert a.sent == [{"answer": {"projection": {"panel_type": "file"}, "request_id": "r-1"},
                       "request_id": "r-1"}]
    # The routed request never reached A's own subscribers (its panel stream).
    assert pushed_a.empty()
    # B's answer belonged to that request alone: B's own subscribers never saw it (#1141 S3).
    assert pushed_b.empty()
    a.end()
    b.end()


def test_a_destination_the_runtime_does_not_hold_is_answered_as_an_error() -> None:
    _, (a, _, transport_a), _ = _two()
    transport_a.start()

    a.emit({"route": {"destination": "no-such-transport", "payload": {"command": "projection"}},
            "request_id": "r-2"})

    _until(lambda: a.sent)
    assert a.sent[0]["request_id"] == "r-2" and "error" in a.sent[0]
    a.end()


def test_a_destination_held_by_another_runtime_is_not_reachable() -> None:
    _, (a, _, transport_a), _ = _two()
    other = AppTransports()
    ref_elsewhere, _ = other.open(_Peer(_projection_answer))
    transport_a.start()

    a.emit({"route": {"destination": ref_elsewhere, "payload": {"command": "projection"}},
            "request_id": "r-3"})

    _until(lambda: a.sent)
    assert "error" in a.sent[0]
    a.end()


def test_a_destination_that_ends_before_answering_is_answered_as_an_error() -> None:
    _, (a, _, transport_a), (b, ref_b, transport_b) = _two(answer_b=None)
    transport_a.start()
    transport_b.start()

    a.emit({"route": {"destination": ref_b, "payload": {"command": "projection"}},
            "request_id": "r-4"})
    _until(lambda: b.sent)
    b.end(1)

    _until(lambda: a.sent)
    assert a.sent[0]["request_id"] == "r-4"
    assert "ended before answering" in a.sent[0]["error"]
    a.end()


def test_an_asker_that_has_ended_is_not_answered() -> None:
    def late(command: dict[str, Any]) -> dict[str, Any]:
        _until(lambda: a.exit_code is not None)
        return _projection_answer(command)

    _, (a, _, transport_a), (b, ref_b, transport_b) = _two(answer_b=late)
    transport_a.start()
    transport_b.start()

    a.emit({"route": {"destination": ref_b, "payload": {"command": "projection"}},
            "request_id": "r-5"})
    _until(lambda: b.sent)
    a.end()

    time.sleep(0.5)
    assert a.sent == []
    b.end()


#: Two real applications in one runtime: B serves its projection; A asks B for it.
SERVER = textwrap.dedent("""
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(201)
    for command in channel.commands():
        if command.get("command") == "projection":
            channel.emit({"projection": {"panel_type": "file"},
                          "request_id": command.get("request_id")})
        if command.get("command") == "close":
            break
    channel.close()
""")

ASKER = textwrap.dedent("""
    import threading
    from rey_lib.run.app_channel import AttachedApp

    channel = AttachedApp.attach(202)

    def ask(destination):
        channel.emit({"asked": channel.request(destination, {"command": "projection"})})

    for command in channel.commands():
        if command.get("command") == "ask":
            threading.Thread(target=ask, args=(command["destination"],)).start()
        if command.get("command") == "close":
            break
    channel.close()
""")


def test_one_running_application_asks_another_through_their_runtime() -> None:
    transports = AppTransports()
    ref_b, transport_b = transports.open(LaunchedApp.launch([sys.executable, "-c", SERVER]))
    ref_a, transport_a = transports.open(LaunchedApp.launch([sys.executable, "-c", ASKER]))
    pushed_a = _pushed(transport_a)
    try:
        transport_b.start()
        transport_a.start()
        assert transport_a.connected(timeout=WAIT) and transport_b.connected(timeout=WAIT)

        transport_a.send({"command": "ask", "destination": ref_b})

        asked = _next(pushed_a, "asked")["asked"]
        assert asked["answer"]["projection"] == {"panel_type": "file"}
        assert asked["answer"]["request_id"] == asked["request_id"]
    finally:
        transports.close_all()
