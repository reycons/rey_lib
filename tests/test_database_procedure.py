"""DatabaseProcedure owns its DDL (backlog 695).

A procedure's DDL used to be a branch in each provider's ``get_object_ddl``;
it is ``DatabaseProcedure.read_ddl`` now. The server's stored source is a
retrieved fact (``DBAdapter.routine_native_definition``), addressed by Rey's
routine identity -- schema, name, signature -- and never by a provider handle
such as PostgreSQL's oid. The procedure ends the statement for its provider.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseProcedure, DatabaseRoutineIdentity
from rey_lib.db.ddl_export import DatabaseDdlExport
from rey_lib.errors.error_utils import DatabaseError, UnsupportedDatabaseCapabilityError

_IDENTITY = DatabaseRoutineIdentity(
    connection="c", catalog="rey", schema="control", name="load_runs", signature="integer",
)


class _Connector:
    """Answers one provider's stored routine source, recording what was asked."""

    def __init__(self, provider: str, source: str) -> None:
        self._provider = provider
        self.source = source
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def routine_native_definition(
        self, _conn: Any, schema: str, name: str, signature: str, kind: str,
    ) -> str:
        self.asked.append((schema, name, signature, kind))
        if self._provider == "duckdb":
            raise UnsupportedDatabaseCapabilityError("duckdb has no routines")
        return self.source


@pytest.mark.parametrize(("provider", "source", "expected"), [
    ("postgres", "CREATE OR REPLACE PROCEDURE control.load_runs(integer) ...;",
     "CREATE OR REPLACE PROCEDURE control.load_runs(integer) ...;"),
    ("mysql", "CREATE PROCEDURE `load_runs`() BEGIN END",
     "CREATE PROCEDURE `load_runs`() BEGIN END;"),
    ("sqlserver", "CREATE PROCEDURE control.load_runs AS SELECT 1\n",
     "CREATE PROCEDURE control.load_runs AS SELECT 1\nGO"),
])
def test_each_provider_s_source_ends_as_its_script_needs(
    provider: str, source: str, expected: str,
) -> None:
    connector = _Connector(provider, source)

    assert DatabaseProcedure.read_ddl(object(), _IDENTITY, connector) == expected
    assert connector.asked == [("control", "load_runs", "integer", "procedure")]


def test_a_provider_without_procedures_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedDatabaseCapabilityError, match="duckdb"):
        DatabaseProcedure.read_ddl(object(), _IDENTITY, _Connector("duckdb", ""))


def test_sql_server_retrieves_the_stored_definition_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[dict] = []
    monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda _c, _sql, params: asked.append(
        params) or [{"definition": "CREATE PROCEDURE control.load_runs AS SELECT 1"}])

    source = sqlserver_utils.get_routine_definition(
        object(), "", "control", "load_runs", "", "procedure",
    )

    assert source == "CREATE PROCEDURE control.load_runs AS SELECT 1"
    assert asked == [{"qualified": "[control].[load_runs]"}]


def test_sql_server_refuses_a_routine_that_is_not_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda *_a: [{"definition": None}])

    with pytest.raises(DatabaseError, match="procedure not found: control.gone"):
        sqlserver_utils.get_routine_definition(object(), "", "control", "gone", "", "procedure")


class _Cursor:
    """A psycopg cursor answering the export listing with one procedure."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql_text: str, *_a: Any) -> None:
        self.statements.append(sql_text)

    def fetchall(self) -> list[tuple]:
        # The object query answers one procedure; the table, index and
        # constraint listings it also runs answer nothing.
        if "WITH base AS" in self.statements[-1]:
            return [("procedures", "control", "load_runs", 4242, "integer", None)]
        return []

    def fetchone(self) -> tuple:
        return ("rey",)

    def close(self) -> None:
        pass


def test_the_postgres_listing_carries_each_routine_s_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So the export can address an overload by Rey's identity, not by its oid."""
    cursor = _Cursor()
    monkeypatch.setattr(postgres_utils, "_postgres_dependencies", lambda *_a, **_k: [])
    conn = types.SimpleNamespace(cursor=lambda: cursor)

    (listed,) = postgres_utils.list_database_objects(conn, database="rey")

    assert (listed["object_type"], listed["name"], listed["signature"]) == (
        "procedures", "load_runs", "integer",
    )
    assert any("pg_get_function_identity_arguments" in sql for sql in cursor.statements)


def test_the_ddl_export_asks_the_procedure_by_identity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export lists and orders; the procedure writes itself, found by name and signature."""
    asked: list[tuple] = []
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: [
            {"database": "rey", "object_type": "procedures", "schema": "control",
             "name": "load_runs", "signature": "integer", "object_oid": 4242,
             "dependencies": []},
        ],
        get_routine_definition=lambda _c, _catalog, schema, name, signature, kind: asked.append(
            (schema, name, signature, kind)) or "CREATE PROCEDURE control.load_runs(integer) ...;",
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)
    conn = types.SimpleNamespace(provider="postgres")

    DatabaseDdlExport.export(conn, output_root=str(tmp_path))

    assert asked == [("control", "load_runs", "integer", "procedure")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "load_runs" in p.name]
    assert written.read_text(encoding="utf-8") == (
        "CREATE PROCEDURE control.load_runs(integer) ...;\n"
    )
