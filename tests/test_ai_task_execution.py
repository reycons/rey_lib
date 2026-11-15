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


#: The answer run 7322 gave (#1396): a valid envelope behind narration, in a code fence.
_NARRATED = ("I have enough from my queries. Let me compose it.\n\n```json\n"
             + enveloped("# Slides") + "\n```")


class _Investigating:
    """A FunctionModel that calls a tool once, then answers in turn (#1396)."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.requests = 0
        self.retry_prompts: list[str] = []
        self.model = FunctionModel(self._answer)

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        for part in messages[-1].parts:
            if type(part).__name__ == "RetryPromptPart":
                self.retry_prompts.append(str(part.content))
        if self.requests == 1:
            return ModelResponse(parts=[ToolCallPart("run_query", {"sql": "select 1"})])
        return ModelResponse(parts=[TextPart(self._answers.pop(0))])


class TestEnvelopeCorrection:
    """#1396 on the Pydantic AI path: one correction, the tool call not repeated."""

    @staticmethod
    def _prepared(engine: _Investigating, monkeypatch: pytest.MonkeyPatch,
                  **facts: Any) -> tuple[AI, list[dict[str, Any]], AITask, dict[str, Any]]:
        from rey_lib.ai.tools import AITool  # noqa: PLC0415

        tool = AITool(name="run_query", description="Run one read query.",
                      input_schema={"type": "object",
                                    "properties": {"sql": {"type": "string"}},
                                    "required": ["sql"]},
                      replay_safe=False, capability_key="database.query")
        calls: list[dict[str, Any]] = []
        ai = AI()
        monkeypatch.setattr(ai, "_model", lambda *engine_facts: engine.model)
        operations = {"database.query":
                      lambda arguments, tool: calls.append(dict(arguments)) or {"rows": 1}}
        return ai, calls, _task(tools=(tool,), **facts), operations

    def test_a_malformed_envelope_then_a_valid_one_completes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _Investigating(_NARRATED, enveloped("# Slides"))
        ai, calls, task, operations = self._prepared(engine, monkeypatch)

        result = ai.execute_task(task, operations)

        assert result.text == "# Slides"
        assert task.status == AITask.COMPLETED
        assert calls == [{"sql": "select 1"}]          # the tool call is not repeated
        assert engine.requests == 3                    # tool, refused answer, correction
        assert any("Return only the JSON envelope: nothing before or after it, and no code "
                   "fence around it." in prompt for prompt in engine.retry_prompts)
        attempts = result.execution.attempts
        assert [one.failed for one in attempts] == [True, False]
        assert attempts[0].raw_output == _NARRATED
        assert attempts[1].raw_output == enveloped("# Slides")

    def test_two_malformed_envelopes_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Investigating(_NARRATED, _NARRATED)
        ai, calls, task, operations = self._prepared(engine, monkeypatch)

        with pytest.raises(AIOutputError):
            ai.execute_task(task, operations)

        assert task.status == AITask.FAILED
        assert calls == [{"sql": "select 1"}]

    def test_a_correction_counts_against_the_governed_turn_limit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two requests allowed: the tool call and the refused answer use both, so the
        # correction would be a third and the run stops at its budget.
        engine = _Investigating(_NARRATED, enveloped("# Slides"))
        ai, _calls, task, operations = self._prepared(engine, monkeypatch, max_turns=2)

        with pytest.raises(AIExecutionError, match="usage limit"):
            ai.execute_task(task, operations)

        assert engine.requests == 2
