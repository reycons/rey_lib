"""DatabaseIndex owns its DDL (backlog 695).

An index's DDL used to be a branch in each provider's ``get_object_ddl``; it
is ``DatabaseIndex.read_ddl`` now. An index is addressed by the table it
belongs to and its own name, never by a provider handle such as PostgreSQL's
oid. PostgreSQL supplies its own definition; MySQL and SQL Server state the
key columns and the index composes the statement. A primary key's backing
index is the primary key -- DatabasePrimaryKey's -- and is refused here, and
the export listings no longer offer one as an index.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseIndex, DatabaseObjectIdentity
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
}

_TABLE = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="run_log")


class _Connector:
    """The index primitives, as one provider, recording what was asked."""

    def __init__(
        self, provider: str, catalog: dict, native: str = "",
    ) -> None:
        self._provider = provider
        self.catalog = catalog
        self.native = native
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def index_column_catalog(self, _conn: Any, schema: str, table: str, index: str) -> dict:
        self.asked.append(("catalog", schema, table, index))
        return self.catalog

    def index_native_definition(self, _conn: Any, schema: str, table: str, index: str) -> str:
        self.asked.append(("native", schema, table, index))
        return self.native


_ORDINARY = {"unique": False, "primary": False, "columns": ["status", "run_id"]}
_UNIQUE = {"unique": True, "primary": False, "columns": ["batch_id"]}


def test_postgres_writes_the_definition_the_server_supplies() -> None:
    connector = _Connector(
        "postgres", _ORDINARY,
        "CREATE INDEX run_log_status_idx ON control.run_log USING btree (status, run_id)",
    )

    ddl = DatabaseIndex.read_ddl(object(), _TABLE, "run_log_status_idx", connector)

    assert ddl == (
        "CREATE INDEX run_log_status_idx ON control.run_log USING btree (status, run_id);"
    )
    assert ("native", "control", "run_log", "run_log_status_idx") in connector.asked


def test_mysql_composes_from_its_catalog() -> None:
    connector = _Connector("mysql", _UNIQUE)

    ddl = DatabaseIndex.read_ddl(object(), _TABLE, "run_log_batch_uq", connector)

    assert ddl == "CREATE UNIQUE INDEX `run_log_batch_uq` ON `control`.`run_log` (`batch_id`);"
    assert connector.asked == [("catalog", "control", "run_log", "run_log_batch_uq")]


def test_sql_server_composes_and_ends_the_batch() -> None:
    connector = _Connector("sqlserver", _ORDINARY)

    ddl = DatabaseIndex.read_ddl(object(), _TABLE, "run_log_status_idx", connector)

    assert ddl == (
        "CREATE INDEX [run_log_status_idx] ON [control].[run_log] ([status], [run_id]);\nGO"
    )


@pytest.mark.parametrize("provider", ["postgres", "mysql", "sqlserver"])
def test_a_primary_key_s_backing_index_is_refused(provider: str) -> None:
    """Its DDL is DatabasePrimaryKey's, not the index's."""
    connector = _Connector(provider, {"unique": True, "primary": True, "columns": ["run_id"]})

    with pytest.raises(DatabaseError, match="DatabasePrimaryKey"):
        DatabaseIndex.read_ddl(object(), _TABLE, "run_log_pkey", connector)


def test_an_index_that_is_not_there_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="control.run_log.gone"):
        DatabaseIndex.read_ddl(object(), _TABLE, "gone", _Connector("mysql", {}))


def test_postgres_reads_the_catalog_by_table_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict] = []
    monkeypatch.setattr(postgres_utils, "fetch_rows", lambda _c, _sql, params: asked.append(
        params) or [
            {"is_unique": False, "is_primary": False, "column_name": "status"},
            {"is_unique": False, "is_primary": False, "column_name": None},
        ])

    catalog = postgres_utils.index_column_catalog(object(), "control", "run_log", "ix")

    assert catalog == {"unique": False, "primary": False, "columns": ["status"]}
    assert asked == [{"schema": "control", "table": "run_log", "index": "ix"}]


def test_mysql_names_its_primary_key_index_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mysql_utils, "fetch_rows", lambda *_a: [
        {"non_unique": 0, "column_name": "run_id"},
    ])

    assert mysql_utils.index_column_catalog(object(), "control", "run_log", "PRIMARY") == {
        "unique": True, "primary": True, "columns": ["run_id"],
    }


class _Cursor:
    """A psycopg cursor answering the export listing with one table and one index."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql_text: str, *_a: Any) -> None:
        self.statements.append(sql_text)

    def fetchall(self) -> list[tuple]:
        last = self.statements[-1]
        if "relkind IN ('r', 'p')" in last:
            return [("control", "run_log")]
        if "FROM pg_indexes" in last:
            return [("control", "run_log_status_idx", 4242)]
        return []

    def fetchone(self) -> tuple:
        return ("rey",)

    def close(self) -> None:
        pass


def test_the_postgres_listing_names_each_index_s_table_and_skips_the_primary_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(postgres_utils, "_postgres_dependencies", lambda *_a, **_k: [])
    conn = types.SimpleNamespace(cursor=lambda: cursor)

    listed = postgres_utils.list_database_objects(conn, database="rey")

    (index,) = [entry for entry in listed if entry["object_type"] == "indexes"]
    assert (index["name"], index["table"]) == ("run_log_status_idx", "run_log")
    (asked,) = [sql for sql in cursor.statements if "FROM pg_indexes" in sql]
    assert "NOT x.indisprimary" in asked


def test_the_ddl_export_asks_the_index_by_its_table(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export lists and orders; the index writes itself, found by table and name."""
    asked: list[tuple] = []
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: [
            {"database": "rey", "object_type": "index", "schema": "control",
             "name": "run_log_status_idx", "table": "run_log", "dependencies": []},
        ],
        quote_identifier=sqlserver_utils.quote_identifier,
        index_column_catalog=lambda _c, schema, table, index: asked.append(
            (schema, table, index)) or _ORDINARY,
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)
    conn = types.SimpleNamespace(provider="sqlserver")

    DBAdapter().export_database_ddl(conn, output_root=str(tmp_path))

    assert asked == [("control", "run_log", "run_log_status_idx")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "run_log_status_idx" in p.name]
    assert written.read_text(encoding="utf-8") == (
        "CREATE INDEX [run_log_status_idx] ON [control].[run_log] ([status], [run_id]);\nGO\n"
    )
