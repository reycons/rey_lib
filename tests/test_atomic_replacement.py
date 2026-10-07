"""Replace and recreate either complete atomically or leave the target unchanged (backlog 516).

DataLoader stages the new rows beside the destination; DatabaseTable.replace_rows
and replace_table promote them. The contract is the outcome, so where an engine
is available it is asserted against the engine: DuckDB, in memory, through the
real DBAdapter. For PostgreSQL, SQL Server and MySQL no engine runs here; each
of their paths is asserted on the property that makes it atomic -- a single
statement for PostgreSQL's autocommit handle, commit-together / roll-back-
together for the transactional connections, MySQL's single RENAME -- and on
what a failure leaves behind.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from rey_lib.data.logical_type import LogicalType
from rey_lib.db.data_loader import DataLoader
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError, UnsupportedDatabaseCapabilityError

_TARGET = DatabaseObjectIdentity(connection="d", catalog="", schema="s", name="t")
_STAGING = DatabaseObjectIdentity(connection="d", catalog="", schema="s", name="t__rey_stage")
_COLUMNS = [("a", LogicalType("integer")), ("b", LogicalType("text", length=20))]
_OLD = [(1, "old", "x"), (2, "old", "y")]


@pytest.fixture()
def duck() -> duckdb.DuckDBPyConnection:
    """A destination holding two rows and a column the load does not carry."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA s")
    conn.execute("CREATE TABLE s.t (a INTEGER, b VARCHAR, c VARCHAR)")
    conn.execute("INSERT INTO s.t VALUES (1, 'old', 'x'), (2, 'old', 'y')")
    return conn


def _rows(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("SELECT * FROM s.t ORDER BY a").fetchall()


def _tables(conn: duckdb.DuckDBPyConnection) -> list[str]:
    return sorted(row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 's'",
    ).fetchall())


def _columns(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 's' AND table_name = 't' ORDER BY ordinal_position",
    ).fetchall()


class _FailingFill(DBAdapter):
    """The real adapter, except that filling any table fails."""

    def bulk_insert(self, *_args: Any, **_kwargs: Any) -> int:
        raise DatabaseError("the fill failed")


class TestOnARealEngine:
    """DuckDB in memory, through the real adapter: the outcome itself."""

    def test_replace_replaces_the_rows_and_keeps_the_table(self, duck) -> None:
        loaded = DataLoader(adapter=DBAdapter(), replace_destination=True).load(
            duck, _TARGET, [{"a": 3, "b": "new"}], _COLUMNS,
        )

        assert loaded == 1
        assert _rows(duck) == [(3, "new", None)]  # column c is the table's own, kept
        assert _tables(duck) == ["t"]

    def test_recreate_rebuilds_the_table_from_the_load(self, duck) -> None:
        DataLoader(adapter=DBAdapter(), recreate_destination=True).load(
            duck, _TARGET, [{"a": 3, "b": "new"}], _COLUMNS,
        )

        assert _rows(duck) == [(3, "new")]
        # Recreate's meaning: exactly the load's columns, all nullable, as
        # DatabaseTable.create builds them; nothing of the old table survives.
        assert [(name, nullable) for name, _type, nullable in _columns(duck)] == [
            ("a", "YES"), ("b", "YES"),
        ]
        assert _tables(duck) == ["t"]

    @pytest.mark.parametrize("mode", ["replace_destination", "recreate_destination"])
    def test_a_failed_fill_leaves_the_target_unchanged(self, duck, mode: str) -> None:
        # The fill's DatabaseError is replaced by DuckDB refusing the retry
        # path's conn.rollback() with no transaction open -- a defect older
        # than 516 (the same line ran before it), tracked as backlog 746.
        # What this asserts is 516's: the target is untouched.
        with pytest.raises((DatabaseError, duckdb.TransactionException)):
            DataLoader(adapter=_FailingFill(), **{mode: True}).load(
                duck, _TARGET, [{"a": 3, "b": "new"}], _COLUMNS,
            )

        assert _rows(duck) == _OLD
        assert _tables(duck) == ["t"]

    def test_a_failed_promotion_leaves_the_target_unchanged(self, duck) -> None:
        """Staging takes the text; the target's INTEGER column refuses it --
        after the removal has begun inside the promotion."""
        text = [("a", LogicalType("text", length=20)), ("b", LogicalType("text", length=20))]

        with pytest.raises(DatabaseError, match="Conversion"):
            DataLoader(adapter=DBAdapter(), replace_destination=True).load(
                duck, _TARGET, [{"a": "not a number", "b": "new"}], text,
            )

        assert _rows(duck) == _OLD
        assert _tables(duck) == ["t"]
        # The connection is usable afterwards: the promotion's transaction ended.
        assert duck.execute("SELECT count(*) FROM s.t").fetchone() == (2,)

    def test_a_staging_table_a_failed_run_left_is_replaced(self, duck) -> None:
        duck.execute("CREATE TABLE s.t__rey_stage (stale INTEGER)")

        DataLoader(adapter=DBAdapter(), replace_destination=True).load(
            duck, _TARGET, [{"a": 3, "b": "new"}], _COLUMNS,
        )

        assert _rows(duck) == [(3, "new", None)]
        assert _tables(duck) == ["t"]


class _Conn:
    """A transactional connection's boundaries, counted."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _Connector:
    """The primitives DatabaseTable uses, as one provider answers them."""

    def __init__(self, provider: str, *, fail_on: str = "") -> None:
        self._provider = provider
        self._fail_on = fail_on
        self.statements: list[str] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return f"[{name}]" if self._provider == "sqlserver" else f'"{name}"'

    def get_table_columns(self, _conn: Any, _schema: str, _table: str) -> list[str]:
        return ["a", "b"]

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        self.statements.append(sql_text)
        if self._fail_on and sql_text.startswith(self._fail_on):
            raise DatabaseError("refused")
        return 0


class TestEachProviderPathIsAtomic:
    """Where no engine runs here: the property that makes each path atomic."""

    @pytest.mark.parametrize("method", ["replace_rows", "replace_table"])
    def test_postgres_promotes_in_one_statement(self, method: str) -> None:
        """An autocommit handle runs one statement as one transaction, so a
        single statement is all-or-nothing; nothing else is sent."""
        connector = _Connector("postgres")

        getattr(DatabaseTable, method)(_Conn(), _TARGET, _STAGING, connector)

        assert len(connector.statements) == 1
        (statement,) = connector.statements
        if method == "replace_rows":
            assert statement.startswith('WITH removed AS (DELETE FROM "s"."t") INSERT INTO "s"."t"')
        else:
            assert statement.startswith("DO $rey$ BEGIN DROP TABLE") and "RENAME TO" in statement

    @pytest.mark.parametrize("provider", ["sqlserver", "mysql"])
    def test_transactional_rows_commit_together_or_roll_back_together(
        self, provider: str,
    ) -> None:
        conn, connector = _Conn(), _Connector(provider)
        DatabaseTable.replace_rows(conn, _TARGET, _STAGING, connector)
        assert (conn.commits, conn.rollbacks) == (1, 0)

        conn, failing = _Conn(), _Connector(provider, fail_on="INSERT")
        with pytest.raises(DatabaseError):
            DatabaseTable.replace_rows(conn, _TARGET, _STAGING, failing)
        # The DELETE ran, and is undone with the insert: nothing was committed.
        assert failing.statements[0].startswith("DELETE FROM")
        assert (conn.commits, conn.rollbacks) == (0, 1)

    def test_sqlserver_table_replacement_commits_together_or_rolls_back(self) -> None:
        conn, failing = _Conn(), _Connector("sqlserver", fail_on="EXEC")
        with pytest.raises(DatabaseError):
            DatabaseTable.replace_table(conn, _TARGET, _STAGING, failing)
        assert failing.statements[0] == "DROP TABLE [s].[t]"
        assert (conn.commits, conn.rollbacks) == (0, 1)

        conn, connector = _Conn(), _Connector("sqlserver")
        DatabaseTable.replace_table(conn, _TARGET, _STAGING, connector)
        assert connector.statements[1] == "EXEC sp_rename N'[s].[t__rey_stage]', N't'"
        assert (conn.commits, conn.rollbacks) == (1, 0)

    def test_sqlserver_renames_in_the_tables_own_database(self) -> None:
        connector = _Connector("sqlserver")
        elsewhere = DatabaseObjectIdentity(connection="d", catalog="db2", schema="s", name="t")
        staged = DatabaseObjectIdentity(
            connection="d", catalog="db2", schema="s", name="t__rey_stage",
        )

        DatabaseTable.replace_table(_Conn(), elsewhere, staged, connector)

        assert connector.statements[1] == (
            "EXEC [db2].sys.sp_rename N'[s].[t__rey_stage]', N't'"
        )

    def test_mysql_swaps_both_names_in_one_rename(self) -> None:
        """MySQL's DROP and CREATE commit implicitly; RENAME TABLE of both
        names is one atomic statement, so the swap is the promotion."""
        connector = _Connector("mysql")

        DatabaseTable.replace_table(_Conn(), _TARGET, _STAGING, connector)

        assert connector.statements == [
            'RENAME TABLE "s"."t" TO "s"."t__rey_old", "s"."t__rey_stage" TO "s"."t"',
            'DROP TABLE "s"."t__rey_old"',
        ]

    def test_mysql_says_the_target_was_replaced_when_the_old_table_stays(self) -> None:
        connector = _Connector("mysql", fail_on="DROP")

        with pytest.raises(DatabaseError, match="was replaced, but its previous table"):
            DatabaseTable.replace_table(_Conn(), _TARGET, _STAGING, connector)

    @pytest.mark.parametrize("method", ["replace_rows", "replace_table"])
    def test_a_provider_with_no_atomic_answer_is_refused_before_anything_runs(
        self, method: str,
    ) -> None:
        connector = _Connector("oracle")

        with pytest.raises(UnsupportedDatabaseCapabilityError, match="no atomic"):
            getattr(DatabaseTable, method)(_Conn(), _TARGET, _STAGING, connector)

        assert connector.statements == []
