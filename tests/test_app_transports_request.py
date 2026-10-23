"""AppTransport.request: one bounded request/response over the application channel (#1127).

The request is correlated by ``request_id`` alone, waits no longer than its
bound, ends its wait when the application ends, and always releases its
subscription. What the application answers is returned exactly as emitted.
"""

from __future__ import annotations

import queue

from typing import Any, Callable, Iterator, Optional

import pytest

from rey_lib.errors.error_utils import AppError

from rey_lib.run.app_channel import IO_REQUEST_SECONDS
from rey_lib.run.app_transports import AppTransport

_END = object()


class _App:
    """A stand-in launched application: each command sent is answered by ``respond``."""

    def __init__(self, respond: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> None:
        self._respond = respond
        self._events: queue.Queue[Any] = queue.Queue()
        self.sent: list[dict[str, Any]] = []
        self.exit_code: Optional[int] = None
        self.run_id = 100

    def events(self) -> Iterator[dict[str, Any]]:
        while (event := self._events.get()) is not _END:
            yield event

    def output(self) -> Iterator[str]:
        return iter(())

    def send(self, command: dict[str, Any]) -> None:
        self.sent.append(dict(command))
        for event in self._respond(dict(command)):
            self._events.put(event)

    def end(self) -> None:
        self.exit_code = 0
        self._events.put(_END)


def _transport(respond: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> tuple[AppTransport, _App]:
    app = _App(respond)
    transport = AppTransport(app)
    transport.start()
    return transport, app


def _answer(command: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"record": {"key": "k1"}, "request_id": command["request_id"]}]


def test_the_answer_is_the_message_carrying_the_request_id() -> None:
    transport, app = _transport(_answer)

    answer = transport.request({"command": "record", "key": "k1"})

    assert answer == {"record": {"key": "k1"}, "request_id": app.sent[0]["request_id"]}
    assert app.sent[0]["command"] == "record" and app.sent[0]["request_id"]
    app.end()


def test_other_pushes_are_not_the_answer() -> None:
    def respond(command: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"projection": {}}, {"record": {}, "request_id": "someone-else"},
                {"record": {"key": "mine"}, "request_id": command["request_id"]}]

    transport, app = _transport(respond)

    assert transport.request({"command": "record"})["record"] == {"key": "mine"}
    app.end()


def test_an_error_answer_is_returned_untouched() -> None:
    transport, app = _transport(
        lambda command: [{"error": "no such record", "request_id": command["request_id"]}])

    answer = transport.request({"command": "record"})

    assert answer == {"error": "no such record", "request_id": app.sent[0]["request_id"]}
    app.end()


def test_a_callers_request_id_is_kept() -> None:
    transport, app = _transport(_answer)

    answer = transport.request({"command": "record", "request_id": "r-42"})

    assert app.sent[0]["request_id"] == "r-42"
    assert answer["request_id"] == "r-42"
    app.end()


def test_an_answer_pushed_as_the_command_is_sent_is_not_missed() -> None:
    """Subscribed before sending: the stand-in answers inside send itself."""
    transport, app = _transport(_answer)

    assert transport.request({"command": "record"}, timeout=1.0)["record"] == {"key": "k1"}
    app.end()


def test_no_answer_within_the_bound_names_the_command() -> None:
    transport, app = _transport(lambda command: [])

    with pytest.raises(AppError, match="'record'"):
        transport.request({"command": "record"}, timeout=0.3)
    app.end()


def test_an_application_that_ends_unanswered_names_the_command() -> None:
    transport, app = _transport(lambda command: [])
    app.end()

    with pytest.raises(AppError, match="ended before answering 'record'"):
        transport.request({"command": "record"}, timeout=IO_REQUEST_SECONDS)


@pytest.mark.parametrize("respond, raises", [
    (_answer, False), (lambda command: [], True)], ids=["answered", "unanswered"])
def test_the_request_is_no_longer_pending_afterwards(respond: Any, raises: bool) -> None:
    transport, app = _transport(respond)

    if raises:
        with pytest.raises(AppError):
            transport.request({"command": "record"}, timeout=0.3)
    else:
        transport.request({"command": "record"})

    assert transport._pending == {} and transport._listeners == []
    app.end()


def test_the_answer_belongs_to_its_request_and_other_pushes_still_reach_subscribers() -> None:
    """#1141 S3: an answer is not broadcast; everything else is pushed as before."""
    def respond(command: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"projection": {}},
                {"record": {"key": "mine"}, "request_id": command["request_id"]},
                {"record": {}, "request_id": "someone-else"}]

    transport, app = _transport(respond)
    seen: list[dict[str, Any]] = []
    transport.subscribe(seen.append)

    answer = transport.request({"command": "record"})
    app.end()
    while not transport.ended():
        pass

    assert answer["record"] == {"key": "mine"}
    assert seen == [{"projection": {}}, {"record": {}, "request_id": "someone-else"}]
