"""#1337: shared, scoped AI instructions reach the task before its own contract.

The installation's configuration row (ai.ai_installation_vw.instructions) states which
shared instructions apply, in order; their bodies are the instruction rows hydration
already reads (control.f_ai_instruction_get). AITask.system_instructions() composes
them before the task's contract, and both engines send exactly that.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai import ai as ai_module
from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.task import AITask
from tests.ai_answers import enveloped
from tests.test_ai_task_hydration import CONFIGURATION, ENGINES, INSTRUCTIONS

GOVERNANCE = {"instruction_key": "development_governance", "kind": "contract",
              "contract_key": "development_governance", "contract_version": "1.0.0",
              "body": "Read and follow the development rules and recipes."}
STYLE = {"instruction_key": "house_style", "kind": "contract",
         "contract_key": "house_style", "contract_version": "2.1.0",
         "body": "Answer plainly."}


def _shared(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    """The view's instructions list for the given rows, in the order given."""
    return [{"instruction_key": row["instruction_key"], "instruction_kind": row["kind"],
             "contract_key": row["contract_key"], "contract_version": row["contract_version"],
             "sort_order": order}
            for order, row in enumerate(rows, start=1)]


def _configuration(shared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = deepcopy(CONFIGURATION)
    rows[0]["instructions"] = shared
    return rows


def _hydrate(shared: list[dict[str, Any]], **choices: Any) -> AITask:
    return AITask.hydrate(_configuration(shared), ENGINES, [*INSTRUCTIONS, GOVERNANCE, STYLE],
                          installation="local", **choices)


class TestHydration:
    def test_the_rows_shared_instructions_are_carried_in_order_with_their_bodies(self) -> None:
        task = _hydrate(_shared(GOVERNANCE, STYLE), task_key="search_index")

        assert [dict(one) for one in task.instructions] == [
            {"instruction_key": "development_governance", "contract_key": "development_governance",
             "contract_version": "1.0.0", "body": GOVERNANCE["body"]},
            {"instruction_key": "house_style", "contract_key": "house_style",
             "contract_version": "2.1.0", "body": STYLE["body"]},
        ]

    def test_the_task_contract_is_unchanged_by_them(self) -> None:
        task = _hydrate(_shared(GOVERNANCE), task_key="search_index")

        assert (task.instruction_key, task.contract_key, task.contract_body) == (
            "explain_v1", "explain", "Explain it in Markdown.")

    def test_a_row_without_shared_instructions_has_none(self) -> None:
        assert AITask.hydrate(CONFIGURATION, ENGINES, INSTRUCTIONS, installation="local",
                              task_key="search_index").instructions == ()

    def test_the_default_scope_and_an_unconfigured_task_have_none(self) -> None:
        assert _hydrate(_shared(GOVERNANCE)).instructions == ()
        assert _hydrate(_shared(GOVERNANCE), task_key="not_configured").instructions == ()

    def test_an_unconfigured_shared_instruction_is_refused(self) -> None:
        missing = dict(GOVERNANCE, instruction_key="nowhere")
        with pytest.raises(AIConfigurationError, match="nowhere"):
            AITask.hydrate(_configuration(_shared(missing)), ENGINES, INSTRUCTIONS,
                           installation="local", task_key="search_index")

    def test_a_shared_instruction_without_a_body_is_refused(self) -> None:
        empty = dict(GOVERNANCE, body="")
        with pytest.raises(AIConfigurationError, match="carries no body"):
            AITask.hydrate(_configuration(_shared(GOVERNANCE)), ENGINES, [*INSTRUCTIONS, empty],
                           installation="local", task_key="search_index")

    def test_rehydration_reads_them_again(self) -> None:
        class _Control:
            def __init__(self) -> None:
                self.configuration = _configuration(_shared(GOVERNANCE))

            def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
                return deepcopy(self.configuration)

            def ai_engine_profiles(self, installation: str) -> list[dict[str, Any]]:
                return deepcopy(ENGINES)

            def ai_instructions(self) -> list[dict[str, Any]]:
                return [*deepcopy(INSTRUCTIONS), GOVERNANCE, STYLE]

        ai, control = AI(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index")
        control.configuration[0]["instructions"] = _shared(STYLE, GOVERNANCE)
        second = ai.rehydrate_task(control, first.id, {})

        assert [one["instruction_key"] for one in first.instructions] == ["development_governance"]
        assert [one["instruction_key"] for one in second.instructions] == [
            "house_style", "development_governance"]


class TestComposition:
    def test_shared_instructions_come_first_then_the_contract(self) -> None:
        task = _hydrate(_shared(GOVERNANCE, STYLE), task_key="search_index")

        assert task.system_instructions() == (
            "Read and follow the development rules and recipes.\n\n"
            "Answer plainly.\n\n"
            "Explain it in Markdown.")

    def test_without_shared_instructions_it_is_the_contract_alone(self) -> None:
        task = AITask.hydrate(CONFIGURATION, ENGINES, INSTRUCTIONS, installation="local",
                              task_key="search_index")

        assert task.system_instructions() == task.contract_body == "Explain it in Markdown."

    def test_a_task_without_a_contract_is_given_the_shared_instructions_alone(self) -> None:
        task = _hydrate(_shared(GOVERNANCE), task_key="search_index", instruction_key="__none__")

        assert task.contract_body == ""
        assert task.system_instructions() == GOVERNANCE["body"]


class TestProjection:
    def test_it_names_which_applied_and_their_versions_never_their_bodies(self) -> None:
        projection = _hydrate(_shared(GOVERNANCE), task_key="search_index").projection()

        assert projection["instructions"] == [{
            "instruction_key": "development_governance",
            "contract_key": "development_governance", "contract_version": "1.0.0"}]
        assert GOVERNANCE["body"] not in repr(projection)


class TestBothEnginesSendIt:
    def test_pydantic_ai_is_given_the_composed_instructions(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[AgentInfo] = []

        def answer(messages: Any, info: AgentInfo) -> ModelResponse:
            seen.append(info)
            return ModelResponse(parts=[TextPart(enveloped("done"))])

        monkeypatch.setattr(ai_module, "_MAX_OUTPUT_CORRECTIONS", 0)
        ai = AI()
        monkeypatch.setattr(ai, "_model", lambda *facts: FunctionModel(answer))
        task = _hydrate(_shared(GOVERNANCE), task_key="search_index",
                        input=AIInput.prompt("What is this?"))

        ai.execute_task(task)

        assert seen[0].instructions == task.system_instructions()
        assert seen[0].instructions.startswith(GOVERNANCE["body"])

    def test_the_claude_agent_sdk_is_given_the_composed_instructions(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_ai_claude_agent_sdk import _ai, _result, _Sessions, _task

        sessions = _Sessions([_result("done")])
        ai = _ai(sessions, monkeypatch)
        task = _task(instructions=(
            {"instruction_key": "development_governance", "contract_key": "development_governance",
             "contract_version": "1.0.0", "body": GOVERNANCE["body"]},))

        ai.execute_task(task)

        assert sessions.options[0].system_prompt == (
            GOVERNANCE["body"] + "\n\n" + "Explain it.")
