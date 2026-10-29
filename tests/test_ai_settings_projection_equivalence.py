"""The live AI's projection is unchanged by the #1025 cutover.

The golden (fixtures/ai/settings_projection_golden.json) was captured from the
legacy construction -- bootstrap.open_shared_ai over these same governed rows --
before AISettings, AIRegistry and the profiles were deleted. The AI Settings
panel reads ``settings`` and nothing else (ai_settings_panel/types.ts), so the
live AI must answer the same rows with the same projection, value for value.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from rey_lib.ai import AI

_GOLDEN = Path(__file__).parent / "fixtures" / "ai" / "settings_projection_golden.json"


class _Control:
    """The three governed AI reads, as the control.f_ai_*_get rows.

    ``composed_first`` is ``opens_workbench`` under its AI name, as
    control.f_ai_configuration_get returns both.
    """

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        assert installation == "local"
        return [
            {"row_type": "group", "task_key": None, "task_label": None, "is_enabled": True,
             "profile_key": "anthropic", "instruction_key": "__none__", "temperature": 0.2,
             "representation": "redacted", "connection_name": "llm_reader",
             "opens_workbench": False, "composed_first": False},
            {"row_type": "task", "task_key": "search_index", "task_label": "Search the index",
             "is_enabled": True, "profile_key": "local_precision", "instruction_key": "explain_v1",
             "temperature": 0, "representation": "unredacted", "connection_name": "",
             "opens_workbench": True, "composed_first": True},
            {"row_type": "task", "task_key": "generate_sql", "task_label": None, "is_enabled": True,
             "profile_key": "", "instruction_key": "", "temperature": None, "representation": "",
             "connection_name": "rey_apps", "opens_workbench": None, "composed_first": None},
            {"row_type": "task", "task_key": "retired", "task_label": "Retired",
             "is_enabled": False, "profile_key": "", "instruction_key": "__ad_hoc__",
             "temperature": 1.5, "representation": "", "connection_name": "",
             "opens_workbench": False, "composed_first": False},
        ]

    def ai_engine_profiles(self) -> list[dict[str, Any]]:
        return [
            {"profile_key": "anthropic", "provider": "anthropic", "model": "claude-sonnet-4-6",
             "endpoint": "", "timeout_seconds": None, "credential_ref": "", "options": {},
             "access_allowed": ["redacted"], "access_default": "redacted", "is_enabled": True,
             "model_identifier": "anthropic:claude-sonnet-4-6"},
            {"profile_key": "local_precision", "provider": "ollama", "model": "qwen2.5-coder:32b",
             "endpoint": "http://localhost:11434", "timeout_seconds": 300, "credential_ref": "",
             "options": {}, "access_allowed": ["redacted", "unredacted"],
             "access_default": "unredacted", "is_enabled": True,
             "model_identifier": "ollama:qwen2.5-coder:32b"},
            {"profile_key": "no_policy", "provider": "openai", "model": "gpt-4o", "endpoint": "",
             "timeout_seconds": None, "credential_ref": "", "options": {},
             "access_allowed": ["redacted", "redacted"], "access_default": "redacted",
             "is_enabled": True, "model_identifier": "openai-chat:gpt-4o"},
        ]

    def ai_instructions(self) -> list[dict[str, Any]]:
        return [
            {"instruction_key": "explain_v1", "kind": "contract", "body": "Explain it.",
             "contract_key": "explain", "contract_version": "v1"},
            {"instruction_key": "__ad_hoc__", "kind": "raw", "body": None,
             "contract_key": None, "contract_version": None},
            {"instruction_key": "__none__", "kind": "none", "body": None,
             "contract_key": None, "contract_version": None},
        ]


def test_the_projection_equals_the_one_captured_before_the_cutover() -> None:
    # The live AI as ai_app constructs it, offered the same two connections the
    # legacy construction was given when the golden was captured.
    projection = AI(connections=("llm_reader", "rey_apps")).projection(_Control(), "local")

    assert json.loads(json.dumps(projection)) == json.loads(_GOLDEN.read_text())
