"""Tests for build_ctx_from_path with the new installation YAML shapes.

Covers:
- PathResolver built from config.yaml paths list
- {logicalname} references resolved throughout merged ctx
- App-level paths: dict does not corrupt PathResolver list
- Individual app.yaml finds parent PathResolver via _find_parent_install_raw
- App.yaml with paths: dict triggers parent search (not isinstance check)
- _root suffix keys resolved as Path objects (contracts_root, output_root)
- {operation} and {timestamp} placeholders survive resolution
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rey_lib.config.config_utils import PathResolver, build_ctx_from_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def install_root(tmp_path: Path) -> Path:
    """Minimal installation layout matching the new contract."""
    configs = tmp_path / "installations" / "ccc" / "configs" / "v01"
    configs.mkdir(parents=True)

    _write(configs / "config.yaml", f"""\
installation:
  name: ccc

paths:
  - name: root
    path: {tmp_path}

  - name: data
    path: "{{root}}/data"

  - name: logs
    path: "{{root}}/logs"

  - name: contracts
    path: "{{root}}/contracts"

  - name: console_log
    path: "{{logs}}/console/console.{{operation}}.{{timestamp}}.log"

  - name: analyzer_log
    path: "{{logs}}/analyzer/analyzer.{{operation}}.{{timestamp}}.log"

  - name: analyzer_jsonl
    path: "{{logs}}/analyzer/analyzer.{{operation}}.{{timestamp}}.jsonl"

  - name: analyzer_records
    path: "{{data}}/analyzer/records"

  - name: analyzer_contracts
    path: "{{contracts}}/v01"

  - name: redactor_log
    path: "{{logs}}/redactor/redactor.{{operation}}.{{timestamp}}.log"

  - name: redactor_output_root
    path: "{{data}}/redactor"

  - name: redactor_output
    path: "{{data}}/redactor/output/{{source_name}}"

  - name: dated_output
    path: "{{logs}}/{{date}}/{{yyyy}}/{{mm}}/{{dd}}/{{yyymm}}/{{yyymmdd}}.jsonl"
""")

    return configs


# ---------------------------------------------------------------------------
# PathResolver basics
# ---------------------------------------------------------------------------

class TestPathResolverBuilt:
    """PathResolver is created from the paths: list in config.yaml."""

    def test_ctx_paths_is_path_resolver(self, install_root: Path) -> None:
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert isinstance(ctx.paths, PathResolver)

    def test_named_paths_resolve(self, install_root: Path, tmp_path: Path) -> None:
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.paths.resolve("root") == tmp_path.resolve()
        assert ctx.paths.resolve("data") == (tmp_path / "data").resolve()
        assert ctx.paths.resolve("logs") == (tmp_path / "logs").resolve()

    def test_chained_paths_resolve(self, install_root: Path, tmp_path: Path) -> None:
        ctx = build_ctx_from_path(install_root / "config.yaml")
        expected = (tmp_path / "data" / "analyzer" / "records").resolve()
        assert ctx.paths.resolve("analyzer_records") == expected

    def test_date_tokens_use_one_startup_datetime(
        self, install_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """Both resolver passes and final ctx substitution share one clock read."""
        import rey_lib.config.config_context as config_context

        class FixedDatetime:
            calls = 0

            @classmethod
            def now(cls):
                cls.calls += 1
                return datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(config_context, "datetime", FixedDatetime)
        _write(install_root / "console" / "app.yaml", """\
name: console
payload_log_path: "{logs}/llm_evaluation/payloads.{yyymmdd}.jsonl"
""")

        ctx = build_ctx_from_path(install_root / "config.yaml")

        assert FixedDatetime.calls == 1
        assert ctx.paths.resolve("dated_output") == (
            tmp_path / "logs/20260720/2026/07/20/202607/20260720.jsonl"
        ).resolve()
        assert ctx.payload_log_path == str(
            (tmp_path / "logs/llm_evaluation/payloads.20260720.jsonl").resolve()
        )


# ---------------------------------------------------------------------------
# {logicalname} references substituted
# ---------------------------------------------------------------------------

class TestLogicalRefSubstitution:
    """Logical {name} placeholders in app yamls are replaced after PathResolver is built."""

    def test_log_path_logicalname_resolved(self, install_root: Path, tmp_path: Path) -> None:
        _write(install_root / "console" / "app.yaml", """\
name: console
enabled: true
log_path: "{console_log}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        log_path = str(ctx.log_path)
        assert str(tmp_path.resolve()) in log_path

    def test_operation_timestamp_survive(self, install_root: Path) -> None:
        """Runtime placeholders must not be consumed by PathResolver."""
        _write(install_root / "console" / "app.yaml", """\
name: console
log_path: "{console_log}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        log_path = str(ctx.log_path)
        assert "{operation}" in log_path
        assert "{timestamp}" in log_path

    def test_contracts_root_resolved_as_path(self, install_root: Path, tmp_path: Path) -> None:
        """Keys ending in _root are resolved to Path objects."""
        _write(install_root / "analyzer" / "app.yaml", """\
name: analyzer
contracts_root: "{analyzer_contracts}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        expected = (tmp_path / "contracts" / "v01").resolve()
        assert ctx.contracts_root == expected
        assert isinstance(ctx.contracts_root, Path)

    def test_output_root_resolved_as_path(self, install_root: Path, tmp_path: Path) -> None:
        _write(install_root / "redactor" / "app.yaml", """\
name: redactor
paths:
  output_root: "{redactor_output_root}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        expected = (tmp_path / "data" / "redactor").resolve()
        assert ctx.paths.resolve("redactor_output_root") == expected


# ---------------------------------------------------------------------------
# App-level paths: dict does not corrupt PathResolver
# ---------------------------------------------------------------------------

class TestPathsDictIsolation:
    """A paths: dict in an app yaml must never overwrite the PathResolver list."""

    def test_paths_dict_does_not_replace_resolver(self, install_root: Path) -> None:
        _write(install_root / "redactor" / "app.yaml", """\
name: redactor
log_path: "{redactor_log}"
paths:
  output_root: "{redactor_output_root}"
  output: "{redactor_output}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert isinstance(ctx.paths, PathResolver), (
            "paths: dict from app yaml replaced the PathResolver"
        )

    def test_named_paths_still_resolve_after_app_paths_dict(
        self, install_root: Path, tmp_path: Path
    ) -> None:
        _write(install_root / "redactor" / "app.yaml", """\
name: redactor
paths:
  output_root: "{redactor_output_root}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.paths.resolve("logs") == (tmp_path / "logs").resolve()

    def test_log_path_still_resolved_when_app_has_paths_dict(
        self, install_root: Path, tmp_path: Path
    ) -> None:
        _write(install_root / "redactor" / "app.yaml", """\
name: redactor
log_path: "{redactor_log}"
paths:
  output_root: "{redactor_output_root}"
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert str(tmp_path.resolve()) in str(ctx.log_path)
        assert "{operation}" in str(ctx.log_path)


# ---------------------------------------------------------------------------
# Individual app.yaml loading finds parent PathResolver
# ---------------------------------------------------------------------------

class TestParentPathResolverDiscovery:
    """build_ctx_from_path on an app.yaml climbs to find the parent config.yaml."""

    def test_app_yaml_without_paths_gets_resolver(self, install_root: Path) -> None:
        app_yaml = install_root / "analyzer" / "app.yaml"
        _write(app_yaml, """\
name: analyzer
log_path: "{analyzer_log}"
""")
        ctx = build_ctx_from_path(app_yaml)
        assert isinstance(ctx.paths, PathResolver)

    def test_app_yaml_logicalref_resolved_from_parent(
        self, install_root: Path, tmp_path: Path
    ) -> None:
        app_yaml = install_root / "analyzer" / "app.yaml"
        _write(app_yaml, """\
name: analyzer
log_path: "{analyzer_log}"
jsonl_path: "{analyzer_jsonl}"
""")
        ctx = build_ctx_from_path(app_yaml)
        assert str(tmp_path.resolve()) in str(ctx.log_path)
        assert str(tmp_path.resolve()) in str(ctx.jsonl_path)
        assert "{operation}" in str(ctx.log_path)
        assert "{timestamp}" in str(ctx.jsonl_path)

    def test_app_yaml_with_paths_dict_still_finds_parent(self, install_root: Path) -> None:
        """The isinstance check must trigger parent search when paths: is a dict."""
        app_yaml = install_root / "redactor" / "app.yaml"
        _write(app_yaml, """\
name: redactor
log_path: "{redactor_log}"
paths:
  output_root: "{redactor_output_root}"
""")
        ctx = build_ctx_from_path(app_yaml)
        assert isinstance(ctx.paths, PathResolver), (
            "PathResolver not found — isinstance check on paths: dict failed"
        )
        assert "{operation}" in str(ctx.log_path)

    def test_app_yaml_contracts_root_resolved_as_path(
        self, install_root: Path, tmp_path: Path
    ) -> None:
        app_yaml = install_root / "analyzer" / "app.yaml"
        _write(app_yaml, """\
name: analyzer
contracts_root: "{analyzer_contracts}"
""")
        ctx = build_ctx_from_path(app_yaml)
        expected = (tmp_path / "contracts" / "v01").resolve()
        assert isinstance(ctx.contracts_root, Path)
        assert ctx.contracts_root == expected


# ---------------------------------------------------------------------------
# Installation-level settings pass through
# ---------------------------------------------------------------------------

class TestInstallationSettings:
    """Non-path keys in config.yaml survive into ctx."""

    def test_installation_name(self, install_root: Path) -> None:
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.installation.name == "ccc"

    def test_app_enabled_flag(self, install_root: Path) -> None:
        _write(install_root / "console" / "app.yaml", """\
name: console
enabled: true
version: v01
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.enabled is True

    def test_bool_flag_in_app_yaml(self, install_root: Path) -> None:
        _write(install_root / "coordinator" / "app.yaml", """\
name: coordinator
fail_if_venv_missing: true
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.fail_if_venv_missing is True

    def test_nested_messaging_block(self, install_root: Path) -> None:
        _write(install_root / "messaging" / "app.yaml", """\
name: messaging
messaging:
  delivery:
    dry_run: true
  pipeline_summary:
    record_limit: 500
""")
        ctx = build_ctx_from_path(install_root / "config.yaml")
        assert ctx.messaging.delivery.dry_run is True
        assert ctx.messaging.pipeline_summary.record_limit == 500
