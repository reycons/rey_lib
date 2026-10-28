"""An Application operation launched from its semantic inputs (backlog 993).

The Application builds the invocation of one of its published operations from
its own configuration, the operation converts its semantic inputs into its
published command-line form, and LaunchedApp launches it. A caller names the
operation and gives semantic inputs; it never spells an executable, a flag or a
parameter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import (
    JSON_VALUE_TYPE,
    Application,
    ApplicationCommand,
    ApplicationCommandParameter,
)
from rey_lib.errors.error_utils import ConfigError
from rey_lib.run.app_channel import LaunchedApp

PYTHON = "/venv/bin/python"
CONFIG = "/installations/local/install"

SERVE = ApplicationCommand(name="serve", parameters=(
    ApplicationCommandParameter(name="workflow-id", required=True),
))
FLAGS = ApplicationCommand(name="index", parameters=(
    ApplicationCommandParameter(name="target", positional=True),
    ApplicationCommandParameter(name="dry-run"),
    ApplicationCommandParameter(name="limit"),
))


def _application(shared: bool = True) -> Application:
    return Application(name="workflow_app", app_path="/apps/workflow_app",
                       entry_point="main.py", shared_parameters=shared,
                       commands=(SERVE, FLAGS))


def _ctx(*applications: Application, config_path: Any = CONFIG) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(python=SimpleNamespace(executable=PYTHON)),
        config_path=config_path, applications=applications)


def test_a_semantic_input_becomes_the_published_parameter() -> None:
    assert SERVE.arguments({"workflow_id": 15}) == ["--workflow-id", "15"]


def test_flags_positionals_and_empty_values_take_their_published_form() -> None:
    assert FLAGS.arguments({"target": "repo", "dry_run": True, "limit": ""}) == [
        "repo", "--dry-run"]
    assert FLAGS.arguments({"dry_run": "false", "limit": 5}) == ["--limit", "5"]


def test_a_missing_required_or_unpublished_input_is_refused() -> None:
    with pytest.raises(ConfigError, match="requires 'workflow-id'"):
        SERVE.arguments({})
    with pytest.raises(ConfigError, match="publishes no parameter for workflow_name"):
        SERVE.arguments({"workflow_id": 15, "workflow_name": "x"})


STRUCTURED = ApplicationCommand(name="serve", parameters=(
    ApplicationCommandParameter(name="subject-kind", required=True),
    ApplicationCommandParameter(name="filters", value_type=JSON_VALUE_TYPE),
    ApplicationCommandParameter(name="arguments", value_type=JSON_VALUE_TYPE, required=True),
))


def test_a_json_parameter_is_one_canonical_json_argument() -> None:
    # #1143: a governed structured value travels as one argument, serialized
    # canonically -- sorted keys, no spaces -- never as str() or a Python repr.
    filters = [{"value": 15, "column": "run_id", "operator": "equals"}]
    assert STRUCTURED.arguments({
        "subject_kind": "views", "filters": filters, "arguments": {"b": "é", "a": None},
    }) == [
        "--subject-kind", "views",
        "--filters", '[{"column":"run_id","operator":"equals","value":15}]',
        "--arguments", '{"a":null,"b":"é"}',
    ]


def test_an_empty_structure_is_given_and_none_is_not() -> None:
    assert STRUCTURED.arguments({"subject_kind": "views", "filters": [], "arguments": {}}) == [
        "--subject-kind", "views", "--filters", "[]", "--arguments", "{}"]
    with pytest.raises(ConfigError, match="requires 'arguments'"):
        STRUCTURED.arguments({"subject_kind": "views", "arguments": None})


def test_a_value_that_is_not_json_is_refused() -> None:
    with pytest.raises(ConfigError, match="parameter 'filters' takes JSON"):
        STRUCTURED.arguments({"subject_kind": "views", "filters": [object()], "arguments": {}})
    with pytest.raises(ConfigError, match="parameter 'filters' takes JSON"):
        STRUCTURED.arguments({"subject_kind": "views", "filters": [float("nan")],
                              "arguments": {}})


def test_the_application_builds_its_invocation_from_its_configuration() -> None:
    # The line the Console's legacy build_app_command built for the same call.
    assert _application().command_line(_ctx(), "serve", {"workflow_id": 15}) == [
        PYTHON, "/apps/workflow_app/main.py", "--config-path", CONFIG,
        "serve", "--workflow-id", "15"]
    assert _application(shared=False).command_line(_ctx(), "serve", {"workflow_id": 15}) == [
        PYTHON, "/apps/workflow_app/main.py", "serve", "--workflow-id", "15"]


def test_an_unpublished_operation_or_missing_runtime_is_refused() -> None:
    with pytest.raises(ConfigError, match="publishes no operation 'run'"):
        _application().command_line(_ctx(), "run", {})
    with pytest.raises(ConfigError, match="config_path is required"):
        _application().command_line(_ctx(config_path=None), "serve", {"workflow_id": 1})
    with pytest.raises(ConfigError, match="python.executable is required"):
        _application().command_line(SimpleNamespace(config_path=CONFIG), "serve",
                                    {"workflow_id": 1})


def test_launch_operation_launches_the_applications_own_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(LaunchedApp, "launch", classmethod(
        lambda cls, command, **_: launched.append(list(command)) or "handle"))
    other = Application(name="data_engine", commands=(SERVE,))

    handle = LaunchedApp.launch_operation(_ctx(other, _application()), "workflow_app", "serve",
                                          {"workflow_id": 15})

    assert handle == "handle"
    assert launched == [[PYTHON, "/apps/workflow_app/main.py", "--config-path", CONFIG,
                         "serve", "--workflow-id", "15"]]


def test_launch_operation_refuses_an_unconfigured_application() -> None:
    with pytest.raises(ConfigError, match="'workflow_app' is not configured"):
        LaunchedApp.launch_operation(_ctx(), "workflow_app", "serve", {"workflow_id": 15})
