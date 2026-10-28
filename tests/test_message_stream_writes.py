"""A MessageStream writes one message at a time, however many send at once (#1280).

The BFF answers each browser request on its own thread and relays its command
through the one loop to the application's connection. Two arriving together
are two writes of one stream in flight together; the stream is what keeps them
apart, so every message arrives whole and none is refused.
"""

from __future__ import annotations

import threading

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from rey_lib.run.async_runtime import Loop, MessageStream, accept_one, connect_local, listen_local

#: Larger than a socket's buffer, so a write waits for the reader part-way through.
_PAYLOAD = "x" * (2 * 1024 * 1024)

#: How many senders write at once.
_SENDERS = 6


async def _receive(stream: MessageStream, count: int, into: list[dict[str, Any]]) -> None:
    """Read ``count`` messages from ``stream`` into ``into``."""
    for _ in range(count):
        message = await stream.receive()
        if message is None:
            return
        into.append(message)


def test_messages_sent_together_arrive_whole_and_none_is_refused() -> None:
    loop = Loop()
    try:
        listener, port = loop.call(listen_local)
        accepted = loop.start(accept_one, listener, 10.0)
        sender = loop.call(connect_local, port)
        receiver = accepted.result(timeout=10)
        assert receiver is not None

        received: list[dict[str, Any]] = []
        reading = loop.start(_receive, receiver, _SENDERS, received)
        start = threading.Barrier(_SENDERS)

        def send(index: int) -> None:
            start.wait()
            loop.call(sender.send, {"index": index, "payload": _PAYLOAD})

        with ThreadPoolExecutor(max_workers=_SENDERS) as senders:
            sent = [senders.submit(send, index) for index in range(_SENDERS)]
            # A refused write is raised here, in the test, by the send it refused.
            for one in sent:
                one.result(timeout=30)
        reading.result(timeout=30)

        assert sorted(message["index"] for message in received) == list(range(_SENDERS))
        assert all(message["payload"] == _PAYLOAD for message in received)
        loop.call(sender.close)
        loop.call(receiver.close)
        loop.call(listener.aclose)
    finally:
        loop.close()
