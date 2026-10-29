"""The live AI owns its installation's AI settings: edit in session, Save persists (#1191).

An edit changes the live AI's working settings and writes nothing. Save writes
each changed scope's STATED values through Control and re-reads them. NULL is
inheritance: a task value left unset stays NULL, clearing an override writes
NULL, and a default never replaces a task's override. A new task hydrates
with the working settings, unsaved edits included; a task already held keeps
its configuration, and the AI is never replaced.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from rey_lib.ai.ai import AI
from rey_lib.ai.errors import AIRequestError, AISelectionError

_FIELDS = ("profile_key", "instruction_key", "temperature", "representation",
           "connection_name")


class _Control:
    """Governed AI configuration as the database holds it: stated, and resolved."""

    def __init__(self) -> None:
        self.defaults: dict[str, Any] = {
            "profile_key": "anthropic", "instruction_key": None, "temperature": 0.2,
            "representation": "redacted", "connection_name": None}
        self.tasks: dict[str, dict[str, Any]] = {
            "search_index": {"profile_key": "local", "instruction_key": None,
                             "temperature": None, "representation": None,
                             "connection_name": "llm_reader"},
            "generate_sql": {field: None for field in _FIELDS},
        }
        self.writes: list[tuple[str, ...]] = []

    def ai_settings(self, installation: str) -> list[dict[str, Any]]:
        group = {"installation": installation, "ai_group_key": "console_default",
                 **{f"default_{field}": value for field, value in self.defaults.items()}}
        return [{**group, "task_key": task, **{f"task_{field}": value
                                               for field, value in own.items()}}
                for task, own in self.tasks.items()]

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        def resolved(own: dict[str, Any]) -> dict[str, Any]:
            return {field: own[field] if own[field] is not None else self.defaults[field]
                    for field in _FIELDS}
        group = {"row_type": "group", "ai_group_key": "console_default", "task_key": None,
                 "composed_first": False, **self.defaults}
        return [group, *({"row_type": "task", "ai_group_key": "console_default",
                          "task_key": task, "task_label": task, "is_enabled": True,
                          "composed_first": False, "tools": [], **resolved(own)}
                         for task, own in self.tasks.items())]

    def ai_engine_profiles(self) -> list[dict[str, Any]]:
        return [{"profile_key": key, "provider": provider, "model": model,
                 "model_identifier": f"{provider}:{model}", "credential_ref": "",
                 "base_url": "", "execution_mechanism": "pydantic_ai", "is_enabled": True,
                 "access_allowed": ["redacted", "unredacted"], "access_default": "redacted"}
                for key, provider, model in (("anthropic", "anthropic", "claude-sonnet-4-6"),
                                             ("local", "ollama", "qwen"),
                                             ("other", "openai", "gpt-4o"))]

    def ai_instructions(self) -> list[dict[str, Any]]:
        return [{"instruction_key": "__none__", "kind": "none", "body": None},
                {"instruction_key": "explain_v1", "kind": "contract", "body": "Explain it.",
                 "contract_key": "explain", "contract_version": "v1"}]

    def update_ai_group_defaults(self, installation: str, ai_group_key: str,
                                 **values: Any) -> None:
        self.writes.append(("default", installation, ai_group_key))
        self.defaults = dict(values)

    def update_ai_group_task(self, installation: str, ai_group_key: str, task_key: str,
                             **values: Any) -> None:
        self.writes.append((task_key, installation, ai_group_key))
        self.tasks[task_key] = dict(values)


def _ai() -> AI:
    return AI(connections=("llm_reader", "rey_apps"))


def _scope(ai: AI, control: _Control, scope: str) -> dict[str, Any]:
    settings = ai.projection(control, "local")["settings"]
    return next(one for one in settings["scopes"] if one["id"] == scope)


class TestAnEditIsSessionState:
    def test_an_edit_changes_the_live_ai_and_writes_nothing(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "generate_sql", "profile_id", "other")

        assert control.writes == []
        assert _scope(ai, control, "generate_sql")["settings"]["profile_id"] == "other"
        assert ai.projection(control, "local")["settings"]["actions"] == [
            {"role": {"act": "save", "on": "settings"}, "command": {"command": "save_settings"}}]

    def test_an_unedited_survey_declares_no_save(self) -> None:
        ai, control = _ai(), _Control()

        assert ai.projection(control, "local")["settings"]["actions"] == []

    def test_each_scope_declares_its_five_edits_and_not_compose_before_run(self) -> None:
        ai, control = _ai(), _Control()

        actions = _scope(ai, control, "default")["actions"]

        assert [one["role"]["field"] for one in actions] == [
            "profile_id", "instruction_id", "temperature", "representation", "connection"]
        assert actions[0]["command"] == {"command": "set_setting", "scope": "default",
                                         "field": "profile_id"}
        assert actions[0]["inputs"] == {"value": ["value"]}

    def test_a_task_shows_its_own_values_and_inherits_the_rest(self) -> None:
        ai, control = _ai(), _Control()

        scope = _scope(ai, control, "search_index")

        assert scope["settings"]["profile_id"] == "local"
        assert scope["settings"]["temperature"] is None
        assert scope["effective"]["temperature"] == 0.2


class TestSaveKeepsInheritance:
    def test_saving_a_default_does_not_replace_a_tasks_override(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "default", "profile_id", "other")
        ai.save_settings(control, "local")

        assert control.writes == [("default", "local", "console_default")]
        assert control.defaults["profile_key"] == "other"
        assert control.tasks["search_index"]["profile_key"] == "local"
        assert _scope(ai, control, "search_index")["effective"]["profile_id"] == "local"
        assert _scope(ai, control, "generate_sql")["effective"]["profile_id"] == "other"

    def test_saving_a_task_never_materializes_an_inherited_default(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "search_index", "temperature", "0.7")
        ai.save_settings(control, "local")

        saved = control.tasks["search_index"]
        assert saved["temperature"] == 0.7
        # Inherited before the save, inherited after it: NULL, not the default.
        assert saved["instruction_key"] is None
        assert saved["representation"] is None
        assert saved["profile_key"] == "local"

    def test_clearing_an_override_writes_null_and_restores_inheritance(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "search_index", "profile_id", "")
        ai.save_settings(control, "local")

        assert control.tasks["search_index"]["profile_key"] is None
        assert _scope(ai, control, "search_index")["effective"]["profile_id"] == "anthropic"

    def test_only_changed_scopes_are_written_and_the_save_is_re_read(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "generate_sql", "connection", "rey_apps")
        ai.save_settings(control, "local")

        assert control.writes == [("generate_sql", "local", "console_default")]
        assert ai.projection(control, "local")["settings"]["actions"] == []
        ai.save_settings(control, "local")
        assert len(control.writes) == 1


class TestTheOwnerValidates:
    @pytest.mark.parametrize("field, value, refusal", [
        ("profile_id", "absent", AISelectionError),
        ("instruction_id", "absent", AISelectionError),
        ("connection", "warehouse", AISelectionError),
        ("representation", "raw", AISelectionError),
        ("temperature", "warm", AIRequestError),
        ("composed_first", "true", AIRequestError),
    ])
    def test_a_value_the_configuration_does_not_offer_is_refused(
            self, field: str, value: str, refusal: type) -> None:
        ai, control = _ai(), _Control()
        before = deepcopy(_scope(ai, control, "default")["settings"])

        with pytest.raises(refusal):
            ai.set_setting(control, "local", "default", field, value)

        assert control.writes == []
        assert _scope(ai, control, "default")["settings"] == before

    def test_an_unknown_scope_is_refused(self) -> None:
        with pytest.raises(AISelectionError, match="no scope"):
            _ai().set_setting(_Control(), "local", "absent", "profile_id", "other")


class TestTheLiveAiAndItsTasksAreKept:
    def test_a_new_task_takes_the_unsaved_edit_and_a_held_task_keeps_its_own(self) -> None:
        ai, control = _ai(), _Control()
        held = ai.hydrate_task(control, "local", task_key="generate_sql")
        assert held.profile_key == "anthropic"

        ai.set_setting(control, "local", "generate_sql", "profile_id", "other")
        fresh = ai.hydrate_task(control, "local", task_key="generate_sql")

        assert fresh.profile_key == "other"
        assert ai.task(held.id) is held and held.profile_key == "anthropic"

    def test_save_keeps_the_live_ai_and_every_task_it_holds(self) -> None:
        ai, control = _ai(), _Control()
        held = ai.hydrate_task(control, "local", task_key="search_index")
        tasks = dict(ai._tasks)

        ai.set_setting(control, "local", "search_index", "profile_id", "other")
        ai.save_settings(control, "local")

        assert ai._tasks == tasks
        assert ai.task(held.id) is held and held.profile_key == "local"
        assert ai.hydrate_task(control, "local", task_key="search_index").profile_key == "other"
