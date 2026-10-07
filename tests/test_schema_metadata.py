"""The schema's structure facts are a provider primitive (backlog 725).

DatabaseSchema.read -> DBAdapter.get_schema_metadata -> provider.schema_metadata.
Every provider answers one fact shape: tables by name with their keys,
indexes and constraints, and views by name. No column facts (724) and no view
definitions (DatabaseView.read_ddl owns view DDL). Providers construct no
database objects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from rey_lib.db import sqlserver_utils
from rey_lib.db.database_objects import DatabaseSchema
from rey_lib.db.db_adapter import DBAdapter

_TABLE_KEYS = {
    "name", "primary_key", "foreign_keys", "indexes", "unique_constraints", "check_constraints",
}


def test_duckdb_answers_the_structure_from_its_own_catalog() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, u VARCHAR UNIQUE, q INTEGER CHECK (q > 0),"
                 " pid INTEGER REFERENCES p(id))")
    conn.execute("CREATE INDEX t_q ON t(q, id)")
    conn.execute("CREATE VIEW v AS SELECT id FROM t")

    metadata = DBAdapter().get_schema_metadata(conn, "main")

    assert set(metadata) == {"tables", "views"}
    assert [table["name"] for table in metadata["tables"]] == ["p", "t"]
    assert metadata["views"] == [{"name": "v"}]
    t = metadata["tables"][1]
    assert set(t) == _TABLE_KEYS
    assert t["primary_key"] == {"name": "t_id_pkey", "constrained_columns": ["id"]}
    assert t["foreign_keys"] == [{
        "name": "t_pid_id_fkey", "constrained_columns": ["pid"], "referred_schema": "main",
        "referred_table": "p", "referred_columns": ["id"],
    }]
    assert t["unique_constraints"] == [{"name": "t_u_key", "column_names": ["u"]}]
    assert t["check_constraints"] == [{"name": "t_q_check"}]
    assert t["indexes"] == [{"name": "t_q", "column_names": ["q", "id"], "unique": False}]


def test_a_duckdb_schema_read_now_builds_its_objects() -> None:
    """DatabaseSchema.read used to refuse DuckDB at its first call."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE trade (id INTEGER PRIMARY KEY, symbol VARCHAR NOT NULL)")
    conn.execute("CREATE VIEW trade_vw AS SELECT symbol FROM trade")

    schema = DatabaseSchema.read(conn, "c", "memory", "main", DBAdapter())

    (table,) = schema.tables
    assert [column.name for column in table.columns] == ["id", "symbol"]
    assert table.primary_key is not None and table.primary_key.columns == ("id",)
    (view,) = schema.views
    assert view.ddl == "CREATE VIEW trade_vw AS SELECT symbol FROM trade;"


def test_sql_server_groups_each_key_s_columns_in_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([
        [{"name": "trade"}],                                                    # tables
        [{"table_name": "trade", "key_name": "pk_trade", "key_type": "PK", "column_name": "id"},
         {"table_name": "trade", "key_name": "uq_sym", "key_type": "UQ", "column_name": "symbol"},
         {"table_name": "trade", "key_name": "uq_sym", "key_type": "UQ", "column_name": "day"}],
        [{"table_name": "trade", "fk_name": "fk_book", "column_name": "book_id",
          "referred_schema": "ref", "referred_table": "book", "referred_column": "id"}],
        [{"table_name": "trade", "index_name": "ix_day", "is_unique": False, "column_name": "day"}],
        [{"table_name": "trade", "check_name": "ck_qty"}],
        [{"name": "trade_vw"}],                                                 # views
    ])
    monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda *_a: next(answers))

    metadata = sqlserver_utils.schema_metadata(object(), "dbo")

    (trade,) = metadata["tables"]
    assert set(trade) == _TABLE_KEYS
    assert trade["primary_key"] == {"name": "pk_trade", "constrained_columns": ["id"]}
    assert trade["unique_constraints"] == [{"name": "uq_sym", "column_names": ["symbol", "day"]}]
    assert trade["foreign_keys"][0]["referred_schema"] == "ref"
    assert trade["indexes"] == [{"name": "ix_day", "column_names": ["day"], "unique": False}]
    assert trade["check_constraints"] == [{"name": "ck_qty"}]
    assert metadata["views"] == [{"name": "trade_vw"}]


def test_no_second_view_definition_stands_in_for_the_view_s_ddl() -> None:
    """When DatabaseView.read_ddl yields nothing, the view has no DDL -- the
    structure read carries no definition to fall back on."""
    adapter = SimpleNamespace(
        provider=lambda _conn: "postgres",
        quote_identifier=lambda _conn, name: f'"{name}"',
        get_schema_metadata=lambda *_a: {"tables": [], "views": [
            {"name": "run_vw", "definition": "SELECT 'a stale second definition'"},
        ]},
        table_column_catalog=lambda *_a, **_k: [],
        view_native_definition=lambda *_a: (_ for _ in ()).throw(RuntimeError("down")),
        list_procedures=lambda *_a: [], list_functions=lambda *_a: [],
    )

    (view,) = DatabaseSchema.read(object(), "c", "rey", "control", adapter).views

    assert view.ddl == ""
