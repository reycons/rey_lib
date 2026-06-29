"""Compatibility alias tests for logical config reorganization."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.config.cli import build_ctx_from_args
from rey_lib.config.config_utils import build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _root(tmp_path: Path) -> Path:
    config_dir = tmp_path / "configs"
    _write(config_dir / "config.yaml", f"""\
installation:
  name: local
paths:
  - name: root
    path: {tmp_path}
""")
    return config_dir


def test_db_connections_alias_to_connections(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "shared" / "db.yaml", """\
db_connections:
  - name: rey_apps
    provider: postgres
""")

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert ctx.db_connections[0].name == "rey_apps"
    assert ctx.connections[0].name == "rey_apps"


def test_connections_alias_to_db_connections(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "shared" / "connections.yaml", """\
connections:
  - name: rey_apps
    provider: postgres
""")

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert ctx.connections[0].name == "rey_apps"
    assert ctx.db_connections[0].name == "rey_apps"


def test_conflicting_duplicate_connections_fail_closed(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "shared" / "db.yaml", """\
db_connections:
  - name: rey_apps
    provider: postgres
connections:
  - name: rey_apps
    provider: sqlserver
""")

    with pytest.raises(ConfigError, match="rey_apps"):
        build_ctx_from_path(config_dir / "config.yaml")


def test_llm_profiles_alias_to_llm(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "shared" / "llm_profiles.yaml", """\
llm_profiles:
  - name: local
    provider: ollama
    model: qwen2.5-coder:7b
""")

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert ctx.llm_profiles[0].name == "local"
    assert ctx.llm[0].name == "local"


def test_pipeline_coordinator_pipelines_alias_to_top_level(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "pipelines" / "legacy.yaml", """\
pipeline_coordinator:
  pipelines:
    file_onboarder:
      enabled: true
""")

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert ctx.pipeline_coordinator.pipelines.file_onboarder.enabled is True
    assert ctx.pipelines.file_onboarder.enabled is True


def test_top_level_pipelines_alias_to_pipeline_coordinator(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "pipelines" / "logical.yaml", """\
pipelines:
  file_onboarder:
    enabled: true
""")

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert ctx.pipelines.file_onboarder.enabled is True
    assert ctx.pipeline_coordinator.pipelines.file_onboarder.enabled is True


def test_conflicting_duplicate_pipelines_fail_closed(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "pipelines" / "conflict.yaml", """\
pipeline_coordinator:
  pipelines:
    file_onboarder:
      enabled: true
pipelines:
  file_onboarder:
    enabled: false
""")

    with pytest.raises(ConfigError, match="file_onboarder"):
        build_ctx_from_path(config_dir / "config.yaml")


def test_conflicting_duplicate_named_list_entries_fail_closed(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "sources" / "a.yaml", """\
data_sources:
  - name: trades
    file_pattern: "*.csv"
""")
    _write(config_dir / "sources" / "b.yaml", """\
data_sources:
  - name: trades
    file_pattern: "*.json"
""")

    with pytest.raises(ConfigError, match="trades"):
        build_ctx_from_path(config_dir / "config.yaml")


def test_identical_duplicate_named_list_entries_remain_compatible(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    duplicate = """\
data_sources:
  - name: trades
    file_pattern: "*.csv"
"""
    _write(config_dir / "sources" / "a.yaml", duplicate)
    _write(config_dir / "sources" / "b.yaml", duplicate)

    ctx = build_ctx_from_path(config_dir / "config.yaml")

    assert len(ctx.data_sources) == 1
    assert ctx.data_sources[0].name == "trades"


def test_cli_ctx_build_reports_duplicate_config_before_logging(tmp_path: Path) -> None:
    config_dir = _root(tmp_path)
    _write(config_dir / "sources" / "a.yaml", """\
data_sources:
  - name: trades
    file_pattern: "*.csv"
""")
    _write(config_dir / "sources" / "b.yaml", """\
data_sources:
  - name: trades
    file_pattern: "*.json"
""")
    args = SimpleNamespace(
        config_path=str(config_dir / "config.yaml"),
        ctx_file=None,
    )

    with pytest.raises(SystemExit) as exc_info:
        build_ctx_from_args(args, app_name="rey_loader")

    assert "FATAL: failed to load config" in str(exc_info.value)
    assert "trades" in str(exc_info.value)
