"""The PostgreSQL answer to the adapter's bulk contract.

No database is opened. SQLAlchemy Core compiles the statement, so what is
asserted is the statement built and the parameters bound -- which is where a
bulk contract goes wrong -- not what a server does with them.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import postgres_utils
from rey_lib.errors.error_utils import DatabaseError


class _CoreConnection:
    """A SQLAlchemy connection that records instead of executing."""

    def __init__(self) -> None:
        self.executions: list[tuple[Any, Any]] = []

    def execute(self, statement: Any, parameters: Any = None) -> None:
        self.executions.append((statement, parameters))


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _CoreConnection:
    """Stand in for the connection the provider would reach for."""
    recorder = _CoreConnection()
    monkeypatch.setattr(
        "rey_lib.db._sqlalchemy.core_connection", lambda _conn: recorder
    )
    return recorder


class TestTheContract:
    """What every provider's bulk_insert promises."""

    def test_empty_input_is_a_no_op(self, core: _CoreConnection) -> None:
        """Nothing scanned is a legitimate result, not a failure."""
        assert postgres_utils.bulk_insert(None, "code", "file_stage", [], ["a"]) == 0
        assert core.executions == []

    def test_rows_go_in_one_statement_not_one_per_row(
        self, core: _CoreConnection
    ) -> None:
        """The point of the mechanism: one round trip, one statement."""
        rows = [{"a": index} for index in range(50)]

        inserted = postgres_utils.bulk_insert(None, "code", "file_stage", rows, ["a"])

        assert inserted == 50
        assert len(core.executions) == 1
        _statement, parameters = core.executions[0]
        assert len(parameters) == 50

    def test_columns_are_bound_in_the_declared_order(
        self, core: _CoreConnection
    ) -> None:
        """The caller owns the order; the provider does not reorder it."""
        postgres_utils.bulk_insert(
            None,
            "code",
            "symbol_stage",
            [{"b": 2, "a": 1, "ignored": 3}],
            ["a", "b"],
        )

        statement, parameters = core.executions[0]
        assert list(parameters[0]) == ["a", "b"]
        assert "ignored" not in parameters[0]
        assert 'INSERT INTO code.symbol_stage' in str(statement)

    def test_a_missing_column_is_named(self, core: _CoreConnection) -> None:
        """A silent NULL would be a load that looked like it worked."""
        with pytest.raises(DatabaseError, match="missing column"):
            postgres_utils.bulk_insert(
                None, "code", "file_stage", [{"a": 1}], ["a", "b"]
            )

    def test_an_empty_string_stays_an_empty_string(
        self, core: _CoreConnection
    ) -> None:
        """PostgreSQL distinguishes it from NULL, and so does this estate.

        MySQL and SQL Server map it to NULL for their own loading history. An
        empty owner means a symbol declared at top level; turning that into
        NULL loses the fact and violates a NOT NULL column.
        """
        postgres_utils.bulk_insert(None, "code", "symbol_stage", [{"owner": ""}], ["owner"])

        _statement, parameters = core.executions[0]
        assert parameters[0]["owner"] == ""


class TestIdentifiersAreNotValues:
    """Identifiers are composed into SQL, so they are checked rather than bound."""

    @pytest.mark.parametrize(
        "identifier", ["code; DROP TABLE x", "two words", '"quoted"', "a-b", ""]
    )
    def test_anything_but_a_plain_name_is_refused(
        self, core: _CoreConnection, identifier: str
    ) -> None:
        with pytest.raises(DatabaseError, match="Invalid PostgreSQL identifier"):
            postgres_utils.bulk_insert(
                None, identifier, "file_stage", [{"a": 1}], ["a"]
            )

    def test_a_column_is_checked_too(self, core: _CoreConnection) -> None:
        with pytest.raises(DatabaseError, match="Invalid PostgreSQL identifier"):
            postgres_utils.bulk_insert(
                None, "code", "file_stage", [{"a": 1}], ["a); DROP TABLE x --"]
            )


class TestBatching:
    """A six-figure load is handed over in pieces, not one burst.

    An indexing run stages roughly 95,000 rows, 86% of them reference edges.
    Sent as a single execute they are materialized as one parameter block and
    pushed at the server uninterrupted, and everything sharing that server
    waits behind them.
    """

    def test_a_small_load_still_travels_in_one_batch(self, core: _CoreConnection) -> None:
        """Ordinary callers behave exactly as they did before batching."""
        rows = [{"a": index} for index in range(10)]

        inserted = postgres_utils.bulk_insert(None, "s", "t", rows, ["a"])

        assert inserted == 10
        assert [len(parameters) for _statement, parameters in core.executions] == [10]

    def test_a_large_load_is_split(self, core: _CoreConnection) -> None:
        """Every row still arrives, and no one execute carries them all."""
        size = postgres_utils.BULK_INSERT_BATCH_SIZE
        rows = [{"a": index} for index in range(size * 2 + 1)]

        inserted = postgres_utils.bulk_insert(None, "s", "t", rows, ["a"])

        sizes = [len(parameters) for _statement, parameters in core.executions]
        assert inserted == size * 2 + 1
        assert sum(sizes) == size * 2 + 1
        assert max(sizes) <= size
        assert len(sizes) == 3

    def test_every_row_arrives_exactly_once_and_in_order(
        self, core: _CoreConnection
    ) -> None:
        """Splitting must not drop, duplicate or reorder a row."""
        rows = [{"a": index} for index in range(postgres_utils.BULK_INSERT_BATCH_SIZE + 7)]

        postgres_utils.bulk_insert(None, "s", "t", rows, ["a"])

        sent = [
            row["a"]
            for _statement, parameters in core.executions
            for row in parameters
        ]
        assert sent == [row["a"] for row in rows]

    def test_a_nonsense_batch_size_is_refused(self, core: _CoreConnection) -> None:
        """Zero would loop forever rather than insert nothing."""
        with pytest.raises(DatabaseError, match="batch_size must be positive"):
            postgres_utils.bulk_insert(None, "s", "t", [{"a": 1}], ["a"], batch_size=0)

        assert core.executions == []
