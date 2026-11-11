"""The live AI's projection is unchanged by the #1025 cutover.

The golden (fixtures/ai/settings_projection_golden.json) was captured from the
legacy construction -- bootstrap.open_shared_ai over these same governed rows --
before AISettings, AIRegistry and the profiles were deleted. The AI Settings
panel reads ``settings`` and nothing else (ai_settings_panel/types.ts), so the
live AI must answer the same rows with the same projection, value for value.
Save (#1191) since added the declared actions and lifted read-only; the stated
values now come from control.f_ai_settings_get, given here for the same scopes.
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
        """One row per task, as ai.ai_installation_vw (#1296): the installation's stated
        defaults on every row, the task's stated values, and the effective values."""
        assert installation == "local"
        defaults = {"installation": "local",
                    "default_profile_key": "anthropic", "default_instruction_key": "__none__",
                    "default_temperature": 0.2, "default_representation": "redacted",
                    "default_connection_name": "llm_reader", "default_composed_first": False}
        return [
            {**defaults, "task_key": "search_index", "task_label": "Search the index",
             "is_enabled": True, "task_profile_key": "local_precision",
             "task_instruction_key": "explain_v1", "task_temperature": 0,
             "task_representation": "unredacted", "task_connection_name": None,
             "profile_key": "local_precision", "instruction_key": "explain_v1",
             "temperature": 0, "representation": "unredacted", "connection_name": "",
             "opens_workbench": True, "composed_first": True},
            {**defaults, "task_key": "generate_sql", "task_label": None, "is_enabled": True,
             "task_profile_key": None, "task_instruction_key": None, "task_temperature": None,
             "task_representation": None, "task_connection_name": "rey_apps",
             "profile_key": "", "instruction_key": "", "temperature": None, "representation": "",
             "connection_name": "rey_apps", "opens_workbench": None, "composed_first": None},
            {**defaults, "task_key": "retired", "task_label": "Retired",
             "is_enabled": False, "task_profile_key": None,
             "task_instruction_key": "__ad_hoc__", "task_temperature": 1.5,
             "task_representation": None, "task_connection_name": None,
             "profile_key": "", "instruction_key": "__ad_hoc__",
             "temperature": 1.5, "representation": "", "connection_name": "",
             "opens_workbench": False, "composed_first": False},
        ]

    def ai_engine_profiles(self, installation: str) -> list[dict[str, Any]]:
        assert installation == "local"
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

    # What Save added since (#1191), and nothing else: the declared edits and
    # Save, and a survey that is no longer read-only.
    settings = projection["settings"]
    assert settings.pop("read_only") is False
    assert settings.pop("actions") == []
    for scope in settings["scopes"]:
        # Engine and temperature are the default scope's alone (#1341).
        assert [one["role"]["field"] for one in scope.pop("actions")] == (
            ["profile_id", "instruction_id", "temperature", "representation", "connection",
             "max_turns"] if scope["is_default"]
            else ["instruction_id", "representation", "connection", "max_turns"])
        # #1063 adds a governed turn budget; this older fixture states none.
        assert scope["settings"].pop("max_turns") is None
        assert scope["effective"].pop("max_turns") is None
    # AI now declares task selection/opening for the existing Workbench (#1320).
    # Account for that accepted addition explicitly; keep the historical golden
    # intact so unrelated settings/inventory drift still fails this comparison.
    assert projection.pop("workbench") == {
        "scope": "default",
        "select": {"role": {"act": "select", "on": "task"},
                   "command": {"command": "select_task"}, "inputs": {"scope": ["scope"]}},
        "open": {"role": {"act": "open", "on": "workbench"},
                 "command": {"command": "open_workbench"}},
    }
    golden = json.loads(_GOLDEN.read_text())
    assert golden["settings"].pop("read_only") is True
    # Engine and temperature are the installation's alone (#1341): a task states
    # neither, so it resolves both -- and its engine's representations -- from the
    # default scope; its retired per-task values are not read.
    default = next(one for one in golden["settings"]["scopes"] if one["is_default"])
    for scope in golden["settings"]["scopes"]:
        if not scope["is_default"]:
            scope["settings"].update(profile_id="", temperature=None)
            scope["effective"].update(profile_id=default["effective"]["profile_id"],
                                      temperature=default["effective"]["temperature"])
            scope["representations"] = default["representations"]

    assert json.loads(json.dumps(projection)) == golden
