"""DatabaseQuery: execution mechanics only, in four modes (backlogs 730, 731, 757).

Asserted against a recording adapter: the statement and parameters pass through
once and unchanged, in dataset mode; rows come back as plain dictionaries; an
empty answer is an empty tuple. run and page pass the text and every bound
through unchanged, refuse parameters they cannot bind, and let a failure or a
declination arrive as raised. The object holds no SQL, offers rows, run and page
only, and the result records have one home.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from rey_lib.db import database_query as module
from rey_lib.db.database_query import DatabaseQuery, PageResult, StatementResult, json_value
from rey_lib.errors.error_utils import DatabaseError, UnsupportedDatabaseCapabilityError


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


def test_each_query_has_its_own_parameters_and_reading_state() -> None:
    first, second = DatabaseQuery("SELECT 1"), DatabaseQuery("SELECT 1")

    assert first.params is not second.params
    assert first.order_by is not second.order_by
    assert first.filters is not second.filters


def test_it_offers_the_four_ways_to_run_and_the_live_query_and_holds_no_sql() -> None:
    public = {name for name in vars(DatabaseQuery) if not name.startswith("_")}
    code = Path(module.__file__).read_text(encoding="utf-8").split('"""', 2)[2]

    assert public - {
        "statement", "params", "connection", "pageable", "rows_per_page", "current_page",
        "order_by", "filters",
    } == {"rows", "run", "page", "read", "act", "text", "update", "projection", "execute"}
    for sql in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert sql not in code, sql


class _Executor:
    """Answers run and page as told, recording exactly what was passed."""

    def __init__(
        self, results: Any = None, page: Any = None, raises: Exception | None = None,
    ) -> None:
        self.results, self.answer, self.raises = results or [], page, raises
        self.asked: list[tuple[str, Any, str, dict]] = []

    def execute_statements(self, conn: Any, text: str, **bounds: Any) -> list:
        self.asked.append(("execute_statements", conn, text, bounds))
        if self.raises:
            raise self.raises
        return self.results

    def execute_page(self, conn: Any, text: str, **bounds: Any) -> Any:
        self.asked.append(("execute_page", conn, text, bounds))
        if self.raises:
            raise self.raises
        return self.answer


class TestRunAndPage:
    """SQL text as supplied, run or paged, mechanics only (backlog 731)."""

    def test_run_passes_the_text_and_limit_through_once_and_keeps_every_result(self) -> None:
        results = [StatementResult(columns=["a"], rows=[{"a": 1}], row_count=1),
                   StatementResult(columns=[], rows=[])]
        executor, conn = _Executor(results=results), object()

        answer = DatabaseQuery("SELECT 1; CALL p()").run(conn, limit=10, adapter=executor)

        assert answer == tuple(results)
        assert executor.asked == [
            ("execute_statements", conn, "SELECT 1; CALL p()", {"limit": 10}),
        ]

    def test_page_passes_every_bound_through_unchanged(self) -> None:
        page = PageResult(columns=["a"], rows=[{"a": 1}], total_row_count=None,
                          offset=5, limit=2, max_page_size=50)
        executor, conn = _Executor(page=page), object()
        order_by, filters = [{"column": "a", "direction": "desc"}], [{"column": "a"}]

        answer = DatabaseQuery(" SELECT a FROM t ").page(
            conn, offset=5, limit=2, order_by=order_by, filters=filters, adapter=executor,
        )

        assert answer is page
        assert executor.asked == [("execute_page", conn, " SELECT a FROM t ", {
            "offset": 5, "limit": 2, "order_by": order_by, "filters": filters,
        })]

    @pytest.mark.parametrize("raised", [
        UnsupportedDatabaseCapabilityError("cannot page"), DatabaseError("refused"),
    ])
    def test_a_declination_or_failure_reaches_the_caller_as_raised(
        self, raised: Exception,
    ) -> None:
        query = DatabaseQuery("SELECT 1")

        with pytest.raises(type(raised)) as caught:
            query.page(object(), offset=0, limit=1, adapter=_Executor(raises=raised))
        assert caught.value is raised
        with pytest.raises(type(raised)):
            query.run(object(), limit=1, adapter=_Executor(raises=raised))

    def test_parameters_they_cannot_bind_are_refused_never_dropped(self) -> None:
        executor = _Executor()
        query = DatabaseQuery("SELECT :a", {"a": 1})

        with pytest.raises(DatabaseError, match=r"DatabaseQuery.run .*\['a'\]"):
            query.run(object(), limit=1, adapter=executor)
        with pytest.raises(DatabaseError, match=r"DatabaseQuery.page .*\['a'\]"):
            query.page(object(), offset=0, limit=1, adapter=executor)
        assert executor.asked == []  # refused before anything ran


def test_the_result_records_have_one_home() -> None:
    from rey_lib.db import db_adapter

    assert set(module.__all__) == {
        "DatabaseQuery", "PageResult", "StatementResult", "json_value",
    }
    assert db_adapter.__all__ == ["DBAdapter"]
    source = Path(db_adapter.__file__).read_text(encoding="utf-8")
    assert "class PageResult" not in source and "class StatementResult" not in source


def test_json_value_makes_a_result_value_json_safe() -> None:
    """The one normalization, used by the SQL card and the AI query tool (backlog 732)."""
    assert json_value({"n": 1, "f": 1.5, "s": "x", "b": True, "none": None}) == {
        "n": 1, "f": 1.5, "s": "x", "b": True, "none": None,
    }
    assert json_value(date(2026, 1, 2)) == "2026-01-02"
    assert json_value(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert json_value({1: [date(2026, 1, 2), (Decimal("1.50"),)]}) == {
        "1": ["2026-01-02", ["1.50"]],
    }
    assert json_value(MappingProxyType({"k": Decimal("2")})) == {"k": "2"}


def test_json_value_has_one_implementation() -> None:
    root = Path(module.__file__).resolve().parents[3]
    defined = [
        path for path in root.glob("rey_*/rey_*/**/*.py")
        if "def _json_value(" in path.read_text(encoding="utf-8")
        or "def json_value(" in path.read_text(encoding="utf-8")
    ]

    assert [path.name for path in defined] == ["database_query.py"]


class _Reader:
    """Answers query_rows as a provider does, recording what was asked."""

    def __init__(self, columns: list[str], rows: list[dict[str, Any]]) -> None:
        self.columns, self.rows = columns, rows
        self.asked: list[tuple[Any, str, int | None]] = []

    def query_rows(self, conn: Any, text: str, *, limit: int | None = 1_000) -> tuple:
        self.asked.append((conn, text, limit))
        kept = self.rows if limit is None else self.rows[:limit]
        return self.columns, kept


class TestRead:
    """One read, its columns kept and its rows bounded or not (backlog 757)."""

    def test_the_columns_survive_an_empty_result(self) -> None:
        answer = DatabaseQuery("SELECT a, b FROM t WHERE false").read(
            object(), adapter=_Reader(["a", "b"], []),
        )

        assert answer == StatementResult(columns=["a", "b"], rows=[], row_count=None)

    @pytest.mark.parametrize("limit, expected", [(None, 3), (2, 2), (1, 1)])
    def test_none_is_every_row_and_n_stops_after_n(
        self, limit: int | None, expected: int,
    ) -> None:
        reader, conn = _Reader(["k"], [{"k": 3}, {"k": 1}, {"k": 2}]), object()

        answer = DatabaseQuery("SELECT k FROM t").read(conn, limit=limit, adapter=reader)

        assert answer.rows == [{"k": 3}, {"k": 1}, {"k": 2}][:expected]  # order kept
        assert reader.asked == [(conn, "SELECT k FROM t", limit)]

    def test_it_is_the_canonical_result_record(self) -> None:
        answer = DatabaseQuery("SELECT 1").read(object(), adapter=_Reader(["x"], [{"x": 1}]))

        assert type(answer) is StatementResult

    def test_parameters_it_cannot_bind_are_refused_before_anything_runs(self) -> None:
        reader = _Reader(["a"], [])

        with pytest.raises(DatabaseError, match=r"DatabaseQuery.read .*\['a'\]"):
            DatabaseQuery("SELECT :a", {"a": 1}).read(object(), adapter=reader)
        assert reader.asked == []

    def test_a_real_engine_answers_through_the_provider_fallback(self) -> None:
        """DuckDB reads through the DB-API path query_rows keeps for it."""
        import duckdb

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO t VALUES (2, 'y'), (1, 'x')")

        empty = DatabaseQuery("SELECT a, b FROM t WHERE a > 9").read(conn)
        one = DatabaseQuery("SELECT a, b FROM t ORDER BY a").read(conn, limit=1)
        every = DatabaseQuery("SELECT a, b FROM t ORDER BY a").read(conn)

        assert (empty.columns, empty.rows) == (["a", "b"], [])
        assert one.rows == [{"a": 1, "b": "x"}]
        assert every.rows == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


class _Connection:
    """A shared Connection stand-in: one handle, never opened or closed by the query."""

    def __init__(self) -> None:
        self.name, self.handed = "rey apps", []
        self.closed = False

    def handle(self) -> object:
        handle = object()
        self.handed.append(handle)
        return handle

    def close(self) -> None:
        self.closed = True


class TestTheLiveQuery:
    """The query held live: its reading state, changed by update, acted on by execute."""

    def test_it_reads_as_the_card_does_until_told_otherwise(self) -> None:
        query = DatabaseQuery("SELECT 1")

        assert (query.pageable, query.rows_per_page, query.current_page) == (True, 50, 0)
        assert (query.order_by, query.filters, query.connection) == ([], [], None)

    def test_text_writes_each_parameter_in_as_its_literal(self) -> None:
        query = DatabaseQuery(
            "SELECT * FROM t WHERE a = :a AND b = :b AND c = :c AND d = :d AND x::text = :e",
            {"a": 3, "b": "it's", "c": None, "d": True, "e": "x"},
        )

        assert query.text() == (
            "SELECT * FROM t WHERE a = 3 AND b = 'it''s' AND c = NULL AND d = TRUE "
            "AND x::text = 'x'"
        )
        assert DatabaseQuery("SELECT :ab", {"a": 1}).text() == "SELECT :ab"

    def test_update_changes_live_properties_only(self) -> None:
        query = DatabaseQuery("SELECT :a", {"a": 1})

        query.update(pageable=False, rows_per_page=10, current_page=2)
        assert (query.pageable, query.rows_per_page, query.current_page) == (False, 10, 2)
        assert query.params == {"a": 1}
        with pytest.raises(DatabaseError, match="no live property"):
            query.update(connection=None)

    def test_replacing_the_statement_clears_its_parameters_and_keeps_the_page(self) -> None:
        query = DatabaseQuery("SELECT :a", {"a": 1}, current_page=3)

        query.update(statement="SELECT 2")

        assert (query.statement, query.params, query.current_page) == ("SELECT 2", {}, 3)

    def test_projection_shows_its_text_and_state_and_runs_nothing(self) -> None:
        connection = _Connection()
        query = DatabaseQuery("SELECT :a", {"a": 1}, connection=connection)

        assert query.projection() == {
            "statement": "SELECT 1", "pageable": True, "rows_per_page": 50,
            "current_page": 0, "order_by": [], "filters": [], "connection": "rey apps",
        }
        assert connection.handed == []

    def test_execute_pages_from_its_own_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = PageResult(columns=["a", "doc"], rows=[{"a": date(2026, 1, 2), "doc": {}}],
                          total_row_count=None, offset=20, limit=10, next_offset=30,
                          column_kinds={"doc": "document"})
        executor, connection = _Executor(page=page), _Connection()
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)
        order_by = [{"column": "a", "direction": "desc"}]
        query = DatabaseQuery("SELECT :a", {"a": 1}, connection=connection,
                              rows_per_page=10, current_page=2, order_by=order_by)

        answer = query.execute()

        assert executor.asked == [("execute_page", connection.handed[0], "SELECT 1", {
            "offset": 20, "limit": 10, "order_by": order_by, "filters": None,
        })]
        assert answer == {
            "pageable": True,
            "columns": [{"name": "a"}, {"name": "doc", "kind": "document"}],
            "rows": [{"a": "2026-01-02", "doc": {}}],
            "total_row_count": None, "offset": 20, "limit": 10, "next_offset": 30,
        }
        assert connection.closed is False

    def test_a_provider_that_cannot_page_says_so_to_the_caller(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        declined = UnsupportedDatabaseCapabilityError("cannot page")
        executor = _Executor(raises=declined)
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)

        with pytest.raises(UnsupportedDatabaseCapabilityError):
            DatabaseQuery("CALL p()", connection=_Connection()).execute()
        assert [asked[0] for asked in executor.asked] == ["execute_page"]

    def test_execute_runs_when_not_pageable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = [StatementResult(columns=["a"], rows=[{"a": 1}], row_count=1),
                   StatementResult(columns=[], rows=[])]
        executor = _Executor(results=results)
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)

        answer = DatabaseQuery("SELECT 1; CALL p()", connection=_Connection(),
                               pageable=False).execute()

        assert executor.asked[0][0] == "execute_statements"
        assert executor.asked[0][3] == {"limit": module.RESULT_ROW_LIMIT}
        assert answer == {
            "pageable": False,
            "results": [
                {"columns": [{"name": "a"}], "rows": [{"a": 1}], "row_count": 1,
                 "at_row_limit": False},
                {"columns": [], "rows": [], "row_count": None, "at_row_limit": False},
            ],
            "row_limit": 1_000,
        }

    @pytest.mark.parametrize("query, refusal", [
        (DatabaseQuery("SELECT 1"), "bound to no connection"),
        (DatabaseQuery("  "), "no statement"),
    ])
    def test_execute_refuses_without_a_connection_or_a_statement(
        self, query: DatabaseQuery, refusal: str,
    ) -> None:
        if refusal == "no statement":
            query.connection = _Connection()  # type: ignore[assignment]
        with pytest.raises(DatabaseError, match=refusal):
            query.execute()
