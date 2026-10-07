"""The PostgreSQL answer to the adapter's describe contract.

No database is opened. What is asserted is what this provider DECIDED, not what
a server did with it. Creating a table, and what "exists" means, are
``DatabaseTable``'s now (backlog 690) -- see test_database_table.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import postgres_utils
from rey_lib.errors.error_utils import DatabaseError

def _inspector_returns(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    """Replace the shared inspector call; raise by passing an exception."""

    def _metadata_get_columns(_conn: Any, _catalog: str, _schema: str, _table: str):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        "rey_lib.db._sqlalchemy.metadata_get_columns", _metadata_get_columns
    )


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

