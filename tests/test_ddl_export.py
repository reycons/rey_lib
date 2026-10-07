"""DatabaseDdlExport: export orchestration only (backlog 723).

It takes the database from DatabaseObjectListing, asks each canonical object
for its own DDL, and owns selection, coherence, dependency order, files, the
manifest and build.sql. It writes no SQL and never reaches DBAdapter or a
provider: the adapter it is handed is the database objects' to use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.db import ddl_export, postgres_utils
from rey_lib.db.ddl_export import DatabaseDdlExport
from rey_lib.errors.error_utils import DatabaseError


class _Adapter:
    """What the database objects ask, answered as PostgreSQL."""

    def __init__(self, inventory: list[dict[str, Any]]) -> None:
        self.inventory = inventory

    def provider(self, _conn: Any) -> str:
        return "postgres"

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return postgres_utils.quote_identifier(name)

    def list_database_objects(self, _conn: Any, database: str | None = None) -> list:
        return self.inventory

    def table_column_catalog(self, _conn: Any, _schema: str, _table: str, **_k: Any) -> list:
        return [{"name": "run_id", "type": "integer", "default": None, "nullable": False}]


_INVENTORY = [
    {"database": "rey", "object_type": "table", "schema": "control", "name": "run",
     "dependencies": [{"object_type": "schema", "schema": "control", "name": "control"}]},
    {"database": "rey", "object_type": "schema", "schema": "control", "name": "control"},
]


def test_it_writes_each_object_s_own_ddl_dependencies_first(tmp_path: Path) -> None:
    result = DatabaseDdlExport.export(
        object(), output_root=str(tmp_path), adapter=_Adapter(_INVENTORY),
    )

    assert result["objects"] == ["schemas:control.control", "tables:control.run"]
    manifest = result["build_manifest"]
    assert [one["key"] for one in manifest["objects"]] == result["objects"]
    assert manifest["objects"][1]["dependencies"] == ["schemas:control.control"]
    written = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.sql")}
    assert written["control.control.sql"] == 'CREATE SCHEMA IF NOT EXISTS "control";\n'
    assert written["control.run.sql"].startswith('CREATE TABLE "control"."run"')


def test_validate_refuses_an_entry_no_object_owns() -> None:
    """M4: before any finding, an unowned kind makes the export invalid."""
    adapter = _Adapter([*_INVENTORY, {"object_type": "rule", "schema": "control", "name": "r"}])

    with pytest.raises(DatabaseError, match="No database object owns the DDL of a 'rule'"):
        DatabaseDdlExport.validate(object(), adapter=adapter)


def test_validate_reports_what_is_incoherent() -> None:
    adapter = _Adapter([
        {"database": "rey", "object_type": "table", "schema": "control", "name": "run",
         "dependencies": ["tables:control.gone", "not a reference"]},
    ])

    found = DatabaseDdlExport.validate(object(), adapter=adapter)

    assert found["provider"] == "postgres" and found["database"] == "rey"
    assert found["missing_dependencies"] == ["tables:control.run -> tables:control.gone"]
    assert found["invalid_references"] == ["tables:control.run -> <invalid dependency>"]


def test_a_cycle_is_named() -> None:
    adapter = _Adapter([
        {"object_type": "table", "schema": "s", "name": "a", "dependencies": ["tables:s.b"]},
        {"object_type": "table", "schema": "s", "name": "b", "dependencies": ["tables:s.a"]},
    ])

    with pytest.raises(DatabaseError, match="Circular dependencies detected"):
        DatabaseDdlExport.validate(object(), adapter=adapter)


def test_the_export_reaches_no_adapter_or_provider() -> None:
    """Orchestration only: no DBAdapter, no provider module, no inventory row."""
    source = Path(ddl_export.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]

    for reached in ("db_adapter", "DBAdapter", "_backend", "list_database_objects",
                    "postgres_utils", "mysql_utils", "sqlserver_utils", "duckdb_utils",
                    "_CONSTRAINT_OWNER", "CREATE "):
        assert reached not in code, reached
