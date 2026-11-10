"""Each synchronous AI execution owns and closes its SDK event loop (#1201)."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIProviderError
from rey_lib.ai.task import AITask


def task():
    return AITask(task_key="explain", profile_key="testing", model_identifier="ollama:scripted",
                  input=AIInput.prompt("Explain it."))


@pytest.mark.filterwarnings("error::DeprecationWarning")
@pytest.mark.parametrize("worker", [False, True], ids=["calling-thread", "worker-thread"])
def test_repeated_executions_have_separate_closed_loops(monkeypatch, worker):
    loops = []

    async def answer(_messages, _info):
        loops.append(asyncio.get_running_loop())
        return ModelResponse(parts=[TextPart("ready")])

    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *args: FunctionModel(answer))

    def execute_twice():
        asyncio.set_event_loop(None)
        assert ai.execute_task(task()).text == "ready"
        assert loops[-1].is_closed()
        assert ai.execute_task(task()).text == "ready"
        assert loops[-1].is_closed()

    if worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(execute_twice).result(timeout=10)
    else:
        execute_twice()
    assert len(loops) == 2 and loops[0] is not loops[1]


def test_execution_failure_also_closes_its_loop(monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError
    loops = []

    async def refuse(_messages, _info):
        loops.append(asyncio.get_running_loop())
        raise ModelHTTPError(503, "scripted", {"error": "unavailable"})

    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *args: FunctionModel(refuse))
    with pytest.raises(AIProviderError):
        ai.execute_task(task())
    assert loops and all(loop.is_closed() for loop in loops)
