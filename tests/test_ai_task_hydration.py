"""AITask hydration: governed rows -> one immutable AITask (backlog 1022).

Fixture rows have exactly the shape of control.f_ai_configuration_get,
control.f_ai_engine_profile_get and control.f_ai_instruction_get as backlog 1022
extends them. No live database: ``from_governed`` is exercised with a Control
standing in for the three reads.
"""

from __future__ import annotations

import pytest

from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.instructions import AIInstructionKind
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.task import AITask

CONFIGURATION = [
    {"row_type": "group", "ai_group_key": "console_default", "label": "Console",
     "is_enabled": True, "sort_order": 0, "task_key": None, "task_label": None,
     "profile_key": "local_testing", "instruction_key": "__none__", "temperature": 0.2,
     "representation": "redacted", "opens_workbench": False, "connection_name": None,
     "composed_first": False},
    {"row_type": "task", "ai_group_key": "console_default", "label": "Console",
     "is_enabled": True, "sort_order": 1, "task_key": "search_index",
     "task_label": "Search the index", "profile_key": "local_testing",
     "instruction_key": "explain_v1", "temperature": 0, "representation": "redacted",
     "opens_workbench": True, "connection_name": "llm_reader", "composed_first": True},
    {"row_type": "task", "ai_group_key": "console_default", "label": "Console",
     "is_enabled": False, "sort_order": 2, "task_key": "retired",
     "task_label": "Retired", "profile_key": "local_testing", "instruction_key": None,
     "temperature": None, "representation": None, "opens_workbench": False,
     "connection_name": None, "composed_first": False},
]

ENGINES = [
    {"profile_key": "local_testing", "provider": "ollama",
     "model": "qwen3-coder-next:latest", "credential_ref": "", "endpoint":
     "http://localhost:11434", "timeout_seconds": 120, "temperature": None,
     "access_allowed": ["redacted", "unredacted"], "access_default": "redacted",
     "is_enabled": True, "options": {},
     "model_identifier": "ollama:qwen3-coder-next:latest",
     "base_url": "http://localhost:11434/v1"},
    {"profile_key": "anthropic", "provider": "anthropic", "model": "claude-sonnet-4-6",
     "credential_ref": "env.ANTHROPIC_API_KEY", "endpoint": "", "timeout_seconds": None,
     "temperature": None, "access_allowed": ["redacted"], "access_default": "redacted",
     "is_enabled": True, "options": {},
     "model_identifier": "anthropic:claude-sonnet-4-6", "base_url": None},
    {"profile_key": "switched_off", "provider": "openai", "model": "gpt-4o",
     "credential_ref": "", "endpoint": "", "timeout_seconds": None, "temperature": None,
     "access_allowed": [], "access_default": "", "is_enabled": False, "options": {},
     "model_identifier": "openai-chat:gpt-4o", "base_url": None},
]

INSTRUCTIONS = [
    {"instruction_key": "__none__", "kind": "none", "contract_key": None,
     "contract_version": None, "body": None},
    {"instruction_key": "explain_v1", "kind": "contract", "contract_key": "explain",
     "contract_version": "1", "body": "Explain it in Markdown."},
    {"instruction_key": "empty_contract", "kind": "contract", "contract_key": "empty",
     "contract_version": "1", "body": ""},
]


def _hydrate(**choices: object) -> AITask:
    return AITask.hydrate(CONFIGURATION, ENGINES, INSTRUCTIONS,
                          installation="local", **choices)


class TestScopes:
    def test_no_task_takes_the_group_default(self) -> None:
        task = _hydrate()

        assert task.task_key == ""
        assert task.profile_key == "local_testing"
        assert task.instruction_kind is AIInstructionKind.NONE
        assert task.temperature == 0.2
        assert task.connection == ""
        assert task.composed_first is False

    def test_a_task_row_states_what_applies_to_it(self) -> None:
        task = _hydrate(task_key="search_index")

        assert task.label == "Search the index"
        assert task.instruction_key == "explain_v1"
        assert task.connection == "llm_reader"
        assert task.composed_first is True

    def test_composed_first_is_read_and_the_legacy_column_is_not(self) -> None:
        rows = [dict(CONFIGURATION[1], opens_workbench=True, composed_first=False)]

        task = AITask.hydrate(rows, ENGINES, INSTRUCTIONS, installation="local",
                              task_key="search_index")

        assert task.composed_first is False

    def test_a_stated_zero_temperature_is_kept(self) -> None:
        assert _hydrate(task_key="search_index").temperature == 0.0

    def test_a_task_no_row_configures_takes_the_default_scope(self) -> None:
        task = _hydrate(task_key="not_configured")

        assert task.profile_key == "local_testing"
        assert task.temperature == 0.2


class TestOverridesAreHydrationInputs:
    def test_the_callers_choices_win(self) -> None:
        task = _hydrate(task_key="search_index", profile_key="anthropic",
                        instruction_key="__none__", temperature=0.7)

        assert task.model_identifier == "anthropic:claude-sonnet-4-6"
        assert task.instruction_kind is AIInstructionKind.NONE
        assert task.temperature == 0.7
        # Not overridden: still the task's own.
        assert task.connection == "llm_reader"


class TestFacts:
    def test_the_engine_is_carried_in_pydantic_ai_vocabulary(self) -> None:
        task = _hydrate(task_key="search_index")

        assert task.model_identifier == "ollama:qwen3-coder-next:latest"
        assert task.base_url == "http://localhost:11434/v1"
        assert task.timeout_seconds == 120.0
        assert task.access_allowed == ("redacted", "unredacted")
        assert task.access_default == "redacted"

    def test_an_engine_without_a_base_url_is_the_provider_default(self) -> None:
        task = _hydrate(profile_key="anthropic")

        assert task.base_url == ""
        assert task.credential_ref == "env.ANTHROPIC_API_KEY"

    def test_the_contract_travels_with_its_instruction(self) -> None:
        task = _hydrate(task_key="search_index")

        assert task.instruction_kind is AIInstructionKind.CONTRACT
        assert (task.contract_key, task.contract_version) == ("explain", "1")
        assert task.contract_body == "Explain it in Markdown."

    def test_the_callers_unit_of_work_is_carried(self) -> None:
        task = _hydrate(
            task_key="search_index", input=AIInput.prompt("Where is AITask?"),
            output=AIOutputSpec.markdown(), subject="rey_lib.ai", subject_id="node:1",
            subject_kind="package", origin_id="node:0",
        )

        assert task.input.is_empty() is False
        assert task.output.media_type == "text/markdown"
        assert (task.subject, task.subject_id, task.subject_kind, task.origin_id) == (
            "rey_lib.ai", "node:1", "package", "node:0",
        )
        assert task.status == AITask.IDLE


class TestRefusals:
    @pytest.mark.parametrize(("choices", "message"), [
        ({"profile_key": "nowhere"}, "engine 'nowhere', which is not configured"),
        ({"profile_key": "switched_off"}, "engine 'switched_off', which is disabled"),
        ({"instruction_key": "unknown"}, "instruction 'unknown', which is not configured"),
        ({"instruction_key": "empty_contract"}, "carries no body"),
        ({"task_key": "retired"}, "AI task 'retired' is disabled"),
    ])
    def test_what_cannot_be_executed_is_refused(self, choices: dict, message: str) -> None:
        with pytest.raises(AIConfigurationError, match=message):
            _hydrate(**choices)

    def test_no_engine_chosen_anywhere_is_refused(self) -> None:
        with pytest.raises(AIConfigurationError, match="chooses no engine"):
            AITask.hydrate([], ENGINES, INSTRUCTIONS, installation="local")


class TestFromGoverned:
    class _Control:
        """Stands in for the runtime's Control: the three existing AI reads."""

        def __init__(self) -> None:
            self.asked: list[str] = []

        def ai_configuration(self, installation: str) -> list[dict]:
            self.asked.append(f"configuration:{installation}")
            return CONFIGURATION

        def ai_engine_profiles(self) -> list[dict]:
            self.asked.append("engines")
            return ENGINES

        def ai_instructions(self) -> list[dict]:
            self.asked.append("instructions")
            return INSTRUCTIONS

    def test_it_reads_through_control_and_hydrates_once(self) -> None:
        control = self._Control()

        task = AITask.from_governed(control, "local", "search_index",
                                    input=AIInput.prompt("hi"))

        assert control.asked == ["configuration:local", "engines", "instructions"]
        assert task.installation == "local"
        assert task.connection == "llm_reader"
        assert task.model_identifier == "ollama:qwen3-coder-next:latest"

    def test_a_changed_configuration_is_a_new_task(self) -> None:
        control = self._Control()

        first = AITask.from_governed(control, "local", "search_index")
        second = AITask.from_governed(control, "local", "search_index")

        assert first is not second
        assert first.id != second.id


class TestOffers:
    """#1141 S2: a task states what its own choices may be, from the rows it read."""

    def test_it_offers_the_enabled_engines_the_instructions_and_its_engines_representations(
        self,
    ) -> None:
        task = _hydrate(task_key="search_index")

        assert [dict(one) for one in task.offers["profile_key"]] == [
            {"value": "local_testing", "label": "ollama:qwen3-coder-next:latest"},
            {"value": "anthropic", "label": "anthropic:claude-sonnet-4-6"}]
        assert [one["value"] for one in task.offers["instruction_key"]] == [
            "__none__", "explain_v1", "empty_contract"]
        assert [one["value"] for one in task.offers["representation"]] == [
            "redacted", "unredacted"]

    def test_the_representations_follow_the_chosen_engine(self) -> None:
        task = _hydrate(task_key="search_index", profile_key="anthropic")

        assert [one["value"] for one in task.offers["representation"]] == ["redacted"]

    def test_the_projection_states_its_offers_and_no_presentation(self) -> None:
        """#1215: the task does not state how it is presented."""
        projection = _hydrate(task_key="search_index").projection()

        assert "panel_type" not in projection
        assert not hasattr(AITask, "PANEL_TYPE")
        assert set(projection["offers"]) == {"profile_key", "instruction_key", "representation"}
        assert "offers" not in _hydrate(task_key="search_index").definition()
