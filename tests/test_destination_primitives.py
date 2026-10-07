"""Emptying and dropping a destination: written once, spelled by the provider.

``DatabaseTable.delete_all_rows`` and ``drop`` (backlog 722) compose their
statement themselves and ask the connector for exactly two things -- how it
quotes an identifier and how its driver runs one statement. These tests hold
both halves: the statement each provider receives, and what each provider's
primitive does with it.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError


class _Provider:
    """A provider module with the real quoting and a recording execute."""

    def __init__(self, quote: Any, count: int = 7, fail: Exception | None = None) -> None:
        self.quote_identifier = quote
        self.statements: list[str] = []
        self._count = count
        self._fail = fail

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        if self._fail is not None:
            raise self._fail
        self.statements.append(sql_text)
        return self._count


def _table(
    name: str = "test3", schema: str = "testing", catalog: str = "",
) -> DatabaseObjectIdentity:
    return DatabaseObjectIdentity(connection="c", catalog=catalog, schema=schema, name=name)


def _adapter_over(monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> DBAdapter:
    monkeypatch.setattr(DBAdapter, "_provider_for_conn", lambda _self, _conn: "postgres")
    monkeypatch.setattr(adapter_module, "_backend", lambda _name: provider)
    return DBAdapter()


def _sqlserver_quote(value: str) -> str:
    """SQL Server's quoting, read from its module without importing pyodbc."""
    pytest.importorskip("pyodbc")
    from rey_lib.db import sqlserver_utils

    return sqlserver_utils.quote_identifier(value)


class TestTheTableWritesTheStatement:
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

        removed = DatabaseTable.delete_all_rows(
            object(), _table(), _adapter_over(monkeypatch, provider),
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

        DatabaseTable.drop(object(), _table(), _adapter_over(monkeypatch, provider))

        assert provider.statements == [f"DROP TABLE {qualified}"]

    def test_sqlserver_brackets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _Provider(_sqlserver_quote)

        DatabaseTable.delete_all_rows(
            object(), _table("trade", "dbo"), _adapter_over(monkeypatch, provider),
        )

        assert provider.statements == ["DELETE FROM [dbo].[trade]"]

    def test_an_embedded_quote_is_doubled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _Provider(postgres_utils.quote_identifier)

        DatabaseTable.delete_all_rows(
            object(), _table('say "hi"'), _adapter_over(monkeypatch, provider),
        )

        assert provider.statements == ['DELETE FROM "testing"."say ""hi"""']

    def test_a_named_catalog_is_quoted_part_by_part(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = _Provider(postgres_utils.quote_identifier)

        DatabaseTable.delete_all_rows(
            object(), _table("t", "main", "warehouse"), _adapter_over(monkeypatch, provider),
        )

        assert provider.statements == ['DELETE FROM "warehouse"."main"."t"']

    @pytest.mark.parametrize("operation", ["drop", "delete_all_rows"])
    def test_a_failure_names_the_table(
        self, monkeypatch: pytest.MonkeyPatch, operation: str,
    ) -> None:
        """As create and widen_column already do (backlog 722)."""
        provider = _Provider(postgres_utils.quote_identifier, fail=DatabaseError("locked"))

        with pytest.raises(DatabaseError, match="testing.test3: locked"):
            getattr(DatabaseTable, operation)(
                object(), _table(), _adapter_over(monkeypatch, provider),
            )


class TestColumnNamesAreTheTables:
    """The destination's columns, asked through the get_table_columns primitive (722)."""

    @pytest.mark.parametrize(("catalog", "schema_asked"), [
        ("", "testing"), ("NaviStage", "NaviStage.testing"),
    ])
    def test_the_schema_reaches_the_primitive_as_it_always_has(
        self, catalog: str, schema_asked: str,
    ) -> None:
        asked: list[tuple[str, str]] = []

        class _Connector:
            def get_table_columns(self, _conn: Any, schema: str, table: str) -> list[str]:
                asked.append((schema, table))
                return ["a", "b"]

        names = DatabaseTable.column_names(
            object(), _table(catalog=catalog), _Connector(),
        )

        assert names == ["a", "b"]
        assert asked == [(schema_asked, "test3")]


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
    """DuckDB's half, end to end through the table and the adapter."""

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
        assert DatabaseTable.delete_all_rows(conn, _table(), adapter) == 3
        assert conn.execute("SELECT count(*) FROM testing.test3").fetchone() == (0,)

    def test_drop_table_removes_it(
        self, adapter: DBAdapter, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        DatabaseTable.drop(conn, _table(), adapter)

        remaining = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'testing' AND table_name = 'test3'"
        ).fetchone()
        assert remaining == (0,)
