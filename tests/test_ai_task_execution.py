"""AI executes a hydrated AITask through Pydantic AI (backlog 1020).

Deterministic: the model AI builds is replaced by Pydantic AI's FunctionModel, so
no engine is reached. What is asserted is Rey's side of the contract -- what is
sent, which output mode is asked for, how OutputParser validates and corrects,
how failures become Rey's own errors, and how the task's lifecycle moves.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pydantic_ai
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.artifacts.envelope import build_envelope_instruction
from tests.ai_answers import enveloped
from rey_lib.ai.content import AIContent, AIContentKind, AIInput
from rey_lib.ai.errors import (
    AIExecutionError,
    AIOutputError,
    AIProviderError,
    AIRequestError,
)
from rey_lib.ai import ai as ai_module
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.task import AITask

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _task(**facts: Any) -> AITask:
    given: dict[str, Any] = {
        "task_key": "explain", "profile_key": "local_testing",
        "model_identifier": "ollama:qwen3-coder-next:latest",
        "base_url": "http://localhost:11434/v1",
        "instruction_key": "explain_v1", "contract_body": "Explain it.",
        "temperature": 0.0, "timeout_seconds": 30.0,
        "input": AIInput.prompt("What is this?"),
    }
    given.update(facts)
    return AITask(**given)


class _Engine:
    """A FunctionModel that answers in turn and records what it was sent."""

    def __init__(self, *answers: str, json_schema: bool = False,
                 fails: Exception | None = None,
                 final_result: dict[str, Any] | None = None) -> None:
        self._answers = list(answers)
        self._fails = fails
        # Where given, the model answers by calling the final_result output tool.
        self._final_result = final_result
        self.seen: list[AgentInfo] = []
        self.prompts: list[str] = []
        self.model = FunctionModel(
            self._answer, profile={"supports_json_schema_output": json_schema},
        )

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(info)
        self.prompts.append(str(messages[0].parts[-1].content))
        if self._fails is not None:
            raise self._fails
        if self._final_result is not None:
            return ModelResponse(parts=[ToolCallPart("final_result", dict(self._final_result))])
        return ModelResponse(parts=[TextPart(self._answers.pop(0))])


def _ai(engine: _Engine, monkeypatch: pytest.MonkeyPatch, corrections: int = 0) -> AI:
    monkeypatch.setattr(ai_module, "_MAX_OUTPUT_CORRECTIONS", corrections)
    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: engine.model)
    return ai


class TestText:
    def test_a_text_task_is_answered_and_the_task_records_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine(enveloped("ready"))
        task = _task()

        result = _ai(engine, monkeypatch).execute_task(task)

        assert result.text == "ready"
        assert task.status == AITask.COMPLETED
        assert task.answer is result
        assert result.execution.profile_id == "local_testing"
        assert result.execution.model == "ollama:qwen3-coder-next:latest"

    def test_the_contract_settings_and_subject_are_what_is_sent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine(enveloped("ready"))

        _ai(engine, monkeypatch).execute_task(_task(subject="SELECT 1"))

        assert engine.seen[0].instructions == "Explain it."
        assert engine.seen[0].model_settings["temperature"] == 0.0
        assert engine.seen[0].model_settings["timeout"] == 30.0
        assert engine.prompts[0] == "SELECT 1\n\nWhat is this?" + build_envelope_instruction()

    def test_markdown_is_answered_as_markdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Engine(enveloped("# Title", "markdown"))

        result = _ai(engine, monkeypatch).execute_task(_task(output=AIOutputSpec.markdown()))

        assert result.media_type == "text/markdown"


class TestStructured:
    @pytest.mark.parametrize("json_schema", [True, False])
    def test_every_engine_is_offered_the_answer_as_a_tool_beside_text(
        self, monkeypatch: pytest.MonkeyPatch, json_schema: bool,
    ) -> None:
        """#1344: one contract whatever the engine's profile says -- the answer is a
        final_result tool, text is also allowed (tool_choice auto), and no response
        format is asked for, so the task's tools stay callable."""
        engine = _Engine('{"answer": "ready"}', json_schema=json_schema)

        result = _ai(engine, monkeypatch).execute_task(
            _task(output=AIOutputSpec.schema_of(SCHEMA)),
        )

        parameters = engine.seen[0].model_request_parameters
        assert parameters.output_mode == "tool"
        assert parameters.allow_text_output is True
        assert [tool.name for tool in engine.seen[0].output_tools] == ["final_result"]
        assert result.value == {"answer": "ready"}

    def test_an_answer_through_the_final_result_tool_is_the_tasks_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine(final_result={"answer": "ready"})

        result = _ai(engine, monkeypatch).execute_task(
            _task(output=AIOutputSpec.schema_of(SCHEMA)),
        )

        assert result.value == {"answer": "ready"}

    def test_a_final_result_that_breaks_the_schema_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine(final_result={"wrong": 1})
        task = _task(output=AIOutputSpec.schema_of(SCHEMA))

        with pytest.raises(AIOutputError):
            _ai(engine, monkeypatch, corrections=0).execute_task(task)

        assert task.status == AITask.FAILED

    def test_json_without_a_schema_is_decoded_by_output_parser(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine('Here it is: [1, 2, 3]')

        result = _ai(engine, monkeypatch).execute_task(_task(output=AIOutputSpec.json()))

        assert result.value == [1, 2, 3]

    def test_output_parser_corrects_within_the_allowance(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine('{"wrong": 1}', '{"answer": "ready"}', json_schema=False)

        result = _ai(engine, monkeypatch, corrections=1).execute_task(
            _task(output=AIOutputSpec.schema_of(SCHEMA)),
        )

        assert result.value == {"answer": "ready"}
        assert len(engine.seen) == 2

    def test_output_that_never_satisfies_its_contract_fails_the_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Engine('{"wrong": 1}', json_schema=False)
        task = _task(output=AIOutputSpec.schema_of(SCHEMA))

        with pytest.raises(AIOutputError):
            _ai(engine, monkeypatch, corrections=0).execute_task(task)

        assert task.status == AITask.FAILED


class TestRefusalsAndFailures:
    def test_an_engine_failure_is_a_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Engine(fails=ModelHTTPError(503, "ollama:qwen3-coder-next:latest"))
        task = _task()

        with pytest.raises(AIProviderError):
            _ai(engine, monkeypatch).execute_task(task)

        assert task.status == AITask.FAILED

    def test_inside_a_running_event_loop_it_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Engine(enveloped("ready"))
        task = _task()

        async def inside() -> None:
            _ai(engine, monkeypatch).execute_task(task)

        with pytest.raises(AIExecutionError, match="running event loop"):
            asyncio.run(inside())
        assert task.status == AITask.IDLE
        assert engine.seen == []

    def test_a_structured_part_is_sent_as_its_json_before_the_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1334: the live object's context reaches the model; text-only is unchanged."""
        engine = _Engine(enveloped("ready"), enveloped("ready"))
        context = {"source": {"connection": "file_reader", "provider": "duckdb"}}
        given = AIInput.of(
            AIContent(kind=AIContentKind.TEXT, value="fix this"),
            AIContent(kind=AIContentKind.STRUCTURED, value=context),
        )
        ai = _ai(engine, monkeypatch)

        ai.execute_task(_task(input=given))
        ai.execute_task(_task())

        import json
        assert engine.prompts[0] == (json.dumps(context, indent=2) + "\n\nfix this"
                                 + build_envelope_instruction())
        assert engine.prompts[1] == "What is this?" + build_envelope_instruction()

    def test_input_other_than_text_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Engine(enveloped("ready"))
        image = AIContent(kind=AIContentKind.IMAGE, value="aGk=")

        with pytest.raises(AIRequestError, match="other than text"):
            _ai(engine, monkeypatch).execute_task(_task(input=AIInput.of(image)))


class TestStdout:
    def test_nothing_reaches_stdout_and_the_banner_is_off(
        self, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(pydantic_ai, "BANNER_ENABLED", True)
        engine = _Engine(enveloped("ready"))

        _ai(engine, monkeypatch).execute_task(_task())

        assert capfd.readouterr().out == ""
        assert pydantic_ai.BANNER_ENABLED is False


@pytest.mark.parametrize("limit", [1, 17])
def test_pydantic_execution_receives_the_task_budget(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    from pydantic_ai import Agent
    original = Agent.run_sync
    observed = []
    def run(agent, *args, **kwargs):
        observed.append(kwargs["usage_limits"].request_limit)
        return original(agent, *args, **kwargs)
    monkeypatch.setattr(Agent, "run_sync", run)
    _ai(_Engine(enveloped("ready")), monkeypatch).execute_task(_task(max_turns=limit))
    assert observed == [limit]
