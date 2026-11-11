"""The live AI owns its installation's AI settings: edit in session, Save persists (#1191).

An edit changes the live AI's working settings and writes nothing. Save writes
each changed scope's STATED values through Control and re-reads them. NULL is
inheritance: a task value left unset stays NULL, clearing an override writes
NULL, and a default never replaces a task's override. A new task hydrates
with the working settings, unsaved edits included; a task already held keeps
its configuration, and the AI is never replaced.

Engine and temperature are the installation's alone (#1341): only the default
scope edits and saves them; a task inherits both, cannot edit them, saves both
as NULL, and a retired per-task value never reaches execution. Only the engine
may be chosen again, for the session.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from rey_lib.ai.ai import AI
from rey_lib.ai.errors import AIRequestError, AISelectionError
from rey_lib.ai.task import AITask

_FIELDS = ("profile_key", "instruction_key", "temperature", "representation",
           "connection_name", "max_turns")


class _Control:
    """Governed AI configuration as the database holds it: stated, and resolved."""

    def __init__(self) -> None:
        self.defaults: dict[str, Any] = {
            "profile_key": "anthropic", "instruction_key": None, "temperature": 0.2,
            "representation": "redacted", "connection_name": None, "max_turns": 8}
        # search_index still carries a legacy per-task engine and temperature,
        # retired by #1341: they are never read.
        self.tasks: dict[str, dict[str, Any]] = {
            "search_index": {"profile_key": "local", "instruction_key": None,
                             "temperature": 0.9, "representation": None,
                             "connection_name": "llm_reader", "max_turns": None},
            "generate_sql": {field: None for field in _FIELDS},
        }
        self.writes: list[tuple[str, ...]] = []

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        """One row per task, as ai.ai_installation_vw: stated defaults, stated own, effective."""
        def resolved(own: dict[str, Any]) -> dict[str, Any]:
            return {field: own[field] if own[field] is not None else self.defaults[field]
                    for field in _FIELDS}
        defaults = {"installation": installation, "default_composed_first": False,
                    **{f"default_{field}": value for field, value in self.defaults.items()}}
        return [{**defaults,
                 "task_key": task, "task_label": task, "is_enabled": True,
                 "composed_first": False, "tools": [],
                 **{f"task_{field}": value for field, value in own.items()},
                 **resolved(own)}
                for task, own in self.tasks.items()]

    def ai_engine_profiles(self, installation: str) -> list[dict[str, Any]]:
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

    def update_ai_installation_defaults(self, installation: str, **values: Any) -> None:
        self.writes.append(("default", installation))
        self.defaults = dict(values)

    def update_ai_installation_task(self, installation: str, task_key: str,
                                    **values: Any) -> None:
        self.writes.append((task_key, installation))
        self.tasks[task_key] = dict(values)


def _ai() -> AI:
    return AI(connections=("llm_reader", "rey_apps"))


def _scope(ai: AI, control: _Control, scope: str) -> dict[str, Any]:
    settings = ai.projection(control, "local")["settings"]
    return next(one for one in settings["scopes"] if one["id"] == scope)


class TestAnEditIsSessionState:
    def test_an_edit_changes_the_live_ai_and_writes_nothing(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "generate_sql", "connection", "rey_apps")

        assert control.writes == []
        assert _scope(ai, control, "generate_sql")["settings"]["connection"] == "rey_apps"
        assert ai.projection(control, "local")["settings"]["actions"] == [
            {"role": {"act": "save", "on": "settings"}, "command": {"command": "save_settings"}}]

    def test_an_unedited_survey_declares_no_save(self) -> None:
        ai, control = _ai(), _Control()

        assert ai.projection(control, "local")["settings"]["actions"] == []

    def test_each_scope_declares_its_five_edits_and_not_compose_before_run(self) -> None:
        ai, control = _ai(), _Control()

        actions = _scope(ai, control, "default")["actions"]

        assert [one["role"]["field"] for one in actions] == [
            "profile_id", "instruction_id", "temperature", "representation", "connection", "max_turns"]
        assert actions[0]["command"] == {"command": "set_setting", "scope": "default",
                                         "field": "profile_id"}
        assert actions[0]["inputs"] == {"value": ["value"]}

    def test_a_task_shows_its_own_values_and_inherits_engine_and_temperature(self) -> None:
        ai, control = _ai(), _Control()

        scope = _scope(ai, control, "search_index")

        assert scope["settings"]["connection"] == "llm_reader"
        # The legacy per-task values are not shown as the task's own.
        assert scope["settings"]["profile_id"] == ""
        assert scope["settings"]["temperature"] is None
        assert scope["effective"]["profile_id"] == "anthropic"
        assert scope["effective"]["temperature"] == 0.2

    def test_a_task_scope_declares_no_engine_or_temperature_edit(self) -> None:
        ai, control = _ai(), _Control()

        actions = _scope(ai, control, "search_index")["actions"]

        assert [one["role"]["field"] for one in actions] == [
            "instruction_id", "representation", "connection", "max_turns"]

    @pytest.mark.parametrize("field, value", [("profile_id", "other"), ("temperature", "0.7")])
    def test_a_task_scope_refuses_an_engine_or_temperature_edit(
            self, field: str, value: str) -> None:
        ai, control = _ai(), _Control()
        before = deepcopy(_scope(ai, control, "search_index")["settings"])

        with pytest.raises(AIRequestError, match="installation's defaults"):
            ai.set_setting(control, "local", "search_index", field, value)

        assert control.writes == []
        assert _scope(ai, control, "search_index")["settings"] == before


class TestSaveKeepsInheritance:
    def test_the_default_scope_saves_engine_and_temperature_and_every_task_inherits(
            self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "default", "profile_id", "other")
        ai.set_setting(control, "local", "default", "temperature", "0.5")
        ai.save_settings(control, "local")

        assert control.writes == [("default", "local")]
        assert control.defaults["profile_key"] == "other"
        assert control.defaults["temperature"] == 0.5
        for task in ("search_index", "generate_sql"):
            assert _scope(ai, control, task)["effective"]["profile_id"] == "other"
            assert _scope(ai, control, task)["effective"]["temperature"] == 0.5

    def test_saving_a_task_never_materializes_an_inherited_default(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "search_index", "representation", "unredacted")
        ai.save_settings(control, "local")

        saved = control.tasks["search_index"]
        assert saved["representation"] == "unredacted"
        # Inherited before the save, inherited after it: NULL, not the default.
        assert saved["instruction_key"] is None
        assert saved["max_turns"] is None

    def test_saving_other_task_settings_recreates_no_engine_or_temperature(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "search_index", "connection", "rey_apps")
        ai.save_settings(control, "local")

        saved = control.tasks["search_index"]
        assert saved["connection_name"] == "rey_apps"
        # The legacy per-task engine and temperature are not written back.
        assert saved["profile_key"] is None
        assert saved["temperature"] is None

    def test_clearing_an_override_writes_null_and_restores_inheritance(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "search_index", "connection", "")
        ai.save_settings(control, "local")

        assert control.tasks["search_index"]["connection_name"] is None
        assert _scope(ai, control, "search_index")["effective"]["connection"] == ""

    def test_only_changed_scopes_are_written_and_the_save_is_re_read(self) -> None:
        ai, control = _ai(), _Control()

        ai.set_setting(control, "local", "generate_sql", "connection", "rey_apps")
        ai.save_settings(control, "local")

        assert control.writes == [("generate_sql", "local")]
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

        ai.set_setting(control, "local", "default", "profile_id", "other")
        fresh = ai.hydrate_task(control, "local", task_key="generate_sql")

        assert fresh.profile_key == "other"
        assert ai.task(held.id) is held and held.profile_key == "anthropic"

    def test_save_keeps_the_live_ai_and_every_task_it_holds(self) -> None:
        ai, control = _ai(), _Control()
        held = ai.hydrate_task(control, "local", task_key="search_index")
        tasks = dict(ai._tasks)

        ai.set_setting(control, "local", "default", "profile_id", "other")
        ai.save_settings(control, "local")

        assert ai._tasks == tasks
        assert ai.task(held.id) is held and held.profile_key == "anthropic"
        assert ai.hydrate_task(control, "local", task_key="search_index").profile_key == "other"


class TestEngineAndTemperatureAreTheInstallations:
    def test_a_legacy_per_task_engine_and_temperature_never_reach_execution(self) -> None:
        control = _Control()

        task = AITask.from_governed(control, "local", "search_index")

        assert control.tasks["search_index"]["profile_key"] == "local"
        assert control.tasks["search_index"]["temperature"] == 0.9
        assert task.profile_key == "anthropic"
        assert task.temperature == 0.2

    def test_the_session_engine_overrides_the_installations_and_not_the_temperature(
            self) -> None:
        control = _Control()

        task = AITask.from_governed(control, "local", "search_index", profile_key="other")

        assert task.profile_key == "other"
        assert task.temperature == 0.2


class TestWorkbenchSelectionBelongsToTheAI:
    def test_unselected_workbench_uses_default_and_keeps_its_task(self) -> None:
        ai, control = _ai(), _Control()
        projected = ai.projection(control, "local")["workbench"]
        assert projected["scope"] == "default"
        first = ai.workbench_task(control, "local")
        assert first.task_key == ""
        assert first.profile_key == control.defaults["profile_key"]
        assert ai.workbench_task(control, "local") is first
        assert control.writes == []

    def test_selected_task_uses_unsaved_settings_without_changing_another_object(self) -> None:
        ai, control = _ai(), _Control()
        unrelated = ai.hydrate_task(control, "local", task_key="generate_sql")
        ai.select_workbench_task(control, "local", "generate_sql")
        first = ai.workbench_task(control, "local")
        from rey_lib.ai import AIInput
        edited = ai.rehydrate_task(control, first.id, {"input": AIInput.prompt("Keep this prompt.")})
        ai.set_setting(control, "local", "default", "profile_id", "other")
        changed = ai.workbench_task(control, "local")
        assert changed.id == first.id
        assert changed.input == edited.input
        assert changed.profile_key == "other"
        assert unrelated.profile_key == "anthropic"
        assert control.writes == []
        ai.save_settings(control, "local")
        assert ai.workbench_task(control, "local").id == first.id
        assert ai.hydrate_task(control, "local", task_key="generate_sql").profile_key == "other"
        assert control.writes == [("default", "local")]

    def test_selection_and_default_fallback_are_projected_by_the_object(self) -> None:
        ai, control = _ai(), _Control()
        ai.select_workbench_task(control, "local", "search_index")
        selected = ai.workbench_task(control, "local")
        assert selected.task_key == "search_index"
        assert ai.projection(control, "local")["workbench"]["scope"] == "search_index"
        ai.select_workbench_task(control, "local", "")
        assert ai.workbench_task(control, "local").task_key == ""
        ai.select_workbench_task(control, "local", "search_index")
        assert ai.workbench_task(control, "local").id == selected.id


def test_turn_limit_save_inheritance_and_execution_hydration() -> None:
    ai, control = _ai(), _Control()
    ai.set_setting(control, "local", "default", "max_turns", "21")
    ai.set_setting(control, "local", "search_index", "max_turns", "13")
    ai.select_workbench_task(control, "local", "search_index")
    assert ai.workbench_task(control, "local").max_turns == 13
    ai.select_workbench_task(control, "local", "generate_sql")
    assert ai.workbench_task(control, "local").max_turns == 21
    ai.save_settings(control, "local")
    assert control.defaults["max_turns"] == 21
    assert control.tasks["search_index"]["max_turns"] == 13
    ai.set_setting(control, "local", "search_index", "max_turns", "")
    ai.save_settings(control, "local")
    assert control.tasks["search_index"]["max_turns"] is None
    ai.select_workbench_task(control, "local", "search_index")
    assert ai.workbench_task(control, "local").max_turns == 21
    with pytest.raises(AIRequestError, match="positive integer"):
        ai.set_setting(control, "local", "default", "max_turns", "")
