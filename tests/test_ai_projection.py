"""AI states what it offers an installation, as plain data (backlog 1041, 1047).

What an ai_app session sends its presentation: the governed tasks, in configured
order, each with its key, label and whether it is composed first; and what a
task's choices may name -- the enabled engines and the instructions (D2). Read
through the Control's existing AI reads; nothing else re-derives it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.ai import AI
from rey_lib.ai.registry import AIRegistry


class _Control:
    """The three reads projection uses, as the control.f_ai_*_get rows."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        self.asked.append(installation)
        return [
            {"row_type": "group", "task_key": None, "task_label": None, "is_enabled": True,
             "composed_first": False},
            {"row_type": "task", "task_key": "search_index", "task_label": "Search the index",
             "is_enabled": True, "composed_first": True},
            {"row_type": "task", "task_key": "retired", "task_label": "Retired",
             "is_enabled": False, "composed_first": False},
            {"row_type": "task", "task_key": "generate_sql", "task_label": None,
             "is_enabled": True, "composed_first": None},
        ]

    def ai_engine_profiles(self) -> list[dict[str, Any]]:
        self.asked.append("engines")
        return [
            {"profile_key": "local_testing", "model_identifier": "ollama:qwen3-coder-next:latest",
             "credential_ref": "", "is_enabled": True},
            {"profile_key": "switched_off", "model_identifier": "openai-chat:gpt-4o",
             "credential_ref": "", "is_enabled": False},
            {"profile_key": "anthropic", "model_identifier": "anthropic:claude-sonnet-4-6",
             "credential_ref": "env.ANTHROPIC_API_KEY", "is_enabled": True},
        ]

    def ai_instructions(self) -> list[dict[str, Any]]:
        self.asked.append("instructions")
        return [
            {"instruction_key": "__none__", "kind": "none", "body": None},
            {"instruction_key": "explain_v1", "kind": "contract", "body": "Explain it."},
        ]


def test_the_projection_offers_the_enabled_governed_tasks_in_order() -> None:
    control = _Control()

    projection = AI(registry=AIRegistry()).projection(control, "local")

    assert control.asked == ["local", "engines", "instructions"]
    assert projection["tasks"] == [
        {"task_key": "search_index", "label": "Search the index", "composed_first": True},
        {"task_key": "generate_sql", "label": "generate_sql", "composed_first": False},
    ]


def test_the_projection_offers_what_a_choice_may_name_and_no_credential() -> None:
    projection = AI(registry=AIRegistry()).projection(_Control(), "local")

    assert projection["engines"] == [
        {"profile_key": "local_testing", "model_identifier": "ollama:qwen3-coder-next:latest"},
        {"profile_key": "anthropic", "model_identifier": "anthropic:claude-sonnet-4-6"},
    ]
    assert projection["instructions"] == [
        {"instruction_key": "__none__", "kind": "none"},
        {"instruction_key": "explain_v1", "kind": "contract"},
    ]
    assert "ANTHROPIC" not in str(projection)


def test_settings_are_projected_from_the_live_owner_without_creating_tasks() -> None:
    from rey_lib.ai.settings import AISettings, AISettingsTask
    ai = AI(registry=AIRegistry(), settings=AISettings(temperature=0.5, composed_first=True,
        tasks=(AISettingsTask(name="search_index", temperature=0, composed_first=False),)))
    projection = ai.projection(_Control(), "local")["settings"]
    default, task = projection["scopes"]
    assert default["id"] == "default" and default["effective"]["temperature"] == 0.5
    assert task["label"] == "Search the index" and task["effective"]["temperature"] == 0
    assert task["effective"]["composed_first"] is False
    assert projection["read_only"] is True
    assert ai._tasks == {}
    ai.update_settings(AISettings(temperature=0.8))
    assert ai.projection(_Control(), "local")["settings"]["scopes"][0]["settings"]["temperature"] == 0.8
    assert ai._tasks == {}


def test_settings_projection_is_plain_data_and_cannot_mutate_the_owner() -> None:
    from rey_lib.ai.settings import AISettings
    ai = AI(registry=AIRegistry(), settings=AISettings(temperature=0))
    settings = ai.projection(_Control(), "local")["settings"]
    settings["scopes"][0]["settings"]["temperature"] = 99
    settings["scopes"][0]["effective"]["temperature"] = 100
    assert ai.settings.temperature == 0


def test_settings_include_held_profiles_instruction_text_and_effective_values() -> None:
    from rey_lib.ai.settings import AISettings, AISettingsTask
    from rey_lib.ai.profiles import AIProfile
    from rey_lib.ai.instructions import AIInstruction, AIInstructionKind
    ai = AI(registry=AIRegistry(
        profiles=(AIProfile(id="held", provider="owner-provider", model="owner-model",
            profile_access={"allowed": ["redacted"], "default": "redacted"}),),
        instructions=(AIInstruction(id="explain_v1", kind=AIInstructionKind.RAW,
                                    name="Held instruction", text="actual owner text"),)),
        connections=("llm_reader",),
        settings=AISettings(profile_id="held", instruction_id="explain_v1", temperature=0,
            representation="redacted", connection="llm_reader", composed_first=True,
            tasks=(AISettingsTask(name="search_index"),)))
    survey = ai.projection(_Control(), "local")["settings"]
    default, task = survey["scopes"]
    assert task["effective"] == {"profile_id": "held", "instruction_id": "explain_v1",
        "temperature": 0, "representation": "redacted", "connection": "llm_reader", "composed_first": False}
    assert survey["profiles"][0]["label"] == "owner-provider - owner-model"
    assert default["instruction"]["content"] == "actual owner text"
    assert default["instruction"]["read_only"] is True
    assert survey["connections"] == ["llm_reader"]
    ai.set_instruction_text("explain_v1", "new owner text")
    assert ai.projection(_Control(), "local")["settings"]["scopes"][0]["instruction"]["content"] == "new owner text"
    assert ai._tasks == {}
