"""App declarations own prompts, typed choices and their nested configuration paths."""
import pytest
from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.workflow.workflow_step import WorkflowStep, setting_form, form_fields
from rey_lib.errors.error_utils import AppError

SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "object", "title": "Input source", "additionalProperties": False,
                   "properties": {
                       "mode": {"type": "integer", "title": "Read strategy", "oneOf": [
                           {"const": 1, "title": "Whole input"},
                           {"const": 2, "title": "Changes only"}]}}},
        "operation": {"type": "string", "title": "Action", "enum": ["read", "write"]}},
    "oneOf": [
        {"properties": {"operation": {"const": "read"},
                        "reader": {"type": "object", "title": "Reader settings",
                                   "properties": {"limit": {"type": "integer", "minimum": 1}}}},
         "required": ["operation"]},
        {"properties": {"operation": {"const": "write"},
                        "target": {"type": "string"}}, "required": ["operation"]}]}


def step():
    app = Application(name="fixture_app", workflow_operations=(
        ApplicationOperation(name="load", contract_definition=SCHEMA),))
    return WorkflowStep(id="s", process_name="p", label="Step", ordinal=1,
        application=app, operation_name="load", approved={"load": lambda *a: None},
        settings={"operation": "read", "source": {"mode": 1}, "reader": {"limit": 3}})


def test_nested_prompt_choice_pairs_keep_machine_paths_and_values():
    form = setting_form({"source": {"mode": 1}}, {"source": {"mode": 1}}, SCHEMA, "step:1")
    assert form[0]["label"] == "Input source"
    field = form[0]["items"][0]
    assert (field["name"], field["label"], field["control"], field["value"]) == (
        "source.mode", "Read strategy", "select", "1")
    assert field["choices"] == [{"value": "1", "label": "Whole input"},
                                {"value": "2", "label": "Changes only"}]
    assert field["command"]["arguments"] == {"name": "source.mode"}


def test_update_accepts_machine_value_and_refuses_label_or_unsupported_value():
    s = step()
    s.update("source.mode", "2")
    assert s.settings["source"]["mode"] == 2
    for bad in ("Changes only", "99"):
        with pytest.raises(AppError):
            s.update("source.mode", bad)
    assert s.settings["source"]["mode"] == 2


def test_variant_hierarchy_exposes_only_selected_app_fields():
    s = step()
    fields = {one["name"]: one for one in form_fields(s.form()[1]["items"])}
    assert "reader.limit" in fields
    assert "target" not in fields
    assert fields["operation"]["choices"] == ["read", "write"]
    s.update("reader.limit", "4")
    assert s.settings["reader"]["limit"] == 4
    with pytest.raises(AppError):
        s.update("reader.limit", "0")
    with pytest.raises(AppError):
        s.update("target", "unselected variant")


def test_legacy_unknown_setting_is_visible_but_not_editable():
    s = step()
    s.configured_settings["legacy"] = "old"
    s.settings["legacy"] = "old"
    fields = {one["name"]: one for one in form_fields(s.form()[1]["items"])}
    assert "command" not in fields["legacy"]
    with pytest.raises(AppError, match="has no setting"):
        s.update("legacy", "new")


def test_declared_keyed_maps_keep_their_app_value_contract():
    schema = {"type": "object", "properties": {"params": {
        "type": "object", "title": "Routine parameters", "additionalProperties": {
            "type": "integer", "title": "Parameter value", "minimum": 1}}}}
    values = {"params": {"batch_size": 10}}
    fields = form_fields(setting_form(values, values, schema, "step:1"))
    assert fields[0]["name"] == "params.batch_size"
    assert fields[0]["label"] == "Routine parameters › Parameter value"
    assert fields[0]["command"]["arguments"] == {"name": "params.batch_size"}
