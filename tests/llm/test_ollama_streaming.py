"""Focused Ollama provider streaming-contract coverage."""

from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from rey_lib.llm.exceptions import CancellationFailure
from rey_lib.llm.providers.base import Message
from rey_lib.llm.providers.ollama import OllamaProvider


class _ClosableIterator:
    def __init__(self, values):
        self._values = iter(values)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._values)

    def close(self):
        self.closed = True


def test_sdk_stream_forwards_chunks_and_returns_complete_response(monkeypatch) -> None:
    chunks = [
        SimpleNamespace(
            message=SimpleNamespace(content="", thinking="considering "),
            prompt_eval_count=0,
            eval_count=0,
        ),
        SimpleNamespace(
            message=SimpleNamespace(content="hello world", thinking=""),
            prompt_eval_count=4,
            eval_count=2,
        ),
    ]
    response = _ClosableIterator(chunks)
    client = SimpleNamespace(chat=lambda **kwargs: response)
    monkeypatch.setitem(
        __import__("sys").modules,
        "ollama",
        SimpleNamespace(Client=lambda **kwargs: client),
    )
    seen: list[str] = []

    result = OllamaProvider().run(
        [Message(role="user", content="test")],
        model="model",
        on_chunk=seen.append,
    )

    assert seen == ["considering ", "hello world"]
    assert result.content == "hello world"
    assert result.tokens_in == 4
    assert result.tokens_out == 2
    assert response.closed is True


def test_http_stream_forwards_ndjson_chunks_and_returns_complete_response(
    monkeypatch,
) -> None:
    rows = [
        {"message": {"content": "", "thinking": "considering "}, "done": False},
        {
            "message": {"content": "hello world", "thinking": ""},
            "done": True,
            "prompt_eval_count": 4,
            "eval_count": 2,
        },
    ]
    response = io.BytesIO(
        b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)
    )
    response.__enter__ = lambda: response
    response.__exit__ = lambda *_args: None
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    seen: list[str] = []

    result = OllamaProvider()._run_via_http(
        [{"role": "user", "content": "test"}],
        "model",
        100,
        0.0,
        on_chunk=seen.append,
    )

    assert seen == ["considering ", "hello world"]
    assert result.content == "hello world"
    assert result.tokens_in == 4
    assert result.tokens_out == 2
    assert response.closed is True


def test_http_stream_closes_response_when_cancelled(monkeypatch) -> None:
    class _BlockingResponse:
        def __init__(self):
            self.closed = threading.Event()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def __iter__(self):
            return self

        def __next__(self):
            self.closed.wait(timeout=1)
            raise AttributeError("'NoneType' object has no attribute 'peek'")

        def close(self):
            self.closed.set()

    response = _BlockingResponse()
    cancelled = threading.Event()
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    def request_cancel() -> None:
        time.sleep(0.02)
        cancelled.set()

    threading.Thread(target=request_cancel, daemon=True).start()

    with pytest.raises(CancellationFailure, match="cancelled"):
        OllamaProvider()._run_via_http(
            [{"role": "user", "content": "test"}],
            "model",
            100,
            0.0,
            on_chunk=lambda _chunk: None,
            cancelled=cancelled.is_set,
        )

    assert response.closed.is_set()
