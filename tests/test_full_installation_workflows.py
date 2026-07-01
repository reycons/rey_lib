"""Tests for full-installation ctx scope and workflow ownership stamping.

Covers the workflow inventory app-assignment contract at the single discovery
phase (ctx construction):

- App-scoped ctx sees only the workflows merged by that app's include list.
- ``full_installation=True`` merges every app's workflows into one ctx.
- ``app_name`` identity is preserved even in full-installation mode.
- Each resolved workflow carries its own ``app`` owner (ownership survives the
  concatenating deep-merge instead of collapsing to a single root ``app``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.config_utils import build_ctx_from_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, creating parents; return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def multi_app_install(tmp_path: Path) -> Path:
    """Installation with two apps owning distinct workflow files.

    ``default_behavior`` is ``none`` and each app has an explicit include list,
    mirroring the real installation: only ``full_installation`` can surface
    every app's workflows in one ctx.
    """
    cfg = tmp_path / "install"
    (cfg / "workflows" / "rey_loader").mkdir(parents=True)
    (cfg / "workflows" / "rey_db_admin").mkdir(parents=True)

    _write(cfg / "config.yaml", f"""\
installation:
  name: test

paths:
  - name: root
    path: {tmp_path}
  - name: configs
    path: {cfg}

config_loading:
  default_behavior: none
  apps:
    rey_console:
      include:
        - '{{configs}}/workflows/rey_loader'
    rey_loader:
      include:
        - '{{configs}}/workflows/rey_loader'
    rey_db_admin:
      include:
        - '{{configs}}/workflows/rey_db_admin'
""")

    _write(cfg / "workflows" / "rey_loader" / "internal_etl.yaml", """\
app: rey_loader
workflows:
  - name: transform_load
    steps: [transform-files, load-files]
  - name: sql_apply
    steps: [sql-apply]
""")

    _write(cfg / "workflows" / "rey_db_admin" / "versioning.yaml", """\
app: rey_db_admin
workflows:
  - name: postgres_version_lint_comment
    steps: [export-ddl-before, lint-sql]
""")

    return cfg / "config.yaml"


def _workflow_pairs(ctx: object) -> list[tuple[str, str]]:
    """Return ``(name, app)`` for every workflow on the resolved ctx."""
    pairs: list[tuple[str, str]] = []
    for wf in getattr(ctx, "workflows", None) or []:
        pairs.append((str(getattr(wf, "name", "")), str(getattr(wf, "app", ""))))
    return pairs


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestFullInstallationScope:
    """App-scoped vs full-installation ctx workflow visibility."""

    def test_app_scoped_console_sees_only_included_app(self, multi_app_install: Path) -> None:
        """The rey_console include list merges only rey_loader workflows."""
        ctx = build_ctx_from_path(multi_app_install, app_name="rey_console")
        names = {name for name, _ in _workflow_pairs(ctx)}
        assert names == {"transform_load", "sql_apply"}
        assert "postgres_version_lint_comment" not in names

    def test_full_installation_includes_all_app_workflows(
        self, multi_app_install: Path
    ) -> None:
        """full_installation merges every app's workflows into one ctx."""
        ctx = build_ctx_from_path(
            multi_app_install, app_name="rey_console", full_installation=True
        )
        names = {name for name, _ in _workflow_pairs(ctx)}
        assert names == {"transform_load", "sql_apply", "postgres_version_lint_comment"}

    def test_full_installation_preserves_app_identity(self, multi_app_install: Path) -> None:
        """Requesting full scope does not change the recorded app identity."""
        ctx = build_ctx_from_path(
            multi_app_install, app_name="rey_console", full_installation=True
        )
        assert ctx.app_name == "rey_console"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

class TestWorkflowOwnershipSurvivesMerge:
    """Each resolved workflow keeps its own file-root app owner after merge."""

    def test_each_workflow_keeps_its_owner(self, multi_app_install: Path) -> None:
        ctx = build_ctx_from_path(
            multi_app_install, app_name="rey_console", full_installation=True
        )
        owners = dict(_workflow_pairs(ctx))
        assert owners["transform_load"] == "rey_loader"
        assert owners["sql_apply"] == "rey_loader"
        assert owners["postgres_version_lint_comment"] == "rey_db_admin"
