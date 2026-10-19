"""A running AITask can be withdrawn through AI (backlog 1039).

Deterministic: the model AI builds is replaced by Pydantic AI's FunctionModel,
which signals once it is under way, and the withdrawal comes from another
thread -- as an application's command reader would send it -- only after that
signal. What is asserted is Rey's side: the task records cancelled, the events
end with cancelled and never with failed or completed, AICancelled is raised,
and the execution's token is fresh and held only while it runs.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AICancelled, AIProviderError, AIRequestError
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.streaming import AIEvent, AIEventKind
from rey_lib.ai.task import AITask


def _task() -> AITask:
    return AITask(task_key="explain", profile_key="local_testing",
                  model_identifier="ollama:qwen3-coder-next:latest",
                  input=AIInput.prompt("What is this?"))


def _ai(model: FunctionModel, monkeypatch: pytest.MonkeyPatch) -> AI:
    ai = AI(registry=AIRegistry())
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: model)
    return ai


class _Withdrawal:
    """Cancels a task from another thread once the model is under way."""

    def __init__(self, ai: AI, task: AITask) -> None:
        self.under_way = threading.Event()
        self.signalled: list[bool] = []
        self._thread = threading.Thread(target=self._cancel, args=(ai, task), daemon=True)
        self._thread.start()

    def _cancel(self, ai: AI, task: AITask) -> None:
        self.under_way.wait(5)
        self.signalled.append(ai.cancel_task(task.id))

    def join(self) -> None:
        self._thread.join(5)


def _streaming(withdrawal: list[_Withdrawal]) -> FunctionModel:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "first "
        withdrawal[0].under_way.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            yield "more "
    return FunctionModel(stream_function=stream)


def _waiting(withdrawal: list[_Withdrawal]) -> FunctionModel:
    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        withdrawal[0].under_way.set()
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("too late")])
    return FunctionModel(answer)


class TestWithdrawal:
    def test_a_streamed_run_is_cancelled_and_never_told_as_failed_or_completed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        holder: list[_Withdrawal] = []
        ai, task = _ai(_streaming(holder), monkeypatch), _task()
        holder.append(_Withdrawal(ai, task))
        events: list[AIEvent] = []

        with pytest.raises(AICancelled, match="was cancelled"):
            ai.execute_task(task, observe=events.append)
        holder[0].join()

        assert holder[0].signalled == [True]
        assert task.status == AITask.CANCELLED
        assert (task.answer, task.error) == (None, "")
        kinds = [event.kind for event in events]
        assert kinds[0] is AIEventKind.EXECUTION_STARTED
        assert kinds[-1] is AIEventKind.EXECUTION_CANCELLED
        assert AIEventKind.EXECUTION_FAILED not in kinds
        assert AIEventKind.EXECUTION_COMPLETED not in kinds
        assert len({event.execution_id for event in events}) == 1

    def test_an_unstreamed_call_awaiting_its_answer_is_cancelled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        holder: list[_Withdrawal] = []
        ai, task = _ai(_waiting(holder), monkeypatch), _task()
        holder.append(_Withdrawal(ai, task))

        with pytest.raises(AICancelled):
            ai.execute_task(task)
        holder[0].join()

        assert task.status == AITask.CANCELLED


class TestNothingRunning:
    def test_a_task_not_running_is_not_cancelled(self) -> None:
        ai, task = AI(registry=AIRegistry()), _task()

        assert ai.cancel_task(task.id) is False
        assert ai.cancel_task("no-such-task") is False
        assert task.status == AITask.IDLE

    def test_after_a_run_ends_its_token_is_gone_and_the_outcome_stands(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
        ai, task = _ai(model, monkeypatch), _task()

        ai.execute_task(task)

        assert ai.cancel_task(task.id) is False
        assert task.status == AITask.COMPLETED


class TestFreshTokenPerExecution:
    def test_a_later_execution_of_a_cancelled_task_completes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        holder: list[_Withdrawal] = []
        calls = {"n": 0}

        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            calls["n"] += 1
            yield "first "
            if calls["n"] == 1:
                holder[0].under_way.set()
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    yield "more "

        ai, task = _ai(FunctionModel(stream_function=stream), monkeypatch), _task()
        holder.append(_Withdrawal(ai, task))
        with pytest.raises(AICancelled):
            ai.execute_task(task, observe=lambda event: None)
        holder[0].join()

        result = ai.execute_task(task, observe=lambda event: None)

        assert task.status == AITask.COMPLETED
        assert result.output.text == "first "


class TestOneExecutionAtATime:
    """AI owns the running state: a running task is not executed twice (#1018 D-2)."""

    def test_a_second_execution_of_a_running_task_is_refused_and_the_first_goes_on(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        under_way, release = threading.Event(), threading.Event()

        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            yield "waiting "
            under_way.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield "done"

        ai, task = _ai(FunctionModel(stream_function=stream), monkeypatch), _task()
        first: list[Any] = []
        running = threading.Thread(
            target=lambda: first.append(ai.execute_task(task, observe=lambda event: None)),
            daemon=True,
        )
        running.start()
        assert under_way.wait(5)
        told: list[AIEvent] = []

        with pytest.raises(AIRequestError, match="already running"):
            ai.execute_task(task, observe=told.append)

        assert told == []
        assert task.status == AITask.RUNNING
        release.set()
        running.join(5)
        assert task.status == AITask.COMPLETED
        assert first[0].output.text == "waiting done"
        assert ai.cancel_task(task.id) is False


class TestFailureIsStillFailure:
    def test_an_engine_failure_fails_and_is_not_told_as_cancelled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pydantic_ai.exceptions import ModelHTTPError

        async def refuses(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            raise ModelHTTPError(status_code=503, model_name="test", body="down")
            yield ""  # pragma: no cover -- makes this an async generator

        ai, task = _ai(FunctionModel(stream_function=refuses), monkeypatch), _task()
        events: list[AIEvent] = []

        with pytest.raises(AIProviderError):
            ai.execute_task(task, observe=events.append)

        assert task.status == AITask.FAILED
        assert [event.kind for event in events][-1] is AIEventKind.EXECUTION_FAILED
        assert AIEventKind.EXECUTION_CANCELLED not in [event.kind for event in events]
        assert ai.cancel_task(task.id) is False
