"""A connector describes its document columns and bounds their pages.

Only the connector knows the real database type and controls the fetch, so it
is the connector that states a column is a document and caps the page BEFORE
rows are fetched. The result types carry that as neutral metadata
(``column_kinds``, ``max_page_size``) and learn nothing about JSON.

The assertion that matters is on the statement actually executed: a cap applied
after the fetch would have read the rows it was meant to spare.
"""

from __future__ import annotations

import contextlib
import re
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from rey_lib.db import duckdb_utils, postgres_utils

JSON_OID, JSONB_OID, INT4_OID, TEXT_OID = 114, 3802, 23, 25


class _Result:
    """What ``exec_driver_sql`` hands back, answering only what is asked."""

    def __init__(self, *, description: Any = None, total: int = 0,
                 columns: list[str] | None = None, rows: list[tuple] | None = None) -> None:
        self.cursor = SimpleNamespace(description=description)
        self._total = total
        self._columns = columns or []
        self._rows = rows or []

    def scalar(self) -> int:
        return self._total

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class _Core:
    """A connection that records every statement and answers a fixed table."""

    def __init__(self, description: list[tuple[str, int]], total: int = 300) -> None:
        self.description = description
        self.total = total
        self.executed: list[str] = []

    @contextlib.contextmanager
    def begin(self):
        yield

    def exec_driver_sql(self, sql: str, params: Any = None) -> _Result:
        self.executed.append(sql)
        if sql.startswith("EXPLAIN"):
            return _Result()
        if sql.endswith("LIMIT 0"):
            return _Result(description=self.description)
        if "count(*)" in sql:
            return _Result(total=self.total)
        limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
        names = [name for name, _code in self.description]
        return _Result(columns=names, rows=[tuple(range(len(names)))] * min(limit, self.total))


@pytest.fixture()
def postgres(monkeypatch: pytest.MonkeyPatch):
    """Run postgres_utils.execute_page against a recording core."""
    def _run(description: list[tuple[str, int]], limit: int) -> tuple[Any, _Core]:
        core = _Core(description)

        @contextlib.contextmanager
        def _own(_conn: Any, **_kwargs: Any):
            yield core

        monkeypatch.setattr("rey_lib.db._sqlalchemy.own_connection", _own)
        page = postgres_utils.execute_page(object(), "select * from t", offset=0, limit=limit)
        return page, core
    return _run


class TestPostgresPages:

    def test_a_document_column_bounds_the_statement_before_it_fetches(self, postgres) -> None:
        page, core = postgres([("id", INT4_OID), ("settings", JSONB_OID)], 250)

        fetched = [sql for sql in core.executed
                   if "LIMIT" in sql and not sql.endswith("LIMIT 0")]
        assert len(fetched) == 1
        assert "LIMIT 50" in fetched[0], "the cap was not applied before the fetch"
        assert page.limit == 50
        assert page.max_page_size == 50
        assert page.column_kinds == {"settings": "document"}
        assert len(page.rows) == 50

    def test_json_is_a_document_too(self, postgres) -> None:
        page, _core = postgres([("doc", JSON_OID)], 100)

        assert page.column_kinds == {"doc": "document"}
        assert page.limit == 50

    def test_a_smaller_request_is_left_as_asked(self, postgres) -> None:
        page, _core = postgres([("doc", JSONB_OID)], 25)

        assert page.limit == 25
        assert page.max_page_size == 50

    def test_plain_columns_are_unbounded_and_unstated(self, postgres) -> None:
        page, core = postgres([("id", INT4_OID), ("name", TEXT_OID)], 250)

        assert any("LIMIT 250" in sql for sql in core.executed)
        assert page.limit == 250
        assert page.max_page_size is None
        assert page.column_kinds == {}


class TestPostgresStatements:

    def test_an_eager_result_states_its_document_columns(self, monkeypatch) -> None:
        class _Cursor:
            description = [("id", INT4_OID), ("doc", JSONB_OID)]
            rowcount = 1

            def execute(self, _sql: str) -> None:
                pass

            def fetchmany(self, _size: int) -> list[tuple]:
                return [(1, {"a": 1})]

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            "rey_lib.db._sqlalchemy.raw_dbapi_connection",
            lambda _conn: SimpleNamespace(cursor=_Cursor),
        )
        (result,) = postgres_utils.execute_statements(object(), "select 1")

        assert result.column_kinds == {"doc": "document"}


class TestDuckDBPages:
    """Against the real driver: its description names a JSON column 'JSON'."""

    @pytest.fixture()
    def conn(self):
        connection = duckdb.connect()
        connection.execute(
            "CREATE TABLE docs AS SELECT i, ('{\"k\":' || i || '}')::JSON AS doc FROM range(300) r(i)"
        )
        connection.execute("CREATE TABLE plain AS SELECT i FROM range(300) r(i)")
        yield connection
        connection.close()

    def test_a_json_column_bounds_the_page(self, conn) -> None:
        page = duckdb_utils.execute_page(conn, "SELECT * FROM docs", offset=0, limit=250)

        assert len(page.rows) == 50
        assert page.limit == 50
        assert page.max_page_size == 50
        assert page.column_kinds == {"doc": "document"}
        assert page.next_offset == 50

    def test_plain_columns_are_unbounded(self, conn) -> None:
        page = duckdb_utils.execute_page(conn, "SELECT * FROM plain", offset=0, limit=250)

        assert len(page.rows) == 250
        assert page.max_page_size is None
        assert page.column_kinds == {}
