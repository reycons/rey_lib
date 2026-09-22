"""The PostgreSQL answer to the adapter's describe and staging contracts.

No database is opened. What is asserted is what this provider DECIDED -- the
DDL composed, the identifiers refused, the transaction it does not end -- not
what a server did with any of it.

MySQL and SQL Server established this interface, and they are not templates for
it. Each of the three decisions below departs from them deliberately, and the
tests are written so that quietly copying either implementation back in would
fail rather than pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import postgres_utils
from rey_lib.errors.error_utils import DatabaseError

_COLUMNS = [("trade_id", "INTEGER"), ("symbol", "VARCHAR(12)"), ("price", "DECIMAL(12,2)")]


class _CoreConnection:
    """A SQLAlchemy connection that records DDL instead of executing it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def exec_driver_sql(self, sql_text: str) -> None:
        self.statements.append(sql_text)


class _Connection:
    """The handle the loader passes down, watching for a transaction it ends."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _CoreConnection:
    """Stand in for the connection the provider reaches for."""
    recorder = _CoreConnection()
    monkeypatch.setattr(
        "rey_lib.db._sqlalchemy.core_connection", lambda _conn: recorder
    )
    return recorder


def _columns_are(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Fix whether the table is there, so only the decision is tested.

    Creation asks ``table_exists``, not whether a column list came back empty:
    a table with no columns is not a thing, but a failed lookup that answered
    ``[]`` would be read as "create it" and the DDL would then collide.
    """
    monkeypatch.setattr(
        postgres_utils, "table_exists", lambda *_a, **_k: bool(names)
    )
    monkeypatch.setattr(
        postgres_utils, "get_table_columns", lambda *_a, **_k: names
    )


def _inspector_returns(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    """Replace the shared inspector call; raise by passing an exception."""

    def _metadata_get_columns(_conn: Any, _catalog: str, _schema: str, _table: str):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        "rey_lib.db._sqlalchemy.metadata_get_columns", _metadata_get_columns
    )


class TestTableExists:
    """Existence asked directly, because creation policy now depends on it."""

    def test_it_asks_the_inspector_rather_than_counting_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """has_table is the Inspector's own answer to this question.

        Deriving it from a column list would conflate "there is no such
        table" with "I was given no columns" -- here the table exists and
        reports nothing, and existence must still be True.
        """
        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.metadata_table_exists",
            lambda _conn, _schema, _table: True,
        )
        _inspector_returns(monkeypatch, [])

        assert postgres_utils.table_exists(None, "staging", "trade") is True

    def test_an_absent_table_is_False(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The answer the loader refuses or creates on."""
        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.metadata_table_exists",
            lambda _conn, _schema, _table: False,
        )

        assert postgres_utils.table_exists(None, "staging", "trade") is False

    def test_a_catalog_failure_is_reported_not_answered_False(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost connection must not read as "the table is not there".

        Answering False would send the loader down the create path on a
        connection that cannot answer -- or refuse a load whose table is
        present.
        """
        def _raise(_conn, _schema, _table):
            raise RuntimeError("connection closed")

        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.metadata_table_exists", _raise
        )

        with pytest.raises(DatabaseError) as raised:
            postgres_utils.table_exists(None, "staging", "trade")

        assert "staging.trade" in str(raised.value)
        assert raised.value.__cause__ is not None

    @pytest.mark.parametrize(
        "schema,table",
        [("stag ing", "trade"), ("staging", "trade; DROP TABLE x")],
    )
    def test_an_identifier_that_is_not_a_plain_name_is_refused(
        self, schema: str, table: str
    ) -> None:
        """Refused before the catalog is touched, as the siblings refuse."""
        with pytest.raises(DatabaseError):
            postgres_utils.table_exists(None, schema, table)


class TestGetTableColumns:
    """The first statement of a load, and the one that used to raise."""

    def test_columns_come_back_in_ordinal_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The order IS the contract -- the loader compares it to a file header."""
        _inspector_returns(
            monkeypatch,
            [{"name": "trade_id"}, {"name": "symbol"}, {"name": "price"}],
        )

        assert postgres_utils.get_table_columns(None, "staging", "trade") == [
            "trade_id",
            "symbol",
            "price",
        ]

    def test_a_table_that_does_not_exist_is_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unambiguous, because a table that exists always has a column.

        The inspector raises NoSuchTableError where MySQL's implementation
        finds no match and returns []; the caller must not have to tell the
        two providers apart.
        """
        from sqlalchemy.exc import NoSuchTableError

        _inspector_returns(monkeypatch, NoSuchTableError("trade"))

        assert postgres_utils.get_table_columns(None, "staging", "trade") == []

    def test_any_other_catalog_failure_is_reported_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost connection must not read as "the table is not there".

        That would send the loader down the create-a-staging-table path on a
        connection that cannot answer, and the real fault would surface later
        wearing someone else's name.
        """
        _inspector_returns(monkeypatch, RuntimeError("connection closed"))

        with pytest.raises(DatabaseError) as raised:
            postgres_utils.get_table_columns(None, "staging", "trade")

        assert "staging.trade" in str(raised.value)
        assert raised.value.__cause__ is not None

    @pytest.mark.parametrize(
        "schema,table",
        [("stag ing", "trade"), ("staging", "trade; DROP TABLE x")],
    )
    def test_an_identifier_that_is_not_a_plain_name_is_refused(
        self, schema: str, table: str
    ) -> None:
        """Refused before the catalog is touched, as bulk_insert refuses."""
        with pytest.raises(DatabaseError):
            postgres_utils.get_table_columns(None, schema, table)


class TestCreateStagingTable:
    """Three decisions taken for PostgreSQL rather than carried over."""

    def test_an_existing_table_is_not_recreated_and_answers_False(
        self, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contract distinguishes "created now" from "already there".

        MySQL returns True either way, so its caller cannot tell. Here the
        answer is established before deciding, which is the only way the
        return value means what the adapter documents.
        """
        _columns_are(monkeypatch, ["trade_id"])

        created = postgres_utils.create_staging_table_if_not_exists(
            _Connection(), "staging", "trade", _COLUMNS
        )

        assert created is False
        assert core.statements == []           # no DDL at all

    def test_a_missing_table_is_created_and_answers_True(
        self, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One statement, carrying every column in the order given."""
        _columns_are(monkeypatch, [])

        created = postgres_utils.create_staging_table_if_not_exists(
            _Connection(), "staging", "trade", _COLUMNS
        )

        assert created is True
        assert len(core.statements) == 1
        ddl = core.statements[0]
        assert 'CREATE TABLE IF NOT EXISTS "staging"."trade"' in ddl
        assert ddl.index("trade_id") < ddl.index("symbol") < ddl.index("price")

    def test_the_neutral_types_are_written_through_unmapped(
        self, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adapter's vocabulary is PostgreSQL's own spelling.

        A translation table would map every entry to itself. This asserts the
        absence of one, so adding a mapping layer has to be a deliberate act
        with a reason rather than a transplant from a provider that needs it.
        """
        _columns_are(monkeypatch, [])

        postgres_utils.create_staging_table_if_not_exists(
            _Connection(), "staging", "trade", _COLUMNS
        )

        ddl = core.statements[0]
        assert "INTEGER" in ddl
        assert "VARCHAR(12)" in ddl
        assert "DECIMAL(12,2)" in ddl

    def test_it_does_not_end_the_transaction(
        self, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PostgreSQL DDL is transactional, so the staging table joins the load.

        bulk_insert does not commit either, and the two run inside one load. A
        commit here would leave the staging table behind when the load that
        needed it fails -- making a failed load partly durable. The other
        providers commit because their DDL is not transactional; copying that
        in would break this and is what the assertion is for.
        """
        _columns_are(monkeypatch, [])
        conn = _Connection()

        postgres_utils.create_staging_table_if_not_exists(
            conn, "staging", "trade", _COLUMNS
        )

        assert conn.commits == 0
        assert conn.rollbacks == 0

    def test_a_failed_create_is_reported_and_leaves_the_transaction_alone(
        self, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rolling back here would discard work this function did not do.

        The load owns the transaction it opened; a provider that rolls it back
        on its own would throw away whatever else that load had already
        staged.
        """
        _columns_are(monkeypatch, [])
        conn = _Connection()
        monkeypatch.setattr(
            core, "exec_driver_sql",
            lambda _sql: (_ for _ in ()).throw(RuntimeError("permission denied")),
        )

        with pytest.raises(DatabaseError) as raised:
            postgres_utils.create_staging_table_if_not_exists(
                conn, "staging", "trade", _COLUMNS
            )

        assert "staging.trade" in str(raised.value)
        assert conn.rollbacks == 0
        assert raised.value.__cause__ is not None


class TestWhatIsRefusedBeforeAnyDDLIsComposed:
    """Types are interpolated, so they take the boundary identifiers take."""

    @pytest.mark.parametrize(
        "sql_type",
        [
            "VARCHAR(12); DROP TABLE trade",
            "INTEGER DEFAULT (SELECT 1)",
            "",
        ],
    )
    def test_a_type_that_is_not_a_bare_type_name_is_refused(
        self, sql_type: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_column_defs supplies these, and a config supplies part of it.

        The column NAME has always been validated; the TYPE was not, and it is
        written into the same statement by the same mechanism.
        """
        _columns_are(monkeypatch, [])

        with pytest.raises(DatabaseError) as raised:
            postgres_utils.create_staging_table_if_not_exists(
                _Connection(), "staging", "trade", [("price", sql_type)]
            )

        assert "column type" in str(raised.value)

    @pytest.mark.parametrize(
        "sql_type", ["INTEGER", "VARCHAR(40)", "DECIMAL(12,2)", "TIMESTAMP", "TEXT"]
    )
    def test_every_type_the_adapter_documents_is_accepted(
        self, sql_type: str, core: _CoreConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal must not reject the vocabulary it exists to protect."""
        _columns_are(monkeypatch, [])

        postgres_utils.create_staging_table_if_not_exists(
            _Connection(), "staging", "trade", [("value", sql_type)]
        )

        assert sql_type in core.statements[0]

    def test_no_columns_is_refused_rather_than_creating_an_empty_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A table with no columns is not a table the load could ever use."""
        _columns_are(monkeypatch, [])

        with pytest.raises(DatabaseError) as raised:
            postgres_utils.create_staging_table_if_not_exists(
                _Connection(), "staging", "trade", []
            )

        assert "no columns" in str(raised.value)
