"""AI holds its session's AITasks and edits them by rehydration (backlog 1037).

An edit is replacement, never mutation: the stored choices change, hydration
resolves the whole task again from the governed rows, and the new idle task
stands under the same id. No live database: a Control stands in for the three
governed reads, with rows a test may change between hydrations.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIRequestError, AISelectionError
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import AIOutputKind, AIOutputSpec
from rey_lib.ai.task import AITask

CONFIGURATION = [
    {"row_type": "group", "task_key": None, "task_label": None,
     "profile_key": "local_testing", "instruction_key": "__none__", "temperature": 0.2,
     "representation": "redacted", "connection_name": None, "composed_first": False,
     "is_enabled": True},
    {"row_type": "task", "task_key": "search_index", "task_label": "Search the index",
     "profile_key": "local_testing", "instruction_key": "explain_v1", "temperature": 0,
     "representation": "redacted", "connection_name": "llm_reader",
     "composed_first": True, "is_enabled": True},
    {"row_type": "task", "task_key": "explain", "task_label": "Explain",
     "profile_key": None, "instruction_key": "explain_v1", "temperature": None,
     "representation": None, "connection_name": None, "composed_first": False,
     "is_enabled": True},
]

ENGINES = [
    {"profile_key": "local_testing", "model_identifier": "ollama:qwen3-coder-next:latest",
     "base_url": "http://localhost:11434/v1", "credential_ref": "", "timeout_seconds": 120,
     "access_allowed": ["redacted", "unredacted"], "access_default": "redacted",
     "is_enabled": True},
    {"profile_key": "anthropic", "model_identifier": "anthropic:claude-sonnet-4-6",
     "base_url": None, "credential_ref": "env.ANTHROPIC_API_KEY", "timeout_seconds": None,
     "access_allowed": ["redacted"], "access_default": "redacted", "is_enabled": True},
]

INSTRUCTIONS = [
    {"instruction_key": "__none__", "kind": "none", "contract_key": None,
     "contract_version": None, "body": None},
    {"instruction_key": "explain_v1", "kind": "contract", "contract_key": "explain",
     "contract_version": "1", "body": "Explain it in Markdown."},
]


class _Control:
    """Stands in for the runtime's Control; its configuration rows may change."""

    def __init__(self) -> None:
        self.configuration = copy.deepcopy(CONFIGURATION)

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        return self.configuration

    def ai_engine_profiles(self) -> list[dict[str, Any]]:
        return ENGINES

    def ai_instructions(self) -> list[dict[str, Any]]:
        return INSTRUCTIONS


def _ai() -> AI:
    return AI(registry=AIRegistry())


class TestHolding:
    def test_a_composed_task_is_held_idle_under_its_id(self) -> None:
        ai = _ai()

        task = ai.hydrate_task(_Control(), "local", task_key="search_index",
                               input=AIInput.prompt("hi"))

        assert task.status == AITask.IDLE
        assert ai.task(task.id) is task
        assert task.connection == "llm_reader"

    def test_an_unknown_id_addresses_nothing(self) -> None:
        assert _ai().task("no-such-task") is None

    def test_only_given_overrides_are_choices(self) -> None:
        task = _ai().hydrate_task(_Control(), "local", task_key="search_index",
                                  profile_key="anthropic", representation="",
                                  temperature=0.0)

        assert dict(task.choices) == {"profile_key": "anthropic", "temperature": 0.0}
        assert task.representation == "redacted"

    def test_an_identity_is_never_given(self) -> None:
        with pytest.raises(AIRequestError, match="task_id"):
            _ai().hydrate_task(_Control(), "local", task_id="chosen")


class TestRehydration:
    def test_an_edit_replaces_the_task_under_the_same_id(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index")

        second = ai.rehydrate_task(control, first.id, {"profile_key": "anthropic"})

        assert second is not first
        assert second.id == first.id
        assert ai.task(first.id) is second
        assert second.status == AITask.IDLE
        assert second.model_identifier == "anthropic:claude-sonnet-4-6"
        assert first.model_identifier == "ollama:qwen3-coder-next:latest"

    def test_an_earlier_choice_survives_an_edit_of_another(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                profile_key="anthropic")

        second = ai.rehydrate_task(control, first.id, {"temperature": 0.7})

        assert dict(second.choices) == {"profile_key": "anthropic", "temperature": 0.7}
        assert second.model_identifier == "anthropic:claude-sonnet-4-6"
        assert second.temperature == 0.7

    def test_an_empty_change_clears_a_choice_back_to_the_governed_value(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                profile_key="anthropic")

        second = ai.rehydrate_task(control, first.id, {"profile_key": ""})

        assert dict(second.choices) == {}
        assert second.profile_key == "local_testing"

    def test_an_unchosen_value_follows_a_governed_change(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="explain", temperature=0.5)
        control.configuration[0]["profile_key"] = "anthropic"
        control.configuration[0]["representation"] = "unredacted"

        second = ai.rehydrate_task(control, first.id, {"temperature": 0.9})

        assert first.profile_key == "local_testing"
        assert second.profile_key == "anthropic"
        assert second.representation == "unredacted"
        assert second.temperature == 0.9

    def test_runtime_facts_are_kept_unless_changed(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                input=AIInput.prompt("first"), subject="SELECT 1",
                                subject_reference={"workflow_id": "7"}, origin_id="node-7")

        second = ai.rehydrate_task(control, first.id,
                                   {"subject": "SELECT 2",
                                    "subject_reference": {"workflow_id": "8"}})

        assert second.input is first.input
        assert (second.subject, dict(second.subject_reference), second.origin_id) == (
            "SELECT 2", {"workflow_id": "8"}, "node-7")
        assert second.task_key == "search_index"

    def test_an_edit_may_choose_another_task(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index")

        second = ai.rehydrate_task(control, first.id, {"task_key": "explain"})

        assert (second.id, second.task_key, second.connection) == (first.id, "explain", "")

    def test_the_governed_configuration_is_never_changed(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index")

        ai.rehydrate_task(control, first.id, {"profile_key": "anthropic"})

        assert control.configuration == CONFIGURATION


class TestOutputIsAChoice:
    """Backlog 1047, D4: hydration derives the output; a stated one is a choice."""

    def test_hydration_derives_markdown_for_a_task_without_a_contract(self) -> None:
        task = _ai().hydrate_task(_Control(), "local")

        assert task.instruction_key == "__none__"
        assert (task.output.kind, task.output.media_type) == (AIOutputKind.TEXT, "text/markdown")
        assert "output" not in task.choices

    def test_hydration_derives_plain_text_for_a_contract_backed_task(self) -> None:
        task = _ai().hydrate_task(_Control(), "local", task_key="search_index")

        assert (task.output.kind, task.output.media_type) == (AIOutputKind.TEXT, "")

    def test_a_derived_output_follows_an_edit_to_another_kind_of_task(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local")

        second = ai.rehydrate_task(control, first.id, {"task_key": "search_index"})

        assert first.output.media_type == "text/markdown"
        assert second.output.media_type == ""
        assert "output" not in second.choices

    def test_a_stated_output_survives_an_unrelated_edit(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                output=AIOutputSpec.markdown())

        second = ai.rehydrate_task(control, first.id, {"temperature": 0.4})

        assert second.output.media_type == "text/markdown"
        assert second.choices["output"] == AIOutputSpec.markdown()

    def test_clearing_a_stated_output_hands_it_back_to_hydration(self) -> None:
        ai, control = _ai(), _Control()
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                output=AIOutputSpec.markdown())

        second = ai.rehydrate_task(control, first.id, {"output": None})

        assert second.output.media_type == ""
        assert "output" not in second.choices


class TestRefusals:
    def test_an_unknown_id_is_refused(self) -> None:
        with pytest.raises(AISelectionError, match="holds no AI task"):
            _ai().rehydrate_task(_Control(), "no-such-task", {"temperature": 0.1})

    def test_a_running_task_is_not_edited(self) -> None:
        ai, control = _ai(), _Control()
        task = ai.hydrate_task(control, "local", task_key="search_index")
        task.started()

        with pytest.raises(AIRequestError, match="is running"):
            ai.rehydrate_task(control, task.id, {"temperature": 0.1})
        assert ai.task(task.id) is task

    def test_a_change_a_task_does_not_take_is_refused(self) -> None:
        ai, control = _ai(), _Control()
        task = ai.hydrate_task(control, "local", task_key="search_index")

        with pytest.raises(AIRequestError, match="model_identifier"):
            ai.rehydrate_task(control, task.id, {"model_identifier": "openai:gpt-4o"})

    def test_choices_and_facts_cannot_be_written(self) -> None:
        task = _ai().hydrate_task(_Control(), "local", task_key="search_index",
                                  profile_key="anthropic")

        with pytest.raises(TypeError):
            task.choices["profile_key"] = "local_testing"  # type: ignore[index]
        with pytest.raises(AttributeError):
            task.profile_key = "local_testing"  # type: ignore[misc]
