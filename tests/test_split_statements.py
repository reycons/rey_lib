"""Dividing SQL text into statements, before anything runs (#1418).

PostgreSQL divides by its own parser; a provider with none divides nothing and
the adapter says so with None. No database is opened.
"""

from __future__ import annotations

import pytest

from rey_lib.db import postgres_utils
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError


class TestPostgresDividesByItsOwnParser:
    """libpg_query through pglast: a semicolon inside a literal divides nothing."""

    def test_each_statement_in_order(self) -> None:
        assert postgres_utils.split_statements("select 1; select 2;") == ["select 1", "select 2"]

    def test_literals_bodies_and_comments_divide_nothing(self) -> None:
        text = "-- a;b\nselect 'x;y';\n/* c; */ call p();\nselect $$p;q$$"

        assert postgres_utils.split_statements(text) == [
            "select 'x;y'", "call p()", "select $$p;q$$"]

    def test_one_statement_is_one(self) -> None:
        assert postgres_utils.split_statements("select 1") == ["select 1"]

    def test_text_that_does_not_parse_is_refused_by_position_without_its_text(self) -> None:
        with pytest.raises(DatabaseError) as raised:
            postgres_utils.split_statements("select 1; selec 2;")

        message = str(raised.value)
        assert "position 11" in message and "nothing was executed" in message
        assert "selec" not in message


class TestTheAdapterDispatches:
    """The provider's own parser, or None where it declares none."""

    @pytest.mark.parametrize("provider, divided", [
        ("postgres", ["select 1", "select 2"]),
        ("duckdb", None),
        ("bigquery", None),
    ])
    def test_the_providers_answer(
        self, monkeypatch: pytest.MonkeyPatch, provider: str, divided: list[str] | None,
    ) -> None:
        monkeypatch.setattr(DBAdapter, "_provider_for_conn", lambda _self, _conn: provider)

        assert DBAdapter().split_statements(object(), "select 1; select 2") == divided
