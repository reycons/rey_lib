"""DatabaseTrigger owns its DDL (backlog 695).

A trigger's DDL used to be a branch in each provider's ``get_object_ddl``,
found by PostgreSQL's oid or SQL Server's object_id. It is
``DatabaseTrigger.read_ddl`` now: the server's stored definition is a
retrieved fact (``DBAdapter.trigger_native_definition``), addressed by the
trigger's table and its own name -- a trigger name alone is not unique -- and
the trigger ends the statement for its provider.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTrigger
from rey_lib.db.ddl_export import DatabaseDdlExport
from rey_lib.errors.error_utils import DatabaseError, UnsupportedDatabaseCapabilityError

_TABLE = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="run_log")


class _Connector:
    """Answers one provider's stored trigger definition, recording what was asked."""

    def __init__(self, provider: str, source: str) -> None:
        self._provider = provider
        self.source = source
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def trigger_native_definition(self, _conn: Any, schema: str, table: str, name: str) -> str:
        self.asked.append((schema, table, name))
        if self._provider == "duckdb":
            raise UnsupportedDatabaseCapabilityError("duckdb has no triggers")
        return self.source


@pytest.mark.parametrize(("provider", "source", "expected"), [
    ("postgres",
     "CREATE TRIGGER run_log_audit AFTER INSERT ON control.run_log "
     "FOR EACH ROW EXECUTE FUNCTION control.audit()",
     "CREATE TRIGGER run_log_audit AFTER INSERT ON control.run_log "
     "FOR EACH ROW EXECUTE FUNCTION control.audit();"),
    ("mysql", "CREATE TRIGGER `run_log_audit` AFTER INSERT ON `run_log` FOR EACH ROW SET @x = 1",
     "CREATE TRIGGER `run_log_audit` AFTER INSERT ON `run_log` FOR EACH ROW SET @x = 1;"),
    ("sqlserver", "CREATE TRIGGER control.run_log_audit ON control.run_log AFTER INSERT AS SELECT 1\n",
     "CREATE TRIGGER control.run_log_audit ON control.run_log AFTER INSERT AS SELECT 1\nGO"),
])
def test_each_provider_s_definition_ends_as_its_script_needs(
    provider: str, source: str, expected: str,
) -> None:
    connector = _Connector(provider, source)

    assert DatabaseTrigger.read_ddl(object(), _TABLE, "run_log_audit", connector) == expected
    assert connector.asked == [("control", "run_log", "run_log_audit")]


def test_a_trigger_the_table_does_not_have_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="control.run_log.gone"):
        DatabaseTrigger.read_ddl(object(), _TABLE, "gone", _Connector("postgres", ""))


def test_a_provider_without_triggers_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedDatabaseCapabilityError, match="duckdb"):
        DatabaseTrigger.read_ddl(object(), _TABLE, "run_log_audit", _Connector("duckdb", ""))


def test_postgres_retrieves_by_table_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict] = []
    monkeypatch.setattr(postgres_utils, "fetch_rows", lambda _c, _sql, params: asked.append(
        params) or [{"definition": "CREATE TRIGGER t ..."}])

    assert postgres_utils.trigger_native_definition(
        object(), "control", "run_log", "t",
    ) == "CREATE TRIGGER t ..."
    assert asked == [{"schema": "control", "table": "run_log", "name": "t"}]


def test_sql_server_with_no_such_trigger_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda *_a: [])

    assert sqlserver_utils.trigger_native_definition(object(), "control", "run_log", "t") == ""


class _Cursor:
    """A psycopg cursor answering the export listing with one trigger."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql_text: str, *_a: Any) -> None:
        self.statements.append(sql_text)

    def fetchall(self) -> list[tuple]:
        if "WITH base AS" in self.statements[-1]:
            return [("triggers", "control", "run_log_audit", 4242, None, "run_log")]
        return []

    def fetchone(self) -> tuple:
        return ("rey",)

    def close(self) -> None:
        pass


def test_the_postgres_listing_names_each_trigger_s_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(postgres_utils, "_postgres_dependencies", lambda *_a, **_k: [])
    conn = types.SimpleNamespace(cursor=lambda: cursor)

    (listed,) = postgres_utils.list_database_objects(conn, database="rey")

    assert (listed["object_type"], listed["name"], listed["table"]) == (
        "triggers", "run_log_audit", "run_log",
    )
    (asked,) = [sql for sql in cursor.statements if "WITH base AS" in sql]
    # From the catalog itself, one row per trigger: information_schema.triggers
    # shows only what the reading role holds privileges on (backlog 760).
    assert "FROM pg_trigger tr" in asked and "NOT tr.tgisinternal" in asked
    assert "FROM information_schema.triggers" not in asked


def test_the_ddl_export_asks_the_trigger_by_its_table(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export lists and orders; the trigger writes itself, found by table and name."""
    asked: list[tuple] = []
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: [
            {"database": "rey", "object_type": "triggers", "schema": "control",
             "name": "run_log_audit", "table": "run_log", "object_oid": 4242,
             "dependencies": []},
        ],
        trigger_native_definition=lambda _c, schema, table, name: asked.append(
            (schema, table, name)) or "CREATE TRIGGER run_log_audit ...",
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)
    conn = types.SimpleNamespace(provider="postgres")

    DatabaseDdlExport.export(conn, connection="rey", output_root=str(tmp_path))

    assert asked == [("control", "run_log", "run_log_audit")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "run_log_audit" in p.name]
    assert written.read_text(encoding="utf-8") == "CREATE TRIGGER run_log_audit ...;\n"
