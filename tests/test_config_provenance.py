"""Tests for config value provenance metadata.

Covers SGC_Config_Utils_Value_Provenance_Metadata:
- runtime config values stay plain (non-breaking / additive)
- metadata is retrievable separately by dotted path
- token dependencies are captured
- raw and resolved values are preserved for tokenized strings
- override history is preserved across layered merges
- source files can be listed for a config subtree
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.config_utils import build_ctx_from_path
from rey_lib.config.provenance import (
    ConfigMetadata,
    ConfigValueMetadata,
    extract_dependencies,
    get_config_metadata,
    get_config_source_files,
)


def _write(path: Path, text: str) -> Path:
    """Write *text* to *path*, creating parents, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def install_root(tmp_path: Path) -> Path:
    """A minimal installation with path tokens and a workflow file."""
    configs = tmp_path / "installations" / "local" / "configs" / "v01"
    configs.mkdir(parents=True)

    _write(configs / "config.yaml", f"""\
installation:
  name: local

paths:
  - name: root
    path: {tmp_path}

  - name: data
    path: "{{root}}/data"

  - name: ddl_root
    path: "{{data}}/rey_db_admin/database_ddl"

  - name: logs
    path: "{{root}}/logs"
""")

    _write(configs / "workflows" / "rey_db_admin.yaml", """\
workflows:
  - name: postgres_version_lint_comment
    app: rey_db_admin
    description: DDL versioning workflow
    steps:
      - name: export
        process: export_database_ddl
""")

    return configs


def test_extract_dependencies_conservative() -> None:
    """extract_dependencies records unique {token} names in order."""
    assert extract_dependencies("{data}/x/{data}/{logs}") == ["data", "logs"]
    assert extract_dependencies("/no/tokens") == []
    assert extract_dependencies(123) == []


def test_runtime_values_remain_plain(install_root: Path) -> None:
    """§11.1 — resolved config values stay plain, not metadata wrappers."""
    ctx = build_ctx_from_path(install_root / "config.yaml")

    data_path = ctx.paths.resolve("data")
    assert isinstance(data_path, Path)
    assert not isinstance(data_path, ConfigValueMetadata)
    assert isinstance(ctx.installation.name, str)


def test_metadata_exists_separately(install_root: Path) -> None:
    """§11.3 — metadata is retrievable and matches the resolved value."""
    ctx = build_ctx_from_path(install_root / "config.yaml")

    meta = get_config_metadata(ctx, "paths.data")
    assert meta is not None
    assert meta.raw_value == "{root}/data"
    assert meta.resolved_value == str(ctx.paths.resolve("data"))
    assert meta.source_file is not None
    assert meta.source_file.endswith("config.yaml")
    assert meta.source_section == "paths"
    assert meta.layer == "installation"


def test_token_dependencies_captured(install_root: Path) -> None:
    """§11.5 — a token that references another records the dependency."""
    ctx = build_ctx_from_path(install_root / "config.yaml")

    meta = get_config_metadata(ctx, "paths.ddl_root")
    assert meta is not None
    assert meta.depends_on == ["data"]
    assert meta.raw_value == "{data}/rey_db_admin/database_ddl"
    assert meta.resolved_value.endswith("/rey_db_admin/database_ddl")
    assert meta.resolved_value == str(ctx.paths.resolve("ddl_root"))


def test_relevant_file_listing(install_root: Path) -> None:
    """§11.6 — source files can be listed for a config subtree."""
    ctx = build_ctx_from_path(install_root / "config.yaml")

    files = get_config_source_files(
        ctx, prefix="workflows.postgres_version_lint_comment"
    )
    assert files
    assert any("rey_db_admin" in f for f in files)

    # Workflow values are classified under the workflow layer.
    meta = get_config_metadata(
        ctx, "workflows.postgres_version_lint_comment.description"
    )
    assert meta is not None
    assert meta.layer == "workflow"


def test_override_history_preserved(tmp_path: Path) -> None:
    """§11.4 — a value overridden by a later layer keeps its prior entry."""
    metadata = ConfigMetadata()
    metadata.record_tree(
        {"paths": [{"name": "data", "path": "/defaults/data"}]},
        source_file="defaults/tokens.yaml",
        layer="default",
    )
    metadata.record_tree(
        {"paths": [{"name": "data", "path": "/install/data"}]},
        source_file="installations/local/tokens.yaml",
        layer="installation",
    )

    meta = metadata.get("paths.data")
    assert meta is not None
    assert meta.raw_value == "/install/data"
    assert meta.source_file.endswith("installations/local/tokens.yaml")
    assert len(meta.overrides) == 1
    assert meta.overrides[0].raw_value == "/defaults/data"
    assert meta.overrides[0].source_file.endswith("defaults/tokens.yaml")


def test_no_override_when_value_unchanged() -> None:
    """Re-recording the same value does not create spurious override history."""
    metadata = ConfigMetadata()
    metadata.record_value("tokens.x", "/same", source_file="a.yaml", layer="default")
    metadata.record_value("tokens.x", "/same", source_file="b.yaml", layer="installation")

    meta = metadata.get("tokens.x")
    assert meta is not None
    assert meta.overrides == []
