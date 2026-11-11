"""#1048: governed contract shape reaches the existing live AI execution path."""
from copy import deepcopy

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError, AIOutputError
from rey_lib.ai.requests import AIOutputKind, AIOutputSpec
from rey_lib.ai.task import AITask
from tests.test_ai_task_hydration import CONFIGURATION, ENGINES, INSTRUCTIONS

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


class _Control:
    def __init__(self, schema=SCHEMA):
        self.instructions = deepcopy(INSTRUCTIONS)
        self.instructions[1]["output_schema"] = deepcopy(schema)

    def ai_configuration(self, installation):
        return deepcopy(CONFIGURATION)

    def ai_engine_profiles(self, installation):
        return deepcopy(ENGINES)

    def ai_instructions(self):
        return self.instructions


def _task(ai, control, **choices):
    return ai.hydrate_task(control, "local", instruction_key="explain_v1",
                           input=AIInput.prompt("Explain it."), **choices)


def test_live_ai_hydrates_schema_without_a_caller_output_choice():
    control = _Control()
    task = _task(AI(), control)
    assert task.output.kind is AIOutputKind.CONTRACT
    assert task.output_schema == SCHEMA
    control.instructions[1]["output_schema"]["required"].clear()
    assert task.output_schema["required"] == ["answer"]
    assert "output" not in task.choices


@pytest.mark.parametrize("schema", [None, {}])
def test_absent_schema_stays_text_but_an_explicit_empty_schema_is_structured(schema):
    task = _task(AI(), _Control(schema))
    assert task.output.kind is (AIOutputKind.TEXT if schema is None else AIOutputKind.CONTRACT)
    assert task.output_schema == schema


@pytest.mark.parametrize("schema", [[], "json", {"type": "wrong"}])
def test_invalid_governed_schema_is_a_configuration_refusal(schema):
    with pytest.raises(AIConfigurationError, match="output schema"):
        _task(AI(), _Control(schema))


def test_noncontract_instruction_cannot_declare_a_contract_shape():
    control = _Control()
    control.instructions[0]["output_schema"] = SCHEMA
    with pytest.raises(AIConfigurationError, match="output schema"):
        AI().hydrate_task(control, "local")


def test_explicit_caller_output_choice_retains_existing_precedence():
    chosen = AIOutputSpec.json()
    task = _task(AI(), _Control(), output=chosen)
    assert task.output == chosen
    assert task.output_schema == SCHEMA


@pytest.mark.parametrize("native", [True, False])
def test_governed_shape_is_validated_in_existing_ai_execution(monkeypatch, native):
    ai = AI()
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"answer":"ready"}')]),
                          profile={"supports_json_schema_output": native})
    monkeypatch.setattr(ai, "_model", lambda *args: model)
    task = _task(ai, _Control())
    result = ai.execute_task(task)
    assert result.value == {"answer": "ready"}
    assert result.media_type == "application/json"
    assert task.status == AITask.COMPLETED


def test_wrong_governed_shape_fails_through_existing_output_validation(monkeypatch):
    from rey_lib.ai import ai as module

    monkeypatch.setattr(module, "_MAX_OUTPUT_CORRECTIONS", 0)
    ai = AI()
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"wrong":1}')]),
                          profile={"supports_json_schema_output": False})
    monkeypatch.setattr(ai, "_model", lambda *args: model)
    task = _task(ai, _Control())
    with pytest.raises(AIOutputError):
        ai.execute_task(task)
    assert task.status == AITask.FAILED


def test_rehydration_reads_the_new_governed_shape_and_keeps_identity():
    ai, control = AI(), _Control()
    first = _task(ai, control)
    control.instructions[1]["output_schema"] = {"type": "array"}
    second = ai.rehydrate_task(control, first.id, {})
    assert second.id == first.id
    assert second.output_schema == {"type": "array"}
    assert first.output_schema == SCHEMA
