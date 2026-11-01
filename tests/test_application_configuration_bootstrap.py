"""All app startup configuration is hydrated through the shared bootstrap."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rey_lib.config import bootstrap
from rey_lib.config.applications import configured_application
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import PathResolver
from rey_lib.installation.installation import Installation
from rey_lib.errors.error_utils import AppError


def runtime(tmp_path, monkeypatch):
    registrations = {name: {"name": name, "entry_point": "main.py", "cli": {}}
                     for name in ("one", "two")}
    monkeypatch.setattr("rey_lib.config.applications.discover_registrations", lambda: registrations)
    control = Mock()
    control.get_application_installations.return_value = [
        {"application": name, "is_enabled": True, "app_path": "{apps}/" + name,
         "settings": {"server": {"port": port}, "log_path": "{apps}/run.{operation}.log"},
         "ai": {"tasks": [{"task_key": "query"}]} if name == "one" else {}}
        for name, port in (("one", 1234), ("two", 5678))]
    ctx = Namespace({"app_name": "one", "applications": None})
    ctx.installation = Installation(name="test", installation_id=9)
    ctx.paths = PathResolver({"apps": tmp_path}, {})
    ctx.shared_control = control
    return ctx, control


def test_shared_bootstrap_reads_once_and_objects_own_isolated_settings(tmp_path, monkeypatch):
    ctx, control = runtime(tmp_path, monkeypatch)
    bootstrap.open_shared_control(ctx)
    bootstrap.open_shared_control(ctx)
    control.get_application_installations.assert_called_once_with(9)
    one, two = ctx.applications
    assert ctx.application is one
    assert configured_application(ctx, "one") is one
    assert one.settings["server"]["port"] == 1234
    assert two.settings["server"]["port"] == 5678
    assert one.app_path == str(tmp_path / "one")
    assert one.settings["log_path"] == str(tmp_path / "run.{operation}.log")
    assert one.ai["tasks"][0]["task_key"] == "query"
    assert not hasattr(ctx, "server")
    assert not hasattr(ctx, "apps")


def test_a_database_read_failure_is_not_an_empty_or_yaml_configuration(tmp_path, monkeypatch):
    ctx, control = runtime(tmp_path, monkeypatch)
    control.get_application_installations.side_effect = AppError("Configuration read refused")
    with pytest.raises(AppError, match="read refused"):
        bootstrap.open_shared_control(ctx)
    assert ctx.applications is None


def test_a_disabled_application_is_not_constructed(tmp_path, monkeypatch):
    ctx, control = runtime(tmp_path, monkeypatch)
    control.get_application_installations.return_value[1]["is_enabled"] = False
    bootstrap.open_shared_control(ctx)
    assert [app.name for app in ctx.applications] == ["one"]


@pytest.mark.parametrize("adopted", [False, True])
def test_process_bootstrap_loads_before_logging_and_reuses_control(tmp_path, monkeypatch, adopted):
    ctx, control = runtime(tmp_path, monkeypatch)
    if adopted:
        ctx.run_id = 42
    monkeypatch.setattr(bootstrap, "build_connections", lambda ctx: {})
    monkeypatch.setattr(bootstrap, "register_runtime_object", lambda *args: None)
    opener = Mock(side_effect=AssertionError("Control must be reused"))
    monkeypatch.setattr(bootstrap, "_open_control", opener)
    start = Mock(return_value=SimpleNamespace(run_id=42))
    monkeypatch.setattr(bootstrap.Run, "start", start)
    monkeypatch.setattr(bootstrap, "establish_run_identity", lambda ctx: None)
    monkeypatch.setattr("rey_lib.logs.log_writer.build_log_writer", lambda *args: None)
    configured = []
    monkeypatch.setattr(bootstrap.AppLogger, "_configure",
                        lambda ctx, *args: configured.append(ctx.application.settings))
    assert bootstrap.build_ctx_for_app(ctx=ctx, operation="test") is ctx
    control.get_application_installations.assert_called_once_with(9)
    opener.assert_not_called()
    assert start.call_count == (0 if adopted else 1)
    assert configured[0]["server"]["port"] == 1234


def test_invalid_database_settings_are_refused_not_normalized_to_empty(tmp_path, monkeypatch):
    ctx, control = runtime(tmp_path, monkeypatch)
    control.get_application_installations.return_value[0]["settings"] = []
    with pytest.raises(AppError, match="mapping"):
        bootstrap.open_shared_control(ctx)
