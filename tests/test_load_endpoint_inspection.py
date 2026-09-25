"""What the two ends of a load are made of, asked without running it.

A surface setting up a load wants to show where the values will go before
anything is written. The risk in answering that is a SECOND ANSWER: a
calculation that works out "what columns will this load see" beside the one
the load acts on, free to disagree, with the disagreement surfacing as a load
that does something other than what the screen showed.

So what is asserted here is mostly NEGATIVE -- that inspection builds the same
objects the load builds, asks them the same questions, and performs no load.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.load import load_operation
from rey_lib.load.load_operation import inspect_load_endpoints


@pytest.fixture()
def engine():
    """One in-memory DuckDB holding a source relation and a destination."""
    from rey_lib.db.duckdb_utils import open_memory_connection

    with open_memory_connection() as conn:
        conn.execute("CREATE TABLE orders (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO orders VALUES (1, 'x')")
        yield conn


@pytest.fixture()
def named(engine, monkeypatch):
    """Every configured connection name answers with the one engine."""
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(handle=lambda: engine),
    )
    return SimpleNamespace(log_depth=0)


class _Table:
    """A write adapter answering for one destination table."""

    def __init__(self, columns: list[str] | None) -> None:
        self.columns = columns

    def table_exists(self, *_a, **_k) -> bool:
        return self.columns is not None

    def get_table_columns(self, *_a, **_k) -> list[str]:
        return list(self.columns or [])

    def bulk_insert(self, *_a, **_k) -> int:
        raise AssertionError("inspection inserted rows")

    def create_staging_table_if_not_exists(self, *_a, **_k) -> bool:
        raise AssertionError("inspection created a destination")


class TestASourceAnswersForItself:

    def test_a_query_names_its_own_columns_in_order(self, named) -> None:
        found = inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
        )

        assert found.source == ("a", "b")

    def test_the_order_is_the_statements_own(self, named) -> None:
        """Ordered, because everything downstream of it is.

        The insert is built from this order and a header is written in it; a
        set answer would show a correspondence that is not the one the load
        will perform.
        """
        found = inspect_load_endpoints(
            named, statement="SELECT b, a FROM orders", source_connection="w",
        )

        assert found.source == ("b", "a")

    def test_a_file_names_its_header(self, tmp_path) -> None:
        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n", encoding="utf-8")

        found = inspect_load_endpoints(SimpleNamespace(), file=str(given))

        assert found.source == ("a", "b")

    def test_an_unnamed_source_is_empty_rather_than_a_failure(self) -> None:
        """A load being set up is half-named most of the time."""
        assert inspect_load_endpoints(SimpleNamespace()) == load_operation.LoadEndpoints()

    def test_a_file_whose_format_is_unknown_is_refused_as_the_load_would(
        self, tmp_path
    ) -> None:
        """An unreadable end IS an error, unlike an unnamed one."""
        with pytest.raises(ValueError):
            inspect_load_endpoints(SimpleNamespace(), file=str(tmp_path / "in.zzz"))


class TestADestinationHasTwoAnswers:
    """What it HAS, or what it WOULD get. Which applies is its own property."""

    def test_an_existing_table_answers_with_its_own_columns(
        self, named, monkeypatch
    ) -> None:
        """Those are what the insert must match, so those are what is shown."""
        monkeypatch.setattr(
            load_operation, "_write_adapter", _Table(["id", "label"]),
        )

        found = inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            destination="landing.records", connection="reporting",
        )

        assert found.source == ("a", "b")
        assert found.destination == ("id", "label")

    def test_a_table_that_does_not_exist_answers_with_what_would_be_created(
        self, named, monkeypatch
    ) -> None:
        """ABSENT IS NOT EMPTY.

        None means the table is not there; [] would mean a table with no
        columns. A table this load would create is not a destination the
        system is ignorant of, and answering with nothing would claim it was.
        """
        monkeypatch.setattr(load_operation, "_write_adapter", _Table(None))

        found = inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            destination="landing.records", connection="reporting",
        )

        assert found.destination == ("a", "b")

    def test_a_file_destination_answers_with_what_would_be_written(
        self, named, tmp_path
    ) -> None:
        """A file has no physical structure until it is written.

        It is still not unknown: the write takes its header from the records,
        and the records are what the transform produces from the source. So a
        blank destination here would be the screen claiming ignorance the
        load does not have.
        """
        found = inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            out_file=str(tmp_path / "rows.csv"),
        )

        assert found.destination == ("a", "b")

    def test_a_file_to_a_file_answers_the_same_way(self, tmp_path) -> None:
        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n", encoding="utf-8")

        found = inspect_load_endpoints(
            SimpleNamespace(), file=str(given),
            out_file=str(tmp_path / "out.jsonl"),
        )

        assert found.source == ("a", "b")
        assert found.destination == ("a", "b")

    def test_an_out_file_naming_no_format_is_refused(self, named, tmp_path) -> None:
        """Previewing a load that cannot run would be worse than refusing."""
        with pytest.raises(ValueError):
            inspect_load_endpoints(
                named, statement="SELECT a FROM orders", source_connection="w",
                out_file=str(tmp_path / "rows.zzz"),
            )

    def test_an_unnamed_destination_is_empty(self, named) -> None:
        found = inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
        )

        assert found.destination == ()

    def test_a_table_with_no_connection_is_not_reached_for(self, named) -> None:
        """Half a destination is still an unnamed one, not a failure."""
        found = inspect_load_endpoints(
            named, statement="SELECT a FROM orders", source_connection="w",
            destination="landing.records",
        )

        assert found.destination == ()


class TestItRunsNoLoad:
    """The negative half, and the reason this lives beside the load."""

    def test_nothing_is_inserted_or_created(self, named, monkeypatch) -> None:
        """The adapter fails the test if either is attempted."""
        monkeypatch.setattr(load_operation, "_write_adapter", _Table(["a", "b"]))

        inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            destination="landing.records", connection="reporting",
        )

    def test_no_file_is_written(self, named, tmp_path) -> None:
        out = tmp_path / "rows.csv"

        inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            out_file=str(out),
        )

        assert not out.exists()

    def test_it_does_not_go_through_the_boundary(self, named, monkeypatch) -> None:
        """Inspection is not a load with the write removed."""
        monkeypatch.setattr(
            load_operation, "_load_one_file",
            lambda *_a, **_k: pytest.fail("inspection crossed the boundary"),
        )

        inspect_load_endpoints(
            named, statement="SELECT a FROM orders", source_connection="w",
        )


class TestItAsksTheSameObjectsTheLoadDoes:
    """ONE ANSWER, which is the whole reason this is here and not in a surface."""

    def test_the_source_is_asked_through_its_own_structure_contract(
        self, named, monkeypatch
    ) -> None:
        """`source_structure()` and not a probe written for the screen.

        A second way of asking a query what it returns would be a second
        answer about the same statement.
        """
        from rey_lib.db.query_source import QuerySource

        asked: list[str] = []
        original = QuerySource.source_structure
        monkeypatch.setattr(
            QuerySource, "source_structure",
            lambda self: (asked.append("asked"), original(self))[1],
        )

        inspect_load_endpoints(
            named, statement="SELECT a FROM orders", source_connection="w",
        )

        assert asked == ["asked"]

    def test_the_produced_columns_come_from_the_transform(
        self, named, monkeypatch
    ) -> None:
        """`columns_for_names`, which states that rule once.

        What a load sees is what the TRANSFORM produces from what the source
        declares. Today's identity transform makes those the same, so reading
        them off the source would be right by coincidence and wrong the
        moment a transform declares columns.
        """
        from rey_lib.data.data_transform import IdentityTransform

        seen: list[list[str]] = []
        original = IdentityTransform.columns_for_names
        monkeypatch.setattr(
            IdentityTransform, "columns_for_names",
            lambda self, actual: (seen.append(list(actual)), original(self, actual))[1],
        )

        inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
        )

        assert seen == [["a", "b"]]

    def test_the_destination_is_asked_through_the_loaders_own_question(
        self, named, monkeypatch
    ) -> None:
        """`destination_columns`, the same call the boundary makes."""
        asked: list[Any] = []
        monkeypatch.setattr(load_operation, "_write_adapter", _Table(["a"]))
        original = load_operation._DataLoader.destination_columns
        monkeypatch.setattr(
            load_operation._DataLoader, "destination_columns",
            lambda self, conn, target: (
                asked.append(target), original(self, conn, target),
            )[1],
        )

        inspect_load_endpoints(
            named, statement="SELECT a FROM orders", source_connection="w",
            destination="landing.records", connection="reporting",
        )

        assert [one.name for one in asked] == ["records"]
        assert [one.schema for one in asked] == ["landing"]


class TestThePreview:
    """What this load would carry, shown without carrying it."""

    def test_it_shows_the_rows_and_the_produced_columns(self, named) -> None:
        from rey_lib.load.load_operation import preview_load

        found = preview_load(
            named, statement="SELECT a, b FROM orders", source_connection="w",
        )

        assert found.columns == ("a", "b")
        assert found.rows == ({"a": 1, "b": "x"},)
        assert found.truncated is False

    def test_it_is_bounded(self, engine, named) -> None:
        """A preview must not cost what a load costs."""
        from rey_lib.load.load_operation import preview_load

        engine.execute(
            "INSERT INTO orders SELECT i, 'y' FROM range(2, 200) t(i)"
        )

        found = preview_load(
            named, statement="SELECT a, b FROM orders ORDER BY a",
            source_connection="w", limit=5,
        )

        assert len(found.rows) == 5
        assert found.truncated is True

    def test_a_source_holding_exactly_the_limit_is_not_truncated(
        self, engine, named
    ) -> None:
        """THE REASON ONE MORE ROW IS ASKED FOR.

        A count equal to the limit says nothing on its own: a source holding
        exactly that many looks identical to one holding more.
        """
        from rey_lib.load.load_operation import preview_load

        found = preview_load(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            limit=1,
        )

        assert len(found.rows) == 1
        assert found.truncated is False

    def test_it_reads_through_sample_and_never_through_read(
        self, named, monkeypatch
    ) -> None:
        """`read()` is unbounded, deliberately. A preview must not reach it."""
        from rey_lib.db.query_source import QuerySource
        from rey_lib.load.load_operation import preview_load

        monkeypatch.setattr(
            QuerySource, "read",
            lambda self: pytest.fail("the preview read the whole source"),
        )

        preview_load(
            named, statement="SELECT a FROM orders", source_connection="w",
        )

    def test_a_file_source_is_previewed_too(self, tmp_path) -> None:
        from rey_lib.load.load_operation import preview_load

        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")

        found = preview_load(SimpleNamespace(), file=str(given), limit=2)

        assert found.columns == ("a", "b")
        assert found.rows == ({"a": "1", "b": "x"}, {"a": "2", "b": "y"})
        assert found.truncated is True

    def test_an_empty_source_still_names_its_columns(self, named) -> None:
        """Columns come from the STRUCTURE, not from the rows.

        A preview showing nothing at all would read as a broken query rather
        than an empty one.
        """
        from rey_lib.load.load_operation import preview_load

        found = preview_load(
            named, statement="SELECT a, b FROM orders WHERE a < 0",
            source_connection="w",
        )

        assert found.columns == ("a", "b")
        assert found.rows == ()

    def test_no_source_is_an_empty_preview_rather_than_a_failure(self) -> None:
        from rey_lib.load.load_operation import LoadPreview, preview_load

        assert preview_load(SimpleNamespace()) == LoadPreview()

    def test_it_takes_no_destination_and_validates_against_none(self) -> None:
        """A preview is about what is LEAVING.

        Refusing a source that does not match its destination is the load's
        job; doing it here would stop a reader looking at the mismatch in
        order to fix it.
        """
        from inspect import signature

        from rey_lib.load.load_operation import preview_load

        taken = set(signature(preview_load).parameters)
        assert not taken & {"destination", "connection", "out_file"}

    def test_it_writes_nothing(self, named, tmp_path, monkeypatch) -> None:
        from rey_lib.load.load_operation import preview_load

        monkeypatch.setattr(
            load_operation, "_load_one_file",
            lambda *_a, **_k: pytest.fail("the preview crossed the boundary"),
        )

        preview_load(
            named, statement="SELECT a FROM orders", source_connection="w",
        )

        assert not list(tmp_path.iterdir())
