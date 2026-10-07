"""fetch_rows: one read with named parameters, bound the way each driver binds (backlog 690).

A caller writes ``:name`` whatever the provider; turning that into what the
driver takes is the provider's. ``::`` casts and words containing a colon are
left alone, and values are always bound, never written into the text.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest
from sqlalchemy import create_engine

from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db._sqlalchemy import ReyConnection
from rey_lib.errors.error_utils import DatabaseError


def test_duckdb_binds_named_parameters_by_name() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE trade (symbol VARCHAR, price DOUBLE)")
    conn.execute("INSERT INTO trade VALUES ('IBM', 1.5), ('MSFT', 2.5)")

    rows = duckdb_utils.fetch_rows(
        conn, "SELECT symbol FROM trade WHERE symbol = :symbol AND price > :floor",
        {"symbol": "IBM", "floor": 1},
    )

    assert rows == [{"symbol": "IBM"}]


@pytest.mark.parametrize("provider_module", [postgres_utils, mysql_utils])
def test_the_sqlalchemy_providers_bind_named_parameters(provider_module: Any) -> None:
    engine = create_engine("sqlite://")
    core = engine.connect()
    core.exec_driver_sql("CREATE TABLE trade (symbol TEXT)")
    core.exec_driver_sql("INSERT INTO trade VALUES ('IBM'), ('MSFT')")
    conn = ReyConnection("postgres", engine, core)
    try:
        rows = provider_module.fetch_rows(
            conn, "SELECT symbol FROM trade WHERE symbol = :symbol", {"symbol": "MSFT"},
        )
    finally:
        conn.close()

    assert rows == [{"symbol": "MSFT"}]


class _Cursor:
    """A pyodbc cursor that records what it was asked to run."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, list]] = []
        self.description = [("present",)]

    def execute(self, statement: str, values: list) -> None:
        self.ran.append((statement, list(values)))

    def fetchall(self) -> list[tuple]:
        return [(1,)]

    def close(self) -> None:
        pass


class _Pyodbc:
    def __init__(self) -> None:
        self.cursor_ = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_


def test_sql_server_turns_each_named_parameter_into_a_positional_marker() -> None:
    conn = _Pyodbc()

    rows = sqlserver_utils.fetch_rows(
        conn,
        "SELECT 1 AS present FROM [db].information_schema.tables "
        "WHERE table_schema = :schema AND table_name = :table AND x::int = :schema",
        {"schema": "staging", "table": "trade"},
    )

    ((statement, values),) = conn.cursor_.ran
    assert statement.endswith("table_schema = ? AND table_name = ? AND x::int = ?")
    assert values == ["staging", "trade", "staging"]
    assert rows == [{"present": 1}]


def test_sql_server_refuses_a_parameter_with_no_value() -> None:
    with pytest.raises(DatabaseError, match=":table"):
        sqlserver_utils.fetch_rows(_Pyodbc(), "SELECT :table", {})
