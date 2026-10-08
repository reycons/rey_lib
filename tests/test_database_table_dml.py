"""DatabaseTable's one-row and targeted DML (backlog database.table_dml_surface).

``insert``, ``update`` and ``delete`` sit beside ``bulk_insert`` and
``delete_all_rows``: DatabaseTable owns the operation, DBAdapter dispatches,
and each provider owns its statement, binding and how it returns the key and
the rows touched.

DuckDB runs for real, in memory, through all three layers. PostgreSQL and MySQL
are asserted on the SQLAlchemy statement they build; SQL Server on the text and
parameters it hands its cursor. No server is opened for those.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from rey_lib.db import mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.errors.error_utils import DatabaseError

_SETUP = Path(__file__).parent / "sql" / "database_table_dml.sql"


def _identity(name: str, schema: str = "main") -> DatabaseObjectIdentity:
    return DatabaseObjectIdentity(connection="test", catalog="", schema=schema, name=name)


@pytest.fixture
def duck() -> Any:
    """An in-memory DuckDB holding the DML test tables."""
    conn = duckdb.connect()
    for statement in _SETUP.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            conn.execute(statement)
    yield conn
    conn.close()


def _rows(conn: Any, table: str) -> list[tuple]:
    return sorted(conn.table(f"main.{table}").fetchall(), key=lambda row: row[0])


class TestThroughAllThreeLayers:
    """DatabaseTable -> DBAdapter -> duckdb_utils, against a real database."""

    def test_insert_returns_the_generated_key(self, duck: Any) -> None:
        first = DatabaseTable.insert(duck, _identity("event"), {"kind": "a", "note": "one"})
        second = DatabaseTable.insert(duck, _identity("event"), {"kind": "b", "note": "two"})

        assert (first, second) == (1, 2)
        assert [row[:3] for row in _rows(duck, "event")] == [(1, "a", "one"), (2, "b", "two")]

    def test_insert_returns_a_supplied_key(self, duck: Any) -> None:
        assert DatabaseTable.insert(duck, _identity("event"), {"event_id": 40, "kind": "a"}) == 40

    def test_a_mapping_is_bound_as_json(self, duck: Any) -> None:
        key = DatabaseTable.insert(duck, _identity("event"), {"payload": {"x": [1, 2]}})

        stored = dict((row[0], row[3]) for row in _rows(duck, "event"))[key]
        assert json.loads(stored) == {"x": [1, 2]}

    def test_update_changes_only_matching_rows_and_counts_them(self, duck: Any) -> None:
        for kind in ("a", "a", "b"):
            DatabaseTable.insert(duck, _identity("event"), {"kind": kind})

        changed = DatabaseTable.update(duck, _identity("event"), {"note": "seen"}, {"kind": "a"})

        assert changed == 2
        assert [(row[1], row[2]) for row in _rows(duck, "event")] == [
            ("a", "seen"), ("a", "seen"), ("b", None),
        ]

    def test_criteria_are_all_required_and_none_matches_null(self, duck: Any) -> None:
        DatabaseTable.insert(duck, _identity("event"), {"kind": "a", "note": None})
        DatabaseTable.insert(duck, _identity("event"), {"kind": "a", "note": "x"})

        assert DatabaseTable.update(
            duck, _identity("event"), {"note": "was null"}, {"kind": "a", "note": None},
        ) == 1
        assert DatabaseTable.delete(duck, _identity("event"), {"kind": "a", "note": "x"}) == 1
        assert [(row[1], row[2]) for row in _rows(duck, "event")] == [("a", "was null")]

    def test_delete_counts_the_rows_it_removed(self, duck: Any) -> None:
        for kind in ("a", "b", "b"):
            DatabaseTable.insert(duck, _identity("event"), {"kind": kind})

        assert DatabaseTable.delete(duck, _identity("event"), {"kind": "b"}) == 2
        assert DatabaseTable.delete(duck, _identity("event"), {"kind": "z"}) == 0
        assert [row[1] for row in _rows(duck, "event")] == ["a"]

    @pytest.mark.parametrize("table, count", [("no_key", 0), ("two_keys", 2)])
    def test_insert_needs_exactly_one_primary_key_column(
        self, duck: Any, table: str, count: int,
    ) -> None:
        with pytest.raises(DatabaseError, match=f"has {count} primary-key column"):
            DatabaseTable.insert(duck, _identity(table), {"a": 1})


class TestTheTableRefusesWhatItNeverDoes:
    """Semantic refusals, before any provider is asked."""

    @pytest.mark.parametrize("call", [
        lambda: DatabaseTable.insert(None, _identity("event"), {}, adapter=object()),
        lambda: DatabaseTable.update(None, _identity("event"), {}, {"kind": "a"}, adapter=object()),
        lambda: DatabaseTable.update(None, _identity("event"), {"kind": "a"}, {}, adapter=object()),
        lambda: DatabaseTable.delete(None, _identity("event"), {}, adapter=object()),
    ])
    def test_empty_values_or_criteria_are_refused(self, call: Any) -> None:
        with pytest.raises(DatabaseError):
            call()


class _Adapter:
    """Records what DatabaseTable hands the adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def insert(self, *args: Any) -> int:
        self.calls.append(("insert", *args))
        return 7

    def update(self, *args: Any) -> int:
        self.calls.append(("update", *args))
        return 3

    def delete(self, *args: Any) -> int:
        self.calls.append(("delete", *args))
        return 2


class TestTheTableOnlyDispatches:
    """DatabaseTable names no provider: schema and table, then the adapter."""

    def test_each_operation_is_one_adapter_call(self) -> None:
        adapter = _Adapter()
        identity = DatabaseObjectIdentity(connection="c", catalog="db", schema="s", name="t")

        assert DatabaseTable.insert("conn", identity, {"a": 1}, adapter=adapter) == 7
        assert DatabaseTable.update("conn", identity, {"a": 1}, {"b": 2}, adapter=adapter) == 3
        assert DatabaseTable.delete("conn", identity, {"b": 2}, adapter=adapter) == 2
        assert adapter.calls == [
            ("insert", "conn", "db.s", "t", {"a": 1}),
            ("update", "conn", "db.s", "t", {"a": 1}, {"b": 2}),
            ("delete", "conn", "db.s", "t", {"b": 2}),
        ]


class _Result:
    def __init__(self, scalar: Any = None, rowcount: int = 0, lastrowid: Any = None) -> None:
        self._scalar, self.rowcount, self.lastrowid = scalar, rowcount, lastrowid

    def scalar_one(self) -> Any:
        return self._scalar


class _CoreConnection:
    """A SQLAlchemy connection that records the statement instead of executing it."""

    def __init__(self, result: _Result) -> None:
        self.result = result
        self.statements: list[Any] = []

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        self.statements.append(statement)
        return self.result


def _compiled(statement: Any, dialect: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=dialect)
    return str(compiled), dict(compiled.params)


class TestPostgreSQL:
    """SQLAlchemy constructs; the key comes back through RETURNING."""

    @pytest.fixture
    def core(self, monkeypatch: pytest.MonkeyPatch) -> _CoreConnection:
        recorder = _CoreConnection(_Result(scalar=11, rowcount=4))
        monkeypatch.setattr("rey_lib.db._sqlalchemy.core_connection", lambda _conn: recorder)
        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.inspect_primary_key", lambda _c, _s, _t: ["run_log_id"],
        )
        return recorder

    def test_insert_binds_values_and_returns_the_key(self, core: _CoreConnection) -> None:
        from psycopg.types.json import Jsonb
        from sqlalchemy.dialects import postgresql

        key = postgres_utils.insert(
            None, "control", "run_log", {"message": "m", "info": {"logger": "x"}},
        )

        sql, params = _compiled(core.statements[0], postgresql.dialect())
        assert key == 11
        assert "RETURNING control.run_log.run_log_id" in sql
        assert params["message"] == "m"
        assert isinstance(params["info"], Jsonb)

    def test_update_and_delete_bind_criteria_and_count(self, core: _CoreConnection) -> None:
        from sqlalchemy.dialects import postgresql

        assert postgres_utils.update(None, "s", "t", {"a": 1}, {"b": 2, "c": None}) == 4
        assert postgres_utils.delete(None, "s", "t", {"b": 2}) == 4

        update_sql, update_params = _compiled(core.statements[0], postgresql.dialect())
        delete_sql, delete_params = _compiled(core.statements[1], postgresql.dialect())
        assert "s.t.c IS NULL" in update_sql
        assert sorted(update_params.values(), key=str) == [1, 2]
        assert list(delete_params.values()) == [2]
        assert "WHERE s.t.b =" in delete_sql

    def test_a_table_without_one_key_column_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.inspect_primary_key", lambda _c, _s, _t: ["a", "b"],
        )
        with pytest.raises(DatabaseError, match="has 2 primary-key column"):
            postgres_utils.insert(None, "s", "t", {"a": 1})


class TestMySQL:
    """SQLAlchemy constructs; no RETURNING, so the supplied key or lastrowid."""

    @pytest.fixture
    def core(self, monkeypatch: pytest.MonkeyPatch) -> _CoreConnection:
        recorder = _CoreConnection(_Result(rowcount=5, lastrowid=21))
        monkeypatch.setattr("rey_lib.db._sqlalchemy.core_connection", lambda _conn: recorder)
        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.inspect_primary_key", lambda _c, _s, _t: ["id"],
        )
        return recorder

    def test_insert_returns_the_generated_or_supplied_key(self, core: _CoreConnection) -> None:
        from sqlalchemy.dialects import mysql

        assert mysql_utils.insert(None, "s", "t", {"a": 1, "doc": {"k": 1}}) == 21
        assert mysql_utils.insert(None, "s", "t", {"id": 9, "a": 1}) == 9

        _sql, params = _compiled(core.statements[0], mysql.dialect())
        assert params == {"a": 1, "doc": json.dumps({"k": 1})}

    def test_update_and_delete_count(self, core: _CoreConnection) -> None:
        assert mysql_utils.update(None, "s", "t", {"a": 1}, {"b": 2}) == 5
        assert mysql_utils.delete(None, "s", "t", {"b": 2}) == 5


class _Cursor:
    def __init__(self, fetched: Any, rowcount: int) -> None:
        self.fetched, self.rowcount = fetched, rowcount
        self.executed: list[tuple[str, list[Any]]] = []
        self.closed = False

    def execute(self, sql: str, params: list[Any]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self.fetched

    def close(self) -> None:
        self.closed = True


class _OdbcConnection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class TestSQLServer:
    """Provider SQL text; the key comes back through OUTPUT INSERTED."""

    @pytest.fixture(autouse=True)
    def _one_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sqlserver_utils, "schema_metadata", lambda _c, _s: {"tables": [
            {"name": "t", "primary_key": {"constrained_columns": ["id"]}},
        ]})

    def test_insert_binds_values_and_reads_the_output_key(self) -> None:
        cursor = _Cursor(fetched=(33,), rowcount=1)

        key = sqlserver_utils.insert(_OdbcConnection(cursor), "s", "t", {"a": 1, "doc": {"k": 1}})

        sql, params = cursor.executed[0]
        assert key == 33
        assert "OUTPUT INSERTED.[id]" in sql
        assert params == [1, json.dumps({"k": 1})]
        assert cursor.closed

    def test_update_and_delete_bind_criteria_and_count(self) -> None:
        cursor = _Cursor(fetched=None, rowcount=6)
        conn = _OdbcConnection(cursor)

        assert sqlserver_utils.update(conn, "s", "t", {"a": 1}, {"b": 2, "c": None}) == 6
        assert sqlserver_utils.delete(conn, "s", "t", {"b": 2}) == 6

        update_sql, update_params = cursor.executed[0]
        assert "[c] IS NULL" in update_sql
        assert update_params == [1, 2]
        assert cursor.executed[1][1] == [2]

    def test_a_table_without_one_key_column_is_refused(self) -> None:
        with pytest.raises(DatabaseError, match="has 0 primary-key column"):
            sqlserver_utils.insert(_OdbcConnection(_Cursor(None, 0)), "s", "missing", {"a": 1})
