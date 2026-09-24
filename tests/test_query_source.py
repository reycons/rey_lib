"""A query on a connection, read as the source of a load.

    QuerySource -> DataTransform / IdentityTransform -> DatabaseObjectIdentity

The database family could be a load's TARGET and never its source, so every
load had to begin at a file. What is asserted here is the source object and
the unbounded read underneath it -- not the load, which still reaches for a
path at its boundary and cannot take this source until that is settled.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.data.errors import DataStructureError
from rey_lib.db.query_source import QuerySource


class _Adapter:
    """Records what the source asked for, and answers it.

    Substituted rather than mocked: the source is constructed WITH its
    adapter, so what it asks the database is observable without patching
    anything.
    """

    def __init__(
        self,
        columns: list[str],
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.columns = columns
        self.rows = rows if rows is not None else []
        self.asked: list[int | None] = []

    def query_rows(
        self, conn: Any, sql_text: str, *, limit: int | None = 1_000,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self.asked.append(limit)
        self.statement = sql_text
        if limit is None:
            return self.columns, list(self.rows)
        return self.columns, list(self.rows[:limit])


def _source(adapter: _Adapter, statement: str = "SELECT a, b FROM t") -> QuerySource:
    return QuerySource(object(), statement, adapter=adapter)


class TestWhatItReads:
    """The two readings, and what each of them costs."""

    def test_the_structure_is_the_queries_columns_in_order(self) -> None:
        adapter = _Adapter(["a", "b"], [{"a": 1, "b": "x"}])

        assert _source(adapter).source_structure() == ["a", "b"]

    def test_the_structure_costs_one_row(self) -> None:
        """A database describes a result by running it; this does not read it.

        Asserted on the bound asked for, because the cost is the point: the
        whole result is not materialised to learn its shape.
        """
        adapter = _Adapter(["a"], [{"a": 1}, {"a": 2}, {"a": 3}])

        _source(adapter).source_structure()

        assert adapter.asked == [1]

    def test_reading_is_unbounded(self) -> None:
        """THE DEFECT THIS EXISTS FOR.

        ``query_rows`` defaults to 1,000 rows -- a reader's preview. A load
        reading its source through that bound loses every row past it and says
        nothing, so the source asks for no bound at all.
        """
        adapter = _Adapter(["a"], [{"a": n} for n in range(2_500)])

        rows = _source(adapter).read()

        assert adapter.asked == [None]
        assert len(rows) == 2_500

    def test_it_reads_the_statement_it_was_given(self) -> None:
        """Held as written. Nothing here parses or rewrites it."""
        adapter = _Adapter(["a"])

        _source(adapter, "SELECT a FROM t WHERE x = 'y: z'").read()

        assert adapter.statement == "SELECT a FROM t WHERE x = 'y: z'"


class TestWhatItRefuses:
    """The two questions ``validate`` answers, and which argument selects."""

    def test_columns_matching_the_destination_pass(self) -> None:
        adapter = _Adapter(["a", "b"])

        _source(adapter).validate(["a", "b"])

    def test_a_different_order_is_refused(self) -> None:
        """ORDER, not membership.

        The insert is built from the column order, so a set comparison would
        pass a result that writes each value into the wrong column -- which is
        the failure that produces a loaded table nobody can see is wrong.
        """
        adapter = _Adapter(["b", "a"])

        with pytest.raises(DataStructureError):
            _source(adapter).validate(["a", "b"])

    def test_a_missing_column_is_refused(self) -> None:
        adapter = _Adapter(["a"])

        with pytest.raises(DataStructureError):
            _source(adapter).validate(["a", "b"])

    def test_the_refusal_is_named_for_the_run_log(self) -> None:
        """What the run log records, and it is not a file's name."""
        adapter = _Adapter(["b"])

        with pytest.raises(DataStructureError) as raised:
            _source(adapter).validate(["a"])

        assert raised.value.validation_name == "load_structure"

    def test_no_destination_asks_only_whether_it_has_columns(self) -> None:
        """INTERNAL CONSISTENCY. A result set has nothing to contradict."""
        adapter = _Adapter(["a", "b"])

        _source(adapter).validate(None)

    def test_a_statement_returning_no_columns_is_refused(self) -> None:
        adapter = _Adapter([])

        with pytest.raises(DataStructureError):
            _source(adapter).validate(None)

    def test_an_empty_destination_is_not_an_absent_one(self) -> None:
        """``None`` is not ``[]``: an empty list is a table with no columns,
        which nothing matches, and conflating them passes a real result
        against nothing."""
        adapter = _Adapter(["a"])

        with pytest.raises(DataStructureError):
            _source(adapter).validate([])


class TestReadValidated:
    """Validate, then read -- this source's own order."""

    def test_it_validates_before_it_reads(self) -> None:
        """A result whose shape is wrong is refused before it is fetched.

        Asserted on the bound: only the one-row structural read happened, so
        nothing asked for the whole result.
        """
        adapter = _Adapter(["b"], [{"b": 1}])

        with pytest.raises(DataStructureError):
            _source(adapter).read_validated(["a"])

        assert adapter.asked == [1]

    def test_it_returns_the_rows_when_the_shape_is_right(self) -> None:
        adapter = _Adapter(["a"], [{"a": 1}, {"a": 2}])

        rows = _source(adapter).read_validated(["a"])

        assert rows == [{"a": 1}, {"a": 2}]
        assert adapter.asked == [1, None]


class TestWhatItIsNot:
    """It is a data object whose primitive is a query, not a file."""

    def test_it_answers_nothing_file_shaped(self) -> None:
        """No path, no format token, no record shape.

        A source that carried them would be inventing a file to satisfy a
        contract the load does not ask of it.
        """
        source = _source(_Adapter(["a"]))

        for absent in ("path", "file_type", "record_shape", "encoding"):
            assert not hasattr(source, absent), absent

    def test_it_does_not_claim_to_declare_its_structure(self) -> None:
        """WHICH IS WHAT KEEPS IT OFF THE NON-ROW PATH.

        ``_non_row_execution_possible`` reads this through ``getattr`` with a
        default of False, and the path it gates hands the destination's
        provider a FILE to read. A source that answered True here would pass
        that gate and reach a call that needs a path it does not have.
        """
        assert not getattr(_source(_Adapter(["a"])), "declares_structure", False)

    def test_it_names_itself_by_its_statement(self) -> None:
        """A failure has to be readable, and a query has no file name."""
        source = _source(_Adapter(["a"]), "SELECT a FROM t")

        assert "SELECT a FROM t" in repr(source)

    def test_a_long_statement_is_shortened_for_a_message(self) -> None:
        source = _source(_Adapter(["a"]), "SELECT " + ", ".join("abcdefgh") * 20)

        assert len(repr(source)) < 100


class TestAgainstARealEngine:
    """The same source, over a real driver rather than a substitute.

    The adapter's generic DBAPI path is what DuckDB reads through, so the
    unbounded read has to be proved against a real cursor: a double that
    answers ``fetchall`` proves the branch was taken, not that a driver
    returns what the branch assumes.
    """

    @pytest.fixture()
    def engine(self):
        """One in-memory DuckDB, closed by its own context manager.

        Entered through ``with`` rather than ``__enter__()``: the context
        manager closes the connection when it is collected, so a caller that
        keeps only the handle gets one that is already shut.
        """
        from rey_lib.db.duckdb_utils import open_memory_connection

        with open_memory_connection() as conn:
            yield conn

    @staticmethod
    def _source(conn, rows: int) -> QuerySource:
        from rey_lib.db.db_adapter import DBAdapter

        conn.execute("CREATE OR REPLACE TABLE records (id INTEGER, name VARCHAR)")
        conn.execute(
            "INSERT INTO records "
            f"SELECT i, 'name-' || i FROM range({rows}) AS t(i)"
        )
        return QuerySource(
            conn,
            "SELECT id, name FROM records ORDER BY id",
            adapter=DBAdapter(),
        )

    def test_the_structure_is_read_from_the_engine(self, engine) -> None:
        assert self._source(engine, 3).source_structure() == ["id", "name"]

    def test_every_row_arrives_past_the_preview_bound(self, engine) -> None:
        """2,500 rows through a default that stops at 1,000.

        The bound is what makes this worth asserting against a real engine:
        a source read through the preview default would return exactly 1,000
        here and report no error at all.
        """
        rows = self._source(engine, 2_500).read()

        assert len(rows) == 2_500
        assert rows[0] == {"id": 0, "name": "name-0"}
        assert rows[-1] == {"id": 2_499, "name": "name-2499"}

    def test_a_result_matching_a_destination_is_read_whole(self, engine) -> None:
        rows = self._source(engine, 1_200).read_validated(["id", "name"])

        assert len(rows) == 1_200

    def test_a_result_that_does_not_match_is_refused_by_the_engines_own_columns(
        self, engine,
    ) -> None:
        with pytest.raises(DataStructureError):
            self._source(engine, 2).read_validated(["id", "name", "missing"])
