"""DatabaseColumn owns its column definition (backlog 721).

The fragment a table lists for a column -- quoted name, physical type, any
default, nullability -- is composed by the column from its own facts, for every
provider. It is a fragment, never a statement. DuckDB takes no explicit NULL.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseColumn

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
    "duckdb": duckdb_utils.quote_identifier,
}


def _connector(provider: str) -> Any:
    return SimpleNamespace(
        provider=lambda _conn: provider,
        quote_identifier=lambda _conn, name: _QUOTE[provider](name),
    )


def _column(**facts: Any) -> DatabaseColumn:
    base = {"name": "run_id", "type": "bigint", "nullable": False, "default": None, "ordinal": 1}
    return DatabaseColumn(**{**base, **facts})


@pytest.mark.parametrize(("provider", "expected"), [
    ("postgres", '"run_id" bigint NOT NULL'),
    ("mysql", "`run_id` bigint NOT NULL"),
    ("sqlserver", "[run_id] bigint NOT NULL"),
    ("duckdb", '"run_id" bigint NOT NULL'),
])
def test_a_not_null_column_in_each_provider_s_quoting(provider: str, expected: str) -> None:
    assert _column().definition(object(), _connector(provider)) == expected


def test_a_default_is_written_before_nullability() -> None:
    column = _column(name="status", type="text", nullable=True, default="'pending'::text")

    assert column.definition(object(), _connector("postgres")) == (
        '"status" text DEFAULT \'pending\'::text NULL'
    )


def test_duckdb_writes_no_explicit_null() -> None:
    column = _column(nullable=True)

    assert column.definition(object(), _connector("duckdb")) == '"run_id" bigint'
    assert column.definition(object(), _connector("postgres")) == '"run_id" bigint NULL'


def test_the_fragment_is_never_a_statement() -> None:
    fragment = _column().definition(object(), _connector("postgres"))

    assert not fragment.upper().startswith(("CREATE", "ALTER"))
    assert not fragment.endswith(";")


def test_a_mysql_schema_read_now_carries_column_definitions() -> None:
    """Was Postgres only, through a provider-owned get_column_ddl that is gone."""
    from rey_lib.db.database_objects import DatabaseSchema

    adapter = SimpleNamespace(
        provider=lambda _conn: "mysql",
        quote_identifier=lambda _conn, name: mysql_utils.quote_identifier(name),
        get_schema_metadata=lambda _conn, _schema: {"tables": [{
            "name": "trade",
            "columns": [{"name": "symbol", "type": "VARCHAR(12)", "nullable": False}],
        }], "views": []},
        table_native_definition=lambda *_a: "CREATE TABLE `trade` (`symbol` varchar(12))",
        table_column_catalog=lambda _conn, _schema, _table, **_k: [
            {"name": "symbol", "type": "varchar(12)", "default": None, "nullable": False},
        ],
        list_procedures=lambda *_a: [], list_functions=lambda *_a: [],
    )

    schema = DatabaseSchema.read(object(), "c", "rey", "rey", adapter)

    (column,) = schema.tables[0].columns
    assert column.ddl == "`symbol` varchar(12) NOT NULL"


def test_no_adapter_or_provider_writes_column_ddl_any_more() -> None:
    from rey_lib.db.db_adapter import DBAdapter

    assert not hasattr(DBAdapter, "get_column_ddl")
    assert not hasattr(postgres_utils, "get_column_ddl")
