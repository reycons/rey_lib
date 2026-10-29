"""AI states what it offers an installation, as plain data (backlog 1041, 1047).

What an ai_app session sends its presentation: the governed tasks, in configured
order, each with its key, label and whether it is composed first; and what a
task's choices may name -- the enabled engines and the instructions (D2). Read
through the Control's existing AI reads; nothing else re-derives it. The
settings survey is held to its pre-#1025 shape by
test_ai_settings_projection_equivalence.py.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.ai import AI


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
             "credential_ref": "", "is_enabled": True,
             "access_allowed": ["redacted"], "access_default": "redacted"},
            {"profile_key": "switched_off", "model_identifier": "openai-chat:gpt-4o",
             "credential_ref": "", "is_enabled": False,
             "access_allowed": ["redacted"], "access_default": "redacted"},
            {"profile_key": "anthropic", "model_identifier": "anthropic:claude-sonnet-4-6",
             "credential_ref": "env.ANTHROPIC_API_KEY", "is_enabled": True,
             "access_allowed": ["redacted"], "access_default": "redacted"},
        ]

    def ai_instructions(self) -> list[dict[str, Any]]:
        self.asked.append("instructions")
        return [
            {"instruction_key": "__none__", "kind": "none", "body": None},
            {"instruction_key": "explain_v1", "kind": "contract", "body": "Explain it."},
        ]


def test_the_projection_offers_the_enabled_governed_tasks_in_order() -> None:
    control = _Control()

    projection = AI().projection(control, "local")

    assert control.asked == ["local", "engines", "instructions"]
    assert projection["tasks"] == [
        {"task_key": "search_index", "label": "Search the index", "composed_first": True},
        {"task_key": "generate_sql", "label": "generate_sql", "composed_first": False},
    ]


def test_the_projection_offers_what_a_choice_may_name_and_no_credential() -> None:
    projection = AI().projection(_Control(), "local")

    assert projection["engines"] == [
        {"profile_key": "local_testing", "model_identifier": "ollama:qwen3-coder-next:latest"},
        {"profile_key": "anthropic", "model_identifier": "anthropic:claude-sonnet-4-6"},
    ]
    assert projection["instructions"] == [
        {"instruction_key": "__none__", "kind": "none"},
        {"instruction_key": "explain_v1", "kind": "contract"},
    ]
    assert "ANTHROPIC" not in str(projection)
