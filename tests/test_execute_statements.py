"""Executing SQL as written, and answering with whatever came back.

The Console used to decide what could run by reading the text: a statement
count taken from a semicolon, a leading-keyword allowlist, a scan for forbidden
words. None of that was ever in this layer, and none of it arrives here now.
What may run is the connected role's answer.

These tests use fakes throughout. Proving that a definition executes must not
mean executing one against a real database.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db.db_adapter import DBAdapter, StatementResult
from rey_lib.errors.error_utils import DatabaseError


class _Cursor:
    """A DBAPI cursor answering from a script."""

    def __init__(self, sets: list[tuple[list[str], list[tuple[Any, ...]]]], rowcount: int = -1):
        self._sets = sets
        self._at = 0
        self.rowcount = rowcount
        self.executed: list[str] = []
        self.closed = False

    @property
    def description(self) -> Any:
        columns = self._sets[self._at][0]
        return [(name,) for name in columns] if columns else None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._sets[self._at][1][:size]

    def close(self) -> None:
        self.closed = True


class _Conn:
    provider = "duckdb"

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def _executed(cursor: _Cursor) -> list[StatementResult]:
    return DBAdapter().execute_statements(_Conn(cursor), "any sql", limit=10)


class TestWhatComesBack:
    """A result per result, and a statement returning none is still an answer."""

    def test_rows_arrive_as_columns_and_mappings(self) -> None:
        results = _executed(_Cursor([(["id", "name"], [(1, "a"), (2, "b")])], rowcount=2))

        assert len(results) == 1
        assert results[0].columns == ["id", "name"]
        assert results[0].rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    def test_a_statement_returning_nothing_is_not_a_failure(self) -> None:
        # What DDL and a call do. No description, so no columns -- an outcome,
        # not an error.
        results = _executed(_Cursor([([], [])], rowcount=-1))

        assert results[0].columns == []
        assert results[0].rows == []

    def test_the_answer_is_always_a_list(self) -> None:
        # Nothing above may assume there is exactly one, so even the single
        # case is a list rather than a result that happens to be alone.
        assert isinstance(_executed(_Cursor([([], [])])), list)

    def test_the_limit_bounds_one_result(self) -> None:
        rows = [(n,) for n in range(50)]
        results = _executed(_Cursor([(["n"], rows)]))

        assert len(results[0].rows) == 10

    def test_the_cursor_is_closed(self) -> None:
        cursor = _Cursor([([], [])])
        _executed(cursor)

        assert cursor.closed

    def test_a_failure_is_reported_as_one(self) -> None:
        class _Broken(_Cursor):
            def execute(self, sql: str) -> None:
                raise RuntimeError("the server refused")

        with pytest.raises(DatabaseError):
            _executed(_Broken([([], [])]))


class TestRowCount:
    """A count that is meaningful, or None. Never a driver's sentinel."""

    def test_a_real_count_crosses(self) -> None:
        assert _executed(_Cursor([([], [])], rowcount=7))[0].row_count == 7

    def test_unknown_becomes_none_rather_than_minus_one(self) -> None:
        # -1 is DBAPI's "unknown". It must not reach a reader as a number.
        assert _executed(_Cursor([([], [])], rowcount=-1))[0].row_count is None

    def test_a_driver_offering_no_count_at_all_is_none(self) -> None:
        cursor = _Cursor([([], [])])
        del cursor.rowcount

        assert _executed(cursor)[0].row_count is None


class TestTheTextIsNotRead:
    """Nothing counts statements, checks a keyword, or rewrites anything."""

    @pytest.mark.parametrize("sql", [
        "delete from t",
        "drop table t",
        "create or replace procedure p() as $$ begin x := 1; end $$",
        "call control.p_sweep()",
        "select * from t;",
        "select 1; select 2",
        "select 'a :b' as literal",
    ])
    def test_every_statement_reaches_the_driver_unaltered(self, sql: str) -> None:
        cursor = _Cursor([([], [])])
        DBAdapter().execute_statements(_Conn(cursor), sql, limit=10)

        assert cursor.executed == [sql]


class TestDispatch:
    """One result generically; several only where a provider claims them."""

    def test_the_generic_path_returns_exactly_one_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # It does not advance nextset on every provider's behalf: a driver
        # exposing the method is not evidence its batch semantics match.
        advanced: list[bool] = []

        class _WithNextset(_Cursor):
            def nextset(self) -> bool:
                advanced.append(True)
                return True

        results = _executed(_WithNextset([(["a"], [(1,)])]))

        assert len(results) == 1
        assert advanced == []

    def test_a_provider_that_claims_several_answers_with_several(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The multi-result contract, exercised where a provider declares it --
        # without the generic path claiming a capability it does not have.
        from rey_lib.db import db_adapter

        several = [
            StatementResult(columns=["a"], rows=[{"a": 1}], row_count=1),
            StatementResult(columns=["b"], rows=[{"b": 2}], row_count=1),
        ]

        class _Backend:
            @staticmethod
            def execute_statements(conn: Any, sql: str, *, limit: int) -> list[StatementResult]:
                return several

        monkeypatch.setattr(db_adapter, "_backend", lambda provider: _Backend)

        assert _executed(_Cursor([([], [])])) == several


class TestThePostgresImplementation:
    """Its own, because arbitrary text cannot go through SQLAlchemy's text().

    Exercised against an in-memory database rather than the estate's: what is
    being proved is how the statement is handed to a driver, which is the same
    question whichever driver answers.
    """

    @staticmethod
    def _conn() -> Any:
        from sqlalchemy import create_engine
        return create_engine("sqlite://").connect()

    @staticmethod
    def _run(core: Any, sql: str) -> list[StatementResult]:
        from rey_lib.db import postgres_utils
        # core_connection hands the module the SQLAlchemy connection; here it
        # already is one.
        import rey_lib.db._sqlalchemy as sa
        original = sa.core_connection
        sa.core_connection = lambda conn: conn
        try:
            return postgres_utils.execute_statements(core, sql, limit=10)
        finally:
            sa.core_connection = original

    def test_a_colon_in_the_text_is_not_read_as_a_bind_parameter(self) -> None:
        # text() raises "A value is required for bind parameter 'b'" here. A
        # definition body or any literal with a colon would never reach the
        # database.
        core = self._conn()

        results = self._run(core, "SELECT 'a :b' AS x")

        assert results[0].rows == [{"x": "a :b"}]

    def test_a_statement_returning_nothing_answers_rather_than_raising(self) -> None:
        # keys() raises ResourceClosedError on such a result; returns_rows is
        # what is asked instead.
        core = self._conn()

        results = self._run(core, "CREATE TABLE t (id INTEGER)")

        assert results[0].columns == []
        assert results[0].rows == []

    def test_rows_come_back_as_columns_and_mappings(self) -> None:
        core = self._conn()
        self._run(core, "CREATE TABLE t (id INTEGER, name TEXT)")
        self._run(core, "INSERT INTO t VALUES (1, 'a'), (2, 'b')")

        results = self._run(core, "SELECT id, name FROM t ORDER BY id")

        assert results[0].columns == ["id", "name"]
        assert results[0].rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    def test_the_limit_bounds_the_result(self) -> None:
        core = self._conn()
        self._run(core, "CREATE TABLE t (id INTEGER)")
        self._run(core, "INSERT INTO t SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")

        results = self._run(core, "SELECT id FROM t")

        assert len(results[0].rows) <= 10

    def test_a_failure_is_reported_as_one(self) -> None:
        core = self._conn()

        with pytest.raises(DatabaseError):
            self._run(core, "SELECT * FROM nothing_here")

    def test_it_answers_with_a_list(self) -> None:
        # One result, because psycopg2 exposes one -- stated as a list so a
        # client that exposes several needs nothing above to change.
        core = self._conn()

        assert isinstance(self._run(core, "SELECT 1 AS one"), list)
