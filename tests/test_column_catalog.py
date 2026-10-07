"""One column-fact source for every provider (backlog 724).

table_column_catalog is the single primitive a column's facts come from:
DatabaseSchema.read builds its DatabaseColumns from it, through
DBAdapter.table_column_catalog, and DatabaseTable writes its DDL from the same
facts. MySQL and DuckDB now answer it too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from rey_lib.db import mysql_utils
from rey_lib.db.database_objects import DatabaseSchema
from rey_lib.db.db_adapter import DBAdapter


def test_mysql_reads_information_schema_in_column_order(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, dict]] = []
    monkeypatch.setattr(mysql_utils, "fetch_rows", lambda _c, sql, params: asked.append(
        (sql, dict(params))) or [
            {"name": "id", "type": "int unsigned", "default_value": None,
             "is_nullable": "NO", "extra": "auto_increment"},
            {"name": "status", "type": "varchar(12)", "default_value": "pending",
             "is_nullable": "YES", "extra": ""},
            {"name": "note", "type": "varchar(40)", "default_value": "it's",
             "is_nullable": "YES", "extra": ""},
            {"name": "made", "type": "datetime", "default_value": "CURRENT_TIMESTAMP",
             "is_nullable": "YES", "extra": "DEFAULT_GENERATED"},
        ])

    columns = mysql_utils.table_column_catalog(object(), "rey", "trade")

    ((sql_text, params),) = asked
    assert "information_schema.columns" in sql_text and "ORDER BY ordinal_position" in sql_text
    assert params == {"schema": "rey", "table": "trade"}
    assert columns == [
        {"name": "id", "type": "int unsigned", "default": None, "nullable": False},
        # A literal default comes back unquoted from MySQL; it is returned as SQL.
        {"name": "status", "type": "varchar(12)", "default": "'pending'", "nullable": True},
        {"name": "note", "type": "varchar(40)", "default": "'it''s'", "nullable": True},
        # An expression default is SQL already.
        {"name": "made", "type": "datetime", "default": "CURRENT_TIMESTAMP", "nullable": True},
    ]


def test_duckdb_reads_its_catalog_for_tables_and_views_through_the_adapter() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE trade (id INTEGER NOT NULL, status VARCHAR DEFAULT 'pending',"
                 " amount DECIMAL(12,2))")
    conn.execute("CREATE VIEW trade_vw AS SELECT id, status FROM trade")
    adapter = DBAdapter()

    assert adapter.table_column_catalog(conn, "main", "trade") == [
        {"name": "id", "type": "INTEGER", "default": None, "nullable": False},
        {"name": "status", "type": "VARCHAR", "default": "'pending'", "nullable": True},
        {"name": "amount", "type": "DECIMAL(12,2)", "default": None, "nullable": True},
    ]
    assert [column["name"] for column in adapter.table_column_catalog(
        conn, "main", "trade_vw")] == ["id", "status"]
    assert adapter.table_column_catalog(conn, "main", "absent") == []


def test_the_schema_read_takes_every_column_from_the_catalog() -> None:
    """Tables and views alike, and never from the inspector's get_columns."""
    asked: list[str] = []

    def catalog(_conn: Any, _schema: str, name: str, **_k: Any) -> list[dict]:
        asked.append(name)
        return [{"name": f"{name}_id", "type": "bigint", "default": None, "nullable": False}]

    adapter = SimpleNamespace(
        provider=lambda _conn: "postgres",
        quote_identifier=lambda _conn, name: f'"{name}"',
        get_schema_metadata=lambda _conn, _schema: {
            "tables": [{"name": "run_log", "columns": [{"name": "IGNORED", "type": "X"}]}],
            "views": [{"name": "run_vw", "definition": "SELECT 1"}],
        },
        table_column_catalog=catalog,
        view_native_definition=lambda *_a: " SELECT 1",
        list_procedures=lambda *_a: [], list_functions=lambda *_a: [],
    )

    schema = DatabaseSchema.read(object(), "c", "rey", "control", adapter)

    assert [c.name for c in schema.tables[0].columns] == ["run_log_id"]
    assert [c.name for c in schema.views[0].columns] == ["run_vw_id"]
    assert "run_log" in asked and "run_vw" in asked
    assert not hasattr(adapter, "get_columns")
