"""AI streams canonical events for a task to an observer (backlog 1038).

Deterministic: the model AI builds is replaced by Pydantic AI's FunctionModel
with a stream function, so no engine is reached. What is asserted is Rey's side:
Pydantic AI's events become AIEvents in order, the observer only relays, and an
observer that fails is reported once and detached while the execution goes on.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIExecutionError, AIProviderError
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.streaming import AIEvent, AIEventKind
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AITool
from rey_lib.errors.error_utils import AppError


def _task(**facts: Any) -> AITask:
    given: dict[str, Any] = {
        "task_key": "explain", "profile_key": "local_testing",
        "model_identifier": "ollama:qwen3-coder-next:latest",
        "input": AIInput.prompt("What is this?"),
    }
    given.update(facts)
    return AITask(**given)


def _tool(name: str) -> AITool:
    return AITool(name=name, description=f"The {name} tool.",
                  input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
                  replay_safe=True, capability_key=name)


def _ai(stream: Any, monkeypatch: pytest.MonkeyPatch) -> AI:
    ai = AI(registry=AIRegistry())
    model = FunctionModel(stream_function=stream)
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: model)
    return ai


async def _says(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    for piece in ("Hel", "lo ", "there"):
        yield piece


class _Observer:
    def __init__(self, fails_on: int | None = None) -> None:
        self.events: list[AIEvent] = []
        self._fails_on = fails_on

    def __call__(self, event: AIEvent) -> None:
        self.events.append(event)
        if self._fails_on is not None and len(self.events) == self._fails_on:
            raise RuntimeError("the relay broke")

    def kinds(self) -> list[AIEventKind]:
        return [event.kind for event in self.events]


class TestText:
    def test_started_then_deltas_then_completed_with_one_execution_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observer = _Observer()
        task = _task()

        result = _ai(_says, monkeypatch).execute_task(task, observe=observer)

        assert observer.kinds() == [
            AIEventKind.EXECUTION_STARTED, AIEventKind.CONTENT_DELTA,
            AIEventKind.CONTENT_DELTA, AIEventKind.CONTENT_DELTA,
            AIEventKind.EXECUTION_COMPLETED,
        ]
        assert "".join(e.text for e in observer.events[1:-1]) == "Hello there"
        assert observer.events[-1].result is result
        assert {e.execution_id for e in observer.events} == {result.execution.execution_id}
        assert task.status == AITask.COMPLETED

    def test_without_an_observer_nothing_is_streamed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def answers(messages: list[ModelMessage], info: AgentInfo) -> Any:
            from pydantic_ai.messages import ModelResponse, TextPart
            return ModelResponse(parts=[TextPart("plain")])

        ai = AI(registry=AIRegistry())
        model = FunctionModel(answers)
        monkeypatch.setattr(ai, "_model", lambda *engine_facts: model)

        result = ai.execute_task(_task())

        assert result.output.text == "plain"


class TestTools:
    @staticmethod
    def _calls(*names: str) -> Any:
        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
            if not any(isinstance(part, (ToolReturnPart, RetryPromptPart))
                       for message in messages if isinstance(message, ModelRequest)
                       for part in message.parts):
                yield {index: DeltaToolCall(name=name, json_args='{"x": 1}',
                                            tool_call_id=f"call-{name}")
                       for index, name in enumerate(names)}
            else:
                yield "done"
        return stream

    def test_a_call_and_its_result_are_told_by_id_name_and_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def lookup(arguments: Mapping[str, Any], tool: AITool) -> Any:
            return {"rows": [arguments["x"]]}

        observer = _Observer()
        _ai(self._calls("lookup"), monkeypatch).execute_task(
            _task(tools=(_tool("lookup"),)), {"lookup": lookup}, observe=observer,
        )

        requested = [e for e in observer.events if e.kind is AIEventKind.TOOL_CALL_REQUESTED]
        accepted = [e for e in observer.events if e.kind is AIEventKind.TOOL_RESULT_ACCEPTED]
        assert [(e.tool_call.id, e.tool_call.name, e.tool_call.arguments) for e in requested] \
            == [("call-lookup", "lookup", {"x": 1})]
        assert [(e.tool_result.call_id, e.tool_result.value, e.tool_result.failed)
                for e in accepted] == [("call-lookup", {"rows": [1]}, False)]

    def test_a_refused_call_is_a_failed_result_with_its_message(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(arguments: Mapping[str, Any], tool: AITool) -> Any:
            raise AppError("the owner refused")

        observer = _Observer()
        task = _task(tools=(_tool("refuse"),))
        _ai(self._calls("refuse"), monkeypatch).execute_task(
            task, {"refuse": refuse}, observe=observer,
        )

        (accepted,) = [e.tool_result for e in observer.events
                       if e.kind is AIEventKind.TOOL_RESULT_ACCEPTED]
        assert (accepted.failed, accepted.value) == (True, None)
        assert "the owner refused" in accepted.message
        assert task.status == AITask.COMPLETED

    def test_a_call_sent_back_for_correction_is_a_failed_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool = AITool(name="strict", description="Strict.",
                      input_schema={"type": "object", "properties": {"x": {"type": "string"}},
                                    "required": ["x"], "additionalProperties": False},
                      replay_safe=True, capability_key="strict")
        observer = _Observer()
        _ai(self._calls("strict"), monkeypatch).execute_task(
            _task(tools=(tool,)), {"strict": lambda arguments, tool: "never"},
            observe=observer,
        )

        (accepted,) = [e.tool_result for e in observer.events
                       if e.kind is AIEventKind.TOOL_RESULT_ACCEPTED]
        assert accepted.failed is True and accepted.message


class TestFailure:
    def test_an_engine_failure_fails_the_task_and_is_told(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pydantic_ai.exceptions import ModelHTTPError

        async def refuses(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            raise ModelHTTPError(status_code=503, model_name="test", body="down")
            yield ""  # pragma: no cover -- makes this an async generator

        observer = _Observer()
        task = _task()
        with pytest.raises(AIProviderError):
            _ai(refuses, monkeypatch).execute_task(task, observe=observer)

        assert observer.kinds() == [AIEventKind.EXECUTION_STARTED, AIEventKind.EXECUTION_FAILED]
        assert task.status == AITask.FAILED


class TestObserverIsolation:
    def test_a_failing_observer_is_reported_once_detached_and_the_run_completes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reported: list[tuple[AppError, str]] = []
        monkeypatch.setattr(
            AIExecutionError, "report",
            lambda self, context, *, source: reported.append((self, context)),
        )
        observer = _Observer(fails_on=2)
        task = _task()

        result = _ai(_says, monkeypatch).execute_task(task, observe=observer)

        assert len(observer.events) == 2
        assert task.status == AITask.COMPLETED
        assert task.answer is result
        assert result.output.text == "Hello there"
        assert len(reported) == 1
        error, context = reported[0]
        assert isinstance(error.cause, RuntimeError)
        assert error.__cause__ is error.cause
        assert context == "AI task 'explain' event notification"

    def test_an_observer_failing_on_started_still_lets_the_run_complete(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(AIExecutionError, "report", lambda self, context, *, source: None)
        observer = _Observer(fails_on=1)
        task = _task()

        _ai(_says, monkeypatch).execute_task(task, observe=observer)

        assert observer.kinds() == [AIEventKind.EXECUTION_STARTED]
        assert task.status == AITask.COMPLETED
