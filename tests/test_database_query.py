"""DatabaseQuery: execution mechanics only, in four modes (backlogs 730, 731, 757).

Asserted against a recording adapter: the statement and parameters pass through
once and unchanged, in dataset mode; rows come back as plain dictionaries; an
empty answer is an empty tuple. run and page pass the text and every bound
through unchanged, refuse parameters they cannot bind, and let a failure or a
declination arrive as raised. The object holds no SQL, offers rows, run and page
only, and the result records have one home.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from rey_lib.db import database_query as module
from rey_lib.db.database_query import DatabaseQuery, PageResult, StatementResult
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
        "order_by", "filters", "current_result", "selection",
    } == {"rows", "run", "page", "read", "act", "text", "update", "projection", "execute",
          # Its statement, linted by the artifact engine; read-only (#1252).
          "lint", "save", "from_saved"}
    for sql in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert sql not in code, sql


class _Executor:
    """Answers run and page as told, recording exactly what was passed."""

    def __init__(
        self, results: Any = None, page: Any = None, raises: Exception | None = None,
        statements: list[str] | None = None,
    ) -> None:
        self.results, self.answer, self.raises = results or [], page, raises
        # What the provider's parser answers: None is a provider that divides nothing.
        self.statements = statements
        self.asked: list[tuple[str, Any, str, dict]] = []

    def split_statements(self, conn: Any, text: str) -> list[str] | None:
        if isinstance(self.statements, Exception):
            raise self.statements
        return self.statements

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
        "DatabaseQuery", "PageResult", "StatementResult",
    }
    assert db_adapter.__all__ == ["DBAdapter"]
    source = Path(db_adapter.__file__).read_text(encoding="utf-8")
    assert "class PageResult" not in source and "class StatementResult" not in source


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

        assert (query.pageable, query.rows_per_page, query.current_page) == (True, 200, 0)
        assert query.current_result == 0
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
            query.update(params={})

    def test_a_new_connection_is_applied_and_moves_nothing_else(self) -> None:
        first, second = _Connection(), _Connection()
        query = DatabaseQuery("SELECT 1", connection=first, current_page=3)

        query.update(connection=second)

        assert query.connection is second and query.current_page == 3
        assert first.closed is False and first.handed == [] and second.handed == []

    def test_replacing_the_statement_clears_its_parameters_and_keeps_the_page(self) -> None:
        query = DatabaseQuery("SELECT :a", {"a": 1}, current_page=3)

        query.update(statement="SELECT 2")

        assert (query.statement, query.params, query.current_page) == ("SELECT 2", {}, 3)

    def test_projection_shows_its_text_and_state_and_runs_nothing(self) -> None:
        connection = _Connection()
        query = DatabaseQuery("SELECT :a", {"a": 1}, connection=connection)

        assert query.projection() == {
            "statement": "SELECT 1", "pageable": True, "rows_per_page": 200,
            "current_page": 0, "current_result": 0, "order_by": [], "filters": [],
            "connection": "rey apps",
            "navigation": {"modified": False, "can_close": True},
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
        assert {k: v for k, v in answer.items() if k not in ("output", "statistics")} == {
            "pageable": True,
            "columns": [{"name": "a"}, {"name": "doc", "kind": "document"}],
            # The provider's own values: making them JSON-safe is the sending boundary's.
            "rows": [{"a": date(2026, 1, 2), "doc": {}}],
            "total_row_count": None, "offset": 20, "limit": 10, "next_offset": 30,
        }
        assert connection.closed is False
        # Reattaching reads this same owner's result without executing again.
        assert query.projection()["result"] == answer
        assert len(executor.asked) == 1
        assert query.connection is connection

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
        assert {k: v for k, v in answer.items() if k not in ("output", "statistics")} == {
            "pageable": False,
            "results": [
                {"columns": [{"name": "a"}], "rows": [{"a": 1}], "row_count": 1,
                 "at_row_limit": False},
                {"columns": [], "rows": [], "row_count": None, "at_row_limit": False},
            ],
            "row_limit": 1_000,
        }

    def test_execution_content_describes_actual_results_without_another_execution(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Executor(results=[StatementResult(columns=[], rows=[], row_count=3),
                                     StatementResult(columns=["a"], rows=[{"a": 1}])])
        monkeypatch.setattr(module, "_adapter", lambda _: executor)
        ticks = iter([1.0, 1.125])
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
        query = DatabaseQuery("the caller's text", connection=_Connection(), pageable=False)

        answer = query.execute()

        assert answer["output"]["text"] == "Result 1: 3 rows affected.\nResult 2: 1 rows returned."
        assert answer["output"]["language"] == "text"
        assert len(answer["statistics"]) == 2
        assert "Execution time (all results): 125.000 ms" in answer["statistics"][0]["text"]
        assert "Rows affected: 3" in answer["statistics"][0]["text"]
        assert "Rows affected: not reported" in answer["statistics"][1]["text"]
        assert query.projection()["result"] is answer
        assert len(executor.asked) == 1

    def test_failed_execution_replaces_successful_result_without_swallowing_the_failure(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Executor(results=[StatementResult(columns=["a"], rows=[{"a": 1}])])
        monkeypatch.setattr(module, "_adapter", lambda _: executor)
        query = DatabaseQuery("the caller's text", connection=_Connection(), pageable=False)
        query.execute()
        failure = DatabaseError("database refused this run")
        executor.raises = failure

        with pytest.raises(DatabaseError) as raised:
            query.execute()

        assert raised.value is failure
        result = query.projection()["result"]
        assert result["results"] == []
        assert result["output"]["text"] == str(failure)
        assert "Execution failed." in result["statistics"][0]["text"]
        assert len(executor.asked) == 2


class TestTheSelection:
    """A highlight runs in place of the statement, which stays whole (#1419)."""

    def test_the_selection_runs_and_the_statement_is_kept(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Executor(results=[StatementResult(columns=["a"], rows=[{"a": 2}])])
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)
        query = DatabaseQuery("p1; p2", connection=_Connection(), pageable=False)

        query.update(statement="p1; p2", selection="p2")
        query.execute()

        assert executor.asked[0][2] == "p2"
        assert query.statement == "p1; p2"
        assert query.projection()["statement"] == "p1; p2"

    def test_a_new_statement_drops_a_highlight_made_in_the_old_one(self) -> None:
        query = DatabaseQuery("p1", selection="p1")

        query.update(statement="p2")

        assert query.selection == ""


class _Script(_Executor):
    """A provider that divides the text, and answers each statement as told."""

    def __init__(self, answers: dict[str, Any], statements: list[str] | Exception) -> None:
        super().__init__(statements=statements)  # type: ignore[arg-type]
        self.answers = answers

    def execute_statements(self, conn: Any, text: str, **bounds: Any) -> list:
        self.asked.append(("execute_statements", conn, text, bounds))
        answer = self.answers[text]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _rows(count: int) -> list[dict[str, int]]:
    return [{"a": at} for at in range(count)]


class TestTheScript:
    """Several statements: each runs once, in order; results retained and paged (#1418)."""

    def _query(self, monkeypatch: pytest.MonkeyPatch, executor: _Executor) -> DatabaseQuery:
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)
        return DatabaseQuery("s1; s2; s3", connection=_Connection())

    def test_each_statement_runs_once_in_order_and_answers_its_first_page(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Script({
            "s1": [StatementResult(columns=["a"], rows=_rows(450))],
            "s2": [StatementResult(columns=[], rows=[], row_count=1)],
            "s3": [StatementResult(columns=["a"], rows=_rows(1))],
        }, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)

        answer = query.execute()

        assert [(asked[2], asked[3]) for asked in executor.asked] == [
            ("s1", {"limit": 1_000}), ("s2", {"limit": 1_000}), ("s3", {"limit": 1_000})]
        first, second, third = answer["results"]
        assert first["rows"] == _rows(200)
        assert first["paging"] == {"offset": 0, "limit": 200, "total_row_count": 450,
                                   "next_offset": 200}
        assert first["columns"] == [{"name": "a", "sortable": False, "filter": False}]
        assert second == {"columns": [], "rows": [], "row_count": 1, "at_row_limit": False}
        assert third["paging"]["next_offset"] is None
        assert answer["output"]["text"].splitlines() == [
            "Result 1: 450 rows returned.", "Result 2: 1 rows affected.",
            "Result 3: 1 rows returned."]

    def test_a_page_is_read_from_what_is_retained_and_nothing_runs_again(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Script({"s1": [StatementResult(columns=["a"], rows=_rows(450))],
                            "s2": [StatementResult(columns=["a"], rows=_rows(300))],
                            "s3": [StatementResult(columns=[], rows=[], row_count=0)]},
                           ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)
        query.execute()

        query.update(current_result=1, current_page=1)
        answer = query.execute()

        assert len(executor.asked) == 3
        assert answer["current_result"] == 1
        # Only the result asked for moves; the others stay on their own page.
        assert answer["results"][0]["paging"]["offset"] == 0
        assert answer["results"][1]["rows"] == _rows(300)[200:300]
        assert answer["results"][1]["paging"] == {"offset": 200, "limit": 200,
                                                  "total_row_count": 300, "next_offset": None}

    def test_a_result_retains_at_most_the_row_limit_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Script({"s1": [StatementResult(columns=["a"], rows=_rows(1_000))],
                            "s2": [], "s3": []}, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)

        answer = query.execute()

        assert answer["results"][0]["at_row_limit"] is True
        assert answer["results"][0]["paging"]["total_row_count"] == 1_000
        assert answer["row_limit"] == 1_000

    def test_the_first_failing_statement_stops_the_script_and_keeps_what_ran(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Script({"s1": [StatementResult(columns=["a"], rows=_rows(2))],
                            "s2": DatabaseError("refused"), "s3": []}, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)

        answer = query.execute()

        assert [asked[2] for asked in executor.asked] == ["s1", "s2"]
        assert answer["results"][0]["rows"] == _rows(2)
        assert answer["results"][1]["error"] == "Statement 2 of 3 failed: refused"
        assert answer["output"]["text"].splitlines()[-1] == "Statement 2 of 3 failed: refused"

    def test_text_that_does_not_parse_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        refusal = DatabaseError("The SQL could not be parsed; nothing was executed.")
        executor = _Script({}, refusal)
        query = self._query(monkeypatch, executor)

        with pytest.raises(DatabaseError) as raised:
            query.execute()

        assert raised.value is refusal and executor.asked == []

    def test_sorting_or_filtering_retained_results_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = _Script({"s1": [], "s2": [], "s3": []}, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)
        query.execute()

        query.update(order_by=[{"column": "a", "direction": "asc"}])
        with pytest.raises(DatabaseError, match="sorting and filtering"):
            query.execute()
        assert len(executor.asked) == 3

    @pytest.mark.parametrize("change", [{"statement": "s1; s2; s3"}, {"connection": None}])
    def test_replacing_the_statement_or_connection_releases_what_was_retained(
        self, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
    ) -> None:
        executor = _Script({"s1": [], "s2": [], "s3": []}, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)
        query.execute()

        query.update(**change)
        if change.get("connection", True) is None:
            query.update(connection=_Connection())
        query.execute()

        assert len(executor.asked) == 6

    @pytest.mark.parametrize("change", [{"current_page": 1}, {"current_result": 1}])
    def test_a_page_with_nothing_retained_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
    ) -> None:
        # A host that keeps no query between commands (the Loader) asks a fresh one.
        executor = _Script({"s1": [], "s2": [], "s3": []}, ["s1", "s2", "s3"])
        query = self._query(monkeypatch, executor)
        query.update(**change)

        with pytest.raises(DatabaseError, match="no longer held"):
            query.execute()
        assert executor.asked == []

    @pytest.mark.parametrize("statements", [None, ["only one"]])
    def test_one_statement_or_a_provider_that_divides_nothing_pages_as_before(
        self, monkeypatch: pytest.MonkeyPatch, statements: list[str] | None,
    ) -> None:
        declined = UnsupportedDatabaseCapabilityError("cannot page")
        executor = _Executor(raises=declined, statements=statements)
        monkeypatch.setattr(module, "_adapter", lambda _adapter: executor)

        with pytest.raises(UnsupportedDatabaseCapabilityError):
            DatabaseQuery("CALL p()", connection=_Connection()).execute()
        assert [asked[0] for asked in executor.asked] == ["execute_page"]

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


# -- linting the live query's statement (#1252) -------------------------------------


class TestItsLint:
    """The query offers a lint of its current statement, delegated to the artifact engine."""

    @staticmethod
    def _engine(monkeypatch: pytest.MonkeyPatch, findings: list[dict[str, Any]]) -> list[tuple]:
        import rey_lib.artifacts as artifacts

        calls: list[tuple] = []
        monkeypatch.setattr(artifacts, "artifact_config_from_ctx", lambda ctx: {"sql": "cfg"})
        monkeypatch.setattr(artifacts, "process_artifact",
                            lambda sql, kind, config: calls.append(("format", sql, kind, config))
                            or sql.upper())
        monkeypatch.setattr(artifacts, "lint_artifact",
                            lambda sql, kind, config: calls.append(("lint", sql, kind, config))
                            or findings)
        return calls

    def test_it_formats_through_the_engine_and_reports_against_the_formatted_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = self._engine(monkeypatch, [{
            "message": "Found unparsable section", "severity": "error", "start_line": 2,
            "start_column": 3, "end_line": 2, "end_column": 8, "code": "PRS",
            "source": "sqlfluff",
        }])
        query = DatabaseQuery("select a from t")

        answered = query.lint(object())

        assert calls == [("format", "select a from t", "sql", {"sql": "cfg"}),
                         ("lint", "SELECT A FROM T", "sql", {"sql": "cfg"})]
        assert answered == {"formattedSql": "SELECT A FROM T", "diagnostics": [{
            "message": "Found unparsable section", "severity": "error", "startLine": 2,
            "startColumn": 3, "endLine": 2, "endColumn": 8, "code": "PRS",
            "source": "sqlfluff",
        }]}

    def test_it_changes_nothing_on_the_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._engine(monkeypatch, [])
        query = DatabaseQuery("select a from t")

        query.lint(object())

        assert query.statement == "select a from t"

    def test_an_empty_statement_has_nothing_to_lint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._engine(monkeypatch, [])

        assert DatabaseQuery("  ").lint(object()) == {"diagnostics": []}
        assert calls == []

    def test_an_engine_refusal_reaches_the_caller_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import rey_lib.artifacts as artifacts
        from rey_lib.artifacts import ArtifactProcessingError

        self._engine(monkeypatch, [])

        def refuse(sql: str, kind: str, config: Any) -> str:
            raise ArtifactProcessingError("engine refused")

        monkeypatch.setattr(artifacts, "process_artifact", refuse)
        with pytest.raises(ArtifactProcessingError, match="engine refused"):
            DatabaseQuery("select 1").lint(object())
