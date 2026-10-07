"""DatabaseQuery: the execution mechanics of a row query, nothing else (backlog 730).

Asserted against a recording adapter: the statement and parameters pass through
once and unchanged, in dataset mode; rows come back as plain dictionaries; an
empty answer is an empty tuple; and the object holds no SQL and offers rows only.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from rey_lib.db import database_query as module
from rey_lib.db.database_query import DatabaseQuery


class _Adapter:
    """Answers with the rows it is given, recording what was asked."""

    def __init__(self, rows: Any = None) -> None:
        self.rows = rows
        self.asked: list[tuple[Any, str, dict, str]] = []

    def execute_sql(self, conn: Any, statement: str, params: dict, mode: str) -> Any:
        self.asked.append((conn, statement, params, mode))
        return self.rows


def test_the_statement_and_its_parameters_pass_through_once_in_dataset_mode() -> None:
    adapter, conn = _Adapter([]), object()

    DatabaseQuery("SELECT :a", {"a": 1, "b": None}).rows(conn, adapter)

    assert adapter.asked == [(conn, "SELECT :a", {"a": 1, "b": None}, "dataset_result")]


def test_a_query_without_parameters_binds_none() -> None:
    adapter = _Adapter([])

    DatabaseQuery("SELECT 1").rows(object(), adapter)

    assert adapter.asked[0][2] == {}


def test_rows_arrive_as_plain_dictionaries_in_the_order_returned() -> None:
    adapter = _Adapter([MappingProxyType({"k": 2}), MappingProxyType({"k": 1})])

    rows = DatabaseQuery("SELECT k").rows(object(), adapter)

    assert rows == ({"k": 2}, {"k": 1})
    assert all(type(row) is dict for row in rows)


@pytest.mark.parametrize("answer", [None, []])
def test_no_rows_is_an_empty_tuple(answer: Any) -> None:
    assert DatabaseQuery("SELECT 1").rows(object(), _Adapter(answer)) == ()


def test_it_is_frozen_and_each_query_has_its_own_parameters() -> None:
    first, second = DatabaseQuery("SELECT 1"), DatabaseQuery("SELECT 1")

    assert first.params is not second.params
    with pytest.raises(AttributeError):
        first.statement = "SELECT 2"  # type: ignore[misc]


def test_it_offers_rows_only_and_holds_no_sql() -> None:
    public = {name for name in vars(DatabaseQuery) if not name.startswith("_")}
    code = Path(module.__file__).read_text(encoding="utf-8").split('"""', 2)[2]

    assert public - {"statement", "params"} == {"rows"}
    assert module.__all__ == ["DatabaseQuery"]
    for sql in ("SELECT", "INSERT", "UPDATE", "DELETE", "execute_page", "execute_statements"):
        assert sql not in code, sql
