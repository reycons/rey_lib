"""AI records what it was sent and what came back under the caller's parent (#1305).

Deterministic: Pydantic AI's FunctionModel stands in for the engine, and the run
log is a stand-in that keeps every record appended under an explicit parent and
refuses or fails as told. What is proved: the package precedes the engine call and
is the task's contract beside the record sent; the result follows, typed by the
task; both carry the origin's run and parent and never the vehicle's; no origin
records nothing; a bad origin refuses before the engine runs; a failed write
changes nothing about the execution.
"""

from __future__ import annotations

import json

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai import ai as ai_module
from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIProviderError, AIRequestError
from rey_lib.ai.evidence import origin_of
from rey_lib.ai.task import AITask
from rey_lib.errors.error_utils import ConfigError

ORIGIN = {"run_id": 3981, "parent_run_log_id": 2849915, "nest_level": 4,
          "correlation_id": "corr-1", "app": "workflow_app"}
SUMMARY = {"record_type": "RESULTS_SUMMARY", "run_id": 3900, "status": "succeeded"}
CONTRACT = "contract:\n  name: rey_log_interpreter\n  version: 1.3.1\npurpose: Interpret.\n"


def _task(**facts: Any) -> AITask:
    given: dict[str, Any] = {
        "task_key": "log_interpretation", "profile_key": "local_testing",
        "model_identifier": "ollama:qwen3-coder-next:latest",
        "instruction_key": "log_interpreter", "contract_key": "rey_log_interpreter",
        "contract_version": "1.3.1", "contract_body": CONTRACT,
        "input": AIInput.prompt(json.dumps(SUMMARY)),
        "origin_id": json.dumps(ORIGIN),
    }
    given.update(facts)
    return AITask(**given)


class _Engine:
    """A FunctionModel that answers once and notes when it was called."""

    def __init__(self, answer: str = "It succeeded.", fails: Exception | None = None) -> None:
        self.calls = 0
        self._answer = answer
        self._fails = fails
        self.model = FunctionModel(self._reply)

    def _reply(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        if self._fails is not None:
            raise self._fails
        return ModelResponse(parts=[TextPart(self._answer)])


class _Evidence:
    """A stand-in run log: what was appended under which parent, in order."""

    def __init__(self, refuses: Exception | None = None, engine: _Engine | None = None) -> None:
        self.appended: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        self.engine_calls_at_append: list[int] = []
        self._refuses = refuses
        self._engine = engine
        self.jsonl_lines = 0

    def append_under(self, parent: Any, record_type: str, *, message: str = "",
                     **fields: Any) -> int | None:
        self.engine_calls_at_append.append(self._engine.calls if self._engine else -1)
        if self._refuses is not None:
            # As RunLog.append_under does with a failed write: reported, None.
            return None
        self.appended.append((dict(parent), record_type, dict(fields)))
        return len(self.appended)


def _ai(engine: _Engine, monkeypatch: pytest.MonkeyPatch) -> AI:
    monkeypatch.setattr(ai_module, "_MAX_OUTPUT_CORRECTIONS", 0)
    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: engine.model)
    return ai


def test_the_package_is_recorded_before_the_engine_and_the_result_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    evidence = _Evidence(engine=engine)
    task = _task()

    result = _ai(engine, monkeypatch).execute_recorded(
        task, None, evidence, source=SUMMARY, source_record_type="RESULTS_SUMMARY")

    assert result.text == "It succeeded."
    assert task.status == AITask.COMPLETED
    types = [record_type for _, record_type, _ in evidence.appended]
    assert types == ["LLM_PACKAGE", "LLM_INTERPRETATION"]
    # The package went before the engine was called, the result after.
    assert evidence.engine_calls_at_append == [0, 1]
    parent, _, package = evidence.appended[0]
    assert parent == ORIGIN
    assert package == {
        "record_group": "results",
        "analysis_name": "log_interpreter",
        "source_record_type": "RESULTS_SUMMARY",
        "instructions": {"contract_key": "rey_log_interpreter", "contract_version": "1.3.1",
                         "contract": {"name": "rey_log_interpreter", "version": "1.3.1"},
                         "purpose": "Interpret."},
        "source": SUMMARY,
    }
    parent, _, answered = evidence.appended[1]
    assert parent == ORIGIN
    assert answered["analysis_name"] == "log_interpreter"
    assert answered["output"] == "It succeeded."
    assert answered["finish_reason"] == "completed"
    assert answered["execution_id"] == result.execution.execution_id
    assert answered["record_group"] == "results"


def test_any_other_task_records_an_analysis_result(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine("select 1")
    evidence = _Evidence(engine=engine)

    _ai(engine, monkeypatch).execute_recorded(
        _task(task_key="generate_sql", instruction_key="sql_generate_sql"), None, evidence)

    assert [t for _, t, _ in evidence.appended] == ["LLM_PACKAGE", "LLM_ANALYSIS_RESULT"]
    _, _, package = evidence.appended[0]
    assert package["source"] is None and package["source_record_type"] == ""


def test_a_task_with_no_origin_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine()
    evidence = _Evidence(engine=engine)

    result = _ai(engine, monkeypatch).execute_recorded(_task(origin_id=""), None, evidence)

    assert result.text == "It succeeded."
    assert evidence.appended == []


@pytest.mark.parametrize("origin", [
    "not json", json.dumps(["a", "list"]), json.dumps({"run_id": 1}),
    json.dumps({"parent_run_log_id": 2}), json.dumps({"run_id": "x", "parent_run_log_id": 2}),
], ids=["malformed", "not a mapping", "no parent record", "no run", "not a number"])
def test_a_bad_origin_refuses_before_the_engine_runs(
    monkeypatch: pytest.MonkeyPatch, origin: str,
) -> None:
    engine = _Engine()
    evidence = _Evidence(engine=engine)
    task = _task(origin_id=origin)

    with pytest.raises(AIRequestError):
        _ai(engine, monkeypatch).execute_recorded(task, None, evidence)

    assert engine.calls == 0
    assert evidence.appended == []
    assert task.status == AITask.IDLE


def test_a_failure_records_the_package_then_the_failure_and_raises_the_same_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai.exceptions import ModelHTTPError

    engine = _Engine(fails=ModelHTTPError(status_code=503, model_name="m", body="down"))
    evidence = _Evidence(engine=engine)
    task = _task()

    with pytest.raises(AIProviderError):
        _ai(engine, monkeypatch).execute_recorded(task, None, evidence, source=SUMMARY,
                                                  source_record_type="RESULTS_SUMMARY")

    assert [t for _, t, _ in evidence.appended] == ["LLM_PACKAGE", "LLM_ANALYSIS_FAILURE"]
    _, _, failure = evidence.appended[1]
    assert failure["status"] == "failed" and failure["error"]
    assert task.status == AITask.FAILED


def test_a_failed_write_changes_nothing_about_the_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    evidence = _Evidence(refuses=ConfigError("no database output"), engine=engine)
    task = _task()

    result = _ai(engine, monkeypatch).execute_recorded(task, None, evidence, source=SUMMARY,
                                                       source_record_type="RESULTS_SUMMARY")

    assert result.text == "It succeeded."
    assert task.status == AITask.COMPLETED
    assert evidence.appended == []
    assert engine.calls == 1


def test_the_origin_is_read_as_the_callers_place_and_nothing_more() -> None:
    assert origin_of(_task()) == ORIGIN
    assert origin_of(_task(origin_id="")) is None
    assert origin_of(_task(origin_id=json.dumps({"run_id": 1, "parent_run_log_id": 2}))) == {
        "run_id": 1, "parent_run_log_id": 2, "nest_level": 0, "correlation_id": "", "app": ""}
