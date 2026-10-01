"""Emptying and dropping a destination: written once, spelled by the provider.

``DBAdapter.delete_all_rows`` and ``drop_table`` compose their statement
themselves and ask the provider for exactly two things -- how it quotes an
identifier and how its driver runs one statement. These tests hold both halves:
the statement each provider receives, and what each provider's primitive does
with it.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils
from rey_lib.db.db_adapter import DBAdapter


class _Provider:
    """A provider module with the real quoting and a recording execute."""

    def __init__(self, quote: Any, count: int = 7) -> None:
        self.quote_identifier = quote
        self.statements: list[str] = []
        self._count = count

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        self.statements.append(sql_text)
        return self._count


def _adapter_over(monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> DBAdapter:
    monkeypatch.setattr(DBAdapter, "_provider_for_conn", lambda _self, _conn: "postgres")
    monkeypatch.setattr(adapter_module, "_backend", lambda _name: provider)
    return DBAdapter()


def _sqlserver_quote(value: str) -> str:
    """SQL Server's quoting, read from its module without importing pyodbc."""
    pytest.importorskip("pyodbc")
    from rey_lib.db import sqlserver_utils

    return sqlserver_utils.quote_identifier(value)


class TestTheAdapterWritesTheStatement:
    """One statement, spelled by whichever provider answers."""

    @pytest.mark.parametrize("quote,qualified", [
        (postgres_utils.quote_identifier, '"testing"."test3"'),
        (duckdb_utils.quote_identifier, '"testing"."test3"'),
        (mysql_utils.quote_identifier, "`testing`.`test3`"),
    ])
    def test_delete_all_rows(
        self, monkeypatch: pytest.MonkeyPatch, quote: Any, qualified: str,
    ) -> None:
        provider = _Provider(quote, count=12)

        removed = _adapter_over(monkeypatch, provider).delete_all_rows(
            object(), "testing", "test3",
        )

        assert provider.statements == [f"DELETE FROM {qualified}"]
        assert removed == 12            # the provider's count, carried

    @pytest.mark.parametrize("quote,qualified", [
        (postgres_utils.quote_identifier, '"testing"."test3"'),
        (mysql_utils.quote_identifier, "`testing`.`test3`"),
    ])
    def test_drop_table_has_no_cascade(
        self, monkeypatch: pytest.MonkeyPatch, quote: Any, qualified: str,
    ) -> None:
        provider = _Provider(quote)

        _adapter_over(monkeypatch, provider).drop_table(object(), "testing", "test3")

        assert provider.statements == [f"DROP TABLE {qualified}"]

    def test_sqlserver_brackets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _Provider(_sqlserver_quote)

        _adapter_over(monkeypatch, provider).delete_all_rows(object(), "dbo", "trade")

        assert provider.statements == ["DELETE FROM [dbo].[trade]"]

    def test_an_embedded_quote_is_doubled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _Provider(postgres_utils.quote_identifier)

        _adapter_over(monkeypatch, provider).delete_all_rows(object(), "testing", 'say "hi"')

        assert provider.statements == ['DELETE FROM "testing"."say ""hi"""']

    def test_a_qualified_schema_is_quoted_part_by_part(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = _Provider(postgres_utils.quote_identifier)

        _adapter_over(monkeypatch, provider).delete_all_rows(object(), "warehouse.main", "t")

        assert provider.statements == ['DELETE FROM "warehouse"."main"."t"']


class TestPostgresRunsOneStatement:
    """Postgres's half: the statement as given, its count, no commit."""

    def test_the_statement_runs_as_given_and_its_count_is_returned(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran: list[str] = []

        class _Result:
            rowcount = 42

        class _Core:
            def exec_driver_sql(self, sql_text: str) -> _Result:
                ran.append(sql_text)
                return _Result()

            def commit(self) -> None:
                raise AssertionError("execute_statement must not commit")

        monkeypatch.setattr("rey_lib.db._sqlalchemy.core_connection", lambda _c: _Core())

        assert postgres_utils.execute_statement(object(), 'DELETE FROM "a"."b"') == 42
        assert ran == ['DELETE FROM "a"."b"']


class TestDuckdbAgainstTheRealDriver:
    """DuckDB's half, end to end through the adapter."""

    @pytest.fixture
    def conn(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(":memory:")
        connection.execute('CREATE SCHEMA testing')
        connection.execute('CREATE TABLE testing.test3 ("Security Description" TEXT)')
        connection.execute("INSERT INTO testing.test3 VALUES ('a'), ('b'), ('c')")
        yield connection
        connection.close()

    @pytest.fixture
    def adapter(self, monkeypatch: pytest.MonkeyPatch) -> DBAdapter:
        monkeypatch.setattr(DBAdapter, "_provider_for_conn", lambda _self, _conn: "duckdb")
        return DBAdapter()

    def test_delete_all_rows_empties_and_counts(
        self, adapter: DBAdapter, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        assert adapter.delete_all_rows(conn, "testing", "test3") == 3
        assert conn.execute("SELECT count(*) FROM testing.test3").fetchone() == (0,)

    def test_drop_table_removes_it(
        self, adapter: DBAdapter, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter.drop_table(conn, "testing", "test3")

        remaining = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'testing' AND table_name = 'test3'"
        ).fetchone()
        assert remaining == (0,)
