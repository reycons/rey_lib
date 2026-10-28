"""The live Application, not its panel, owns Console run settings (#1255)."""
from types import SimpleNamespace

import pytest

from rey_lib.config.applications import (
    Application, ApplicationCommand, ApplicationCommandParameter as Parameter,
    ApplicationMode, ApplicationModeGroup,
)
from rey_lib.errors.error_utils import ConfigError


def configured() -> Application:
    return Application(name="sample", commands=(
        ApplicationCommand("run", parameters=(
            Parameter("source-name", value_type="choice", required=True,
                      possible_values=("alpha", "beta")),
            Parameter("dry-run", value_type="flag"),
            Parameter("limit", value_type="integer"),
        )),
        ApplicationCommand("other", parameters=(Parameter("path", value_type="path"),)),
        ApplicationCommand("serve", invocation="hosted"),
    ))


def test_openings_are_independent_of_each_other_and_shared_configuration() -> None:
    shared = configured()
    first, second = shared.for_console(object()), shared.for_console(object())
    first.update_settings({"values": {"source-name": "alpha"}})
    assert first.prepare_run() == {"command": "run", "params": {"source_name": "alpha"}}
    assert second.settings_projection()["values"] == {}
    assert "settings" not in shared.projection()
    assert first.projection()["settings"]["values"] == {"source-name": "alpha"}


def test_switching_commands_retains_values_without_sending_other_commands_fields() -> None:
    live = configured().for_console(object())
    live.update_settings({"values": {"source-name": "alpha"}})
    live.update_settings({"command": "other", "values": {"path": "/tmp/input"}})
    assert live.prepare_run() == {"command": "other", "params": {"path": "/tmp/input"}}
    live.update_settings({"command": "run"})
    assert live.settings_projection()["values"] == {"source-name": "alpha"}


def test_incomplete_and_invalid_drafts_are_visible_but_never_run() -> None:
    live = configured().for_console(object())
    assert live.settings_projection()["can_run"] is False
    live.update_settings({"values": {"source-name": "not-offered"}})
    assert "offers no choice" in live.settings_projection()["error"]
    with pytest.raises(ConfigError, match="offers no choice"):
        live.prepare_run()
    live.update_settings({"values": {"source-name": "alpha", "limit": "bad"}})
    assert "takes integer" in live.settings_projection()["error"]


def test_booleans_numbers_and_semantic_input_names_are_owned_by_application() -> None:
    live = configured().for_console(object())
    live.update_settings({"values": {"source-name": "alpha", "dry-run": "false", "limit": "0"}})
    assert live.prepare_run()["params"] == {"source_name": "alpha", "dry_run": False, "limit": 0}
    assert live.settings_projection()["values"]["dry-run"] is False


def test_modes_control_applicable_fields_and_conditional_requiredness() -> None:
    modes = ApplicationModeGroup(name="shape", default="simple", modes=(
        ApplicationMode(name="simple"), ApplicationMode(name="direct")))
    app = Application(name="sample", commands=(ApplicationCommand("run", mode_groups=(modes,),
        parameters=(Parameter("table", value_type="string", mode_membership=(("shape", ("direct",)),),
                              required_when=(("shape", ("direct",)),)),)),))
    live = app.for_console(object())
    assert live.prepare_run() == {"command": "run", "params": {}}
    live.update_settings({"modes": {"shape": "direct"}})
    assert live.settings_projection()["parameters"] == [{"name": "table", "required": True}]
    assert not live.settings_projection()["can_run"]
    live.update_settings({"values": {"table": "destination"}})
    assert live.prepare_run()["params"] == {"table": "destination"}
    live.update_settings({"modes": {"shape": "simple"}})
    assert live.prepare_run()["params"] == {}
    assert live.settings_projection()["values"]["table"] == "destination"


@pytest.mark.parametrize("changes", [
    {"command": "serve"}, {"command": "missing"}, {"values": {"unknown": 1}},
    {"modes": {"unknown": "choice"}}, {"save": True},
])
def test_undeclared_edits_are_refused_without_changing_state(changes) -> None:
    live = configured().for_console(object())
    before = live.projection()
    with pytest.raises(ConfigError):
        live.update_settings(changes)
    assert live.projection() == before


def test_database_backed_options_refresh_through_the_existing_control_owner() -> None:
    class Control:
        options = ["one"]
        reads = []
        def http_operation(self, installation):
            self.reads.append(installation)
            return [{"adapter_key": name, "is_enabled": True} for name in self.options]
    control = Control()
    ctx = SimpleNamespace(shared_control=control, installation=SimpleNamespace(name="local"))
    shared = Application(name="sample", commands=(ApplicationCommand("run", parameters=(
        Parameter("operation", value_type="choice", _choice_source="http_operations"),)),))
    live = shared.for_console(ctx)
    assert live.command("run").parameters[0].possible_values == ("one",)
    live.update_settings({"values": {"operation": "one"}})
    control.options = ["two"]
    live.refresh_choices(ctx)
    assert live.command("run").parameters[0].possible_values == ("two",)
    assert not live.settings_projection()["can_run"]
    assert shared.command("run").parameters[0].possible_values == ()
    assert control.reads == ["local", "local"]


def test_projected_and_supplied_structured_values_cannot_mutate_the_owner() -> None:
    app = Application(name="sample", commands=(ApplicationCommand("run", parameters=(
        Parameter("settings", value_type="json"),)),)).for_console(object())
    supplied = {"nested": [1]}
    app.update_settings({"values": {"settings": supplied}})
    supplied["nested"].append(2)
    projected = app.settings_projection()
    projected["values"]["settings"]["nested"].append(3)
    assert app.prepare_run()["params"] == {"settings": {"nested": [1]}}
    app.update_settings({"values": {"settings": '{"nested": [4]}'}})
    assert app.prepare_run()["params"] == {"settings": {"nested": [4]}}


def test_the_canonical_argument_builder_keeps_zero_values() -> None:
    command = ApplicationCommand("run", parameters=(Parameter("limit", value_type="integer"),))
    assert command.arguments({"limit": 0}) == ["--limit", "0"]


def test_application_level_parameters_are_a_root_invocation_not_an_invented_subcommand() -> None:
    shared = Application(name="flat", label="Flat", app_path="/apps/flat", parameters=(
        Parameter("connection", value_type="choice", possible_values=("local",)),))
    opened = shared.for_console(object())
    assert opened.projection()["commands"][0]["label"] == "Flat"
    assert opened.settings_projection()["parameters"] == [{"name": "connection", "required": False}]
    opened.update_settings({"values": {"connection": "local"}})
    request = opened.prepare_run()
    assert request == {"command": "", "params": {"connection": "local"}}
    ctx = SimpleNamespace(runtime=SimpleNamespace(python=SimpleNamespace(executable="/python")))
    assert opened.command_line(ctx, request["command"], request["params"]) == [
        "/python", "/apps/flat/main.py", "--connection", "local"]
    assert shared.commands == ()
