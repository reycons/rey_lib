"""DatabaseQuery: execution mechanics only, in three modes (backlogs 730, 731).

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


def test_it_is_frozen_and_each_query_has_its_own_parameters() -> None:
    first, second = DatabaseQuery("SELECT 1"), DatabaseQuery("SELECT 1")

    assert first.params is not second.params
    with pytest.raises(AttributeError):
        first.statement = "SELECT 2"  # type: ignore[misc]


def test_it_offers_rows_run_and_page_only_and_holds_no_sql() -> None:
    public = {name for name in vars(DatabaseQuery) if not name.startswith("_")}
    code = Path(module.__file__).read_text(encoding="utf-8").split('"""', 2)[2]

    assert public - {"statement", "params"} == {"rows", "run", "page"}
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
