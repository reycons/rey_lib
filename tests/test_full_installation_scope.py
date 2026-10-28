"""Tests for full-installation ctx scope.

Covers the include scope at the single discovery phase (ctx construction):

- An app-scoped ctx sees only what that app's include list merges.
- ``full_installation=True`` merges every app's includes into one ctx.
- ``app_name`` identity is preserved even in full-installation mode.

Workflow ownership stamping is retired with workflow YAML (backlog 1011); the
fixture's per-app files declare data sources instead.
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
    """Installation with two apps each including their own data-source file.

    ``default_behavior`` is ``none`` and each app has an explicit include list,
    mirroring the real installation: only ``full_installation`` can surface
    every app's includes in one ctx.
    """
    cfg = tmp_path / "install"

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
        - '{{configs}}/sources/data_engine'
    data_engine:
      include:
        - '{{configs}}/sources/data_engine'
    rey_db_admin:
      include:
        - '{{configs}}/sources/rey_db_admin'
""")

    _write(cfg / "sources" / "data_engine" / "internal_etl.yaml", """\
data_sources:
  - name: transform_load
  - name: sql_apply
""")

    _write(cfg / "sources" / "rey_db_admin" / "versioning.yaml", """\
data_sources:
  - name: postgres_ddl_export
""")

    return cfg / "config.yaml"


def _source_names(ctx: object) -> set[str]:
    """Return the name of every data source on the resolved ctx."""
    return {str(getattr(one, "name", "")) for one in getattr(ctx, "data_sources", None) or []}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestFullInstallationScope:
    """App-scoped vs full-installation ctx visibility."""

    def test_app_scoped_console_sees_only_included_app(self, multi_app_install: Path) -> None:
        """The rey_console include list merges only rey_loader's file."""
        ctx = build_ctx_from_path(multi_app_install, app_name="rey_console")
        assert _source_names(ctx) == {"transform_load", "sql_apply"}

    def test_full_installation_includes_every_app(self, multi_app_install: Path) -> None:
        """full_installation merges every app's includes into one ctx."""
        ctx = build_ctx_from_path(
            multi_app_install, app_name="rey_console", full_installation=True
        )
        assert _source_names(ctx) == {"transform_load", "sql_apply", "postgres_ddl_export"}

    def test_full_installation_preserves_app_identity(self, multi_app_install: Path) -> None:
        """Requesting full scope does not change the recorded app identity."""
        ctx = build_ctx_from_path(
            multi_app_install, app_name="rey_console", full_installation=True
        )
        assert ctx.app_name == "rey_console"
