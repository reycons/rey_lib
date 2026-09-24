"""A load that begins at a database rather than a file.

    QuerySource -> IdentityTransform -> DatabaseObjectIdentity

THE ARCHITECTURAL PROOF, and deliberately nothing more. There is no public
entry point for this yet, no CLI and no surface: what is asserted is that a
source data object with NO FILE PRIMITIVE passes through the same
``_load_one_file`` every file load uses, and its rows reach the destination.

Both halves already existed and could not meet. ``QuerySource`` (824d33f) is
the database family's read side; the boundary (757915f) stopped flattening
its source into a path. This is the first load that needs both.

The source reads a REAL engine rather than a substitute. A double answering
``read_validated`` would prove the boundary calls it, which was never in
doubt; what is worth proving is that a statement's own result -- columns and
rows the database produced -- survives the whole operation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.data.errors import DataStructureError
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.query_source import QuerySource
from rey_lib.load import load_operation

#: Where these loads write. A table that already exists: creating one from a
#: non-row source is its own open question and is not this test's business.
_TARGET = DatabaseObjectIdentity(
    connection="warehouse", catalog="", schema="landing", name="records",
)


class _Destination:
    """The destination side, recording what the load hands it.

    Only the write half is a double. The READ half is a real DuckDB, because
    the point is that a real result set crosses the boundary.
    """

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.inserted: list[dict[str, Any]] = []
        self.insert_columns: list[str] = []

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native path. A statement is not a file a provider can read."""
        return False

    def table_exists(self, *_args, **_kwargs) -> bool:
        return True

    def get_table_columns(self, *_args, **_kwargs) -> list[str]:
        return list(self.columns)

    def bulk_insert(self, _conn, _schema, _table, rows, columns) -> int:
        self.inserted.extend(rows)
        self.insert_columns = list(columns)
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False

    def create_staging_table_if_not_exists(self, *_args, **_kwargs) -> bool:
        raise AssertionError("the destination exists; nothing may create it")


@pytest.fixture()
def engine():
    """One in-memory DuckDB holding the source relation."""
    from rey_lib.db.duckdb_utils import open_memory_connection

    with open_memory_connection() as conn:
        conn.execute("CREATE TABLE orders (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO orders VALUES (1, 'x'), (2, 'y'), (3, 'z')")
        yield conn


def _load(monkeypatch, run_log, source: Any, destination: _Destination) -> int:
    """Run one load through the shared boundary and return the rows loaded."""
    monkeypatch.setattr(load_operation, "_db_adapter", destination)
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )
    # Nothing is routed, and this proves the source is why rather than the
    # policy: a movement IS declared, and a source with no path has nothing
    # to move.
    monkeypatch.setattr(
        load_operation, "execute_movements",
        lambda *_a, **_k: pytest.fail("a database source has nothing to route"),
    )

    return load_operation._load_one_file(
        source,
        # None, so the boundary builds the IdentityTransform itself -- the
        # same construction a direct file load goes through.
        None,
        _TARGET,
        ctx=SimpleNamespace(log_depth=0), run_log=run_log,
        transform_cfg=None,
        load_cfg=SimpleNamespace(
            name="a_database_load",
            load=SimpleNamespace(destination_table="landing.records"),
            movements=SimpleNamespace(success=["archive"], failure=["reject"]),
        ),
        paths=SimpleNamespace(),
    )


def _source(engine, statement: str = "SELECT a, b FROM orders ORDER BY a"):
    return QuerySource(engine, statement, adapter=DBAdapter())


class TestADatabaseSourceCrossesTheBoundary:
    """The blocker is gone, or it is not."""

    def test_its_rows_reach_the_destination(
        self, engine, monkeypatch, run_log
    ) -> None:
        """THE PROOF. A source with no file primitive loads.

        Before 757915f this raised on the boundary's first line, reaching
        neither the transform nor the target.
        """
        destination = _Destination(["a", "b"])

        loaded = _load(monkeypatch, run_log, _source(engine), destination)

        assert loaded == 3
        assert destination.inserted == [
            {"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"},
        ]

    def test_the_insert_is_built_from_the_querys_own_columns(
        self, engine, monkeypatch, run_log
    ) -> None:
        """In order, because that is what the insert is built from."""
        destination = _Destination(["a", "b"])

        _load(monkeypatch, run_log, _source(engine), destination)

        assert destination.insert_columns == ["a", "b"]

    def test_a_statement_selecting_fewer_columns_is_refused(
        self, engine, monkeypatch, run_log
    ) -> None:
        """The destination check runs for a database source exactly as it does
        for a file: nothing is inserted, and the refusal is the source's."""
        destination = _Destination(["a", "b"])

        loaded = _load(
            monkeypatch, run_log,
            _source(engine, "SELECT a FROM orders"), destination,
        )

        assert loaded == 0
        assert destination.inserted == []

    def test_an_empty_result_is_refused_as_an_empty_file_is(
        self, engine, monkeypatch, run_log
    ) -> None:
        destination = _Destination(["a", "b"])

        loaded = _load(
            monkeypatch, run_log,
            _source(engine, "SELECT a, b FROM orders WHERE a < 0"), destination,
        )

        assert loaded == 0
        assert destination.inserted == []

    def test_it_never_takes_the_file_native_path(
        self, engine, monkeypatch, run_log
    ) -> None:
        """``load_from_path`` needs a path, and this source has none.

        The gate refuses it before that call rather than raising inside it --
        which is the failure this would have been, had the source claimed to
        declare its structure.
        """
        destination = _Destination(["a", "b"])
        monkeypatch.setattr(
            load_operation._DataLoader, "load_from_path",
            lambda *_a, **_k: pytest.fail("a statement is not a path"),
        )

        assert _load(monkeypatch, run_log, _source(engine), destination) == 3


class TestWhatItRecords:
    """Evidence for a source that has no path to record."""

    def test_it_names_the_source_and_records_no_path(
        self, engine, monkeypatch, run_log
    ) -> None:
        """ABSENT, not empty.

        A blank ``path`` reads as a delivery whose location was lost; a
        database source never had one.
        """
        records: list[dict] = []
        monkeypatch.setattr(
            load_operation, "log_row_count",
            lambda _run_log, **kwargs: records.append(kwargs),
        )

        _load(monkeypatch, run_log, _source(engine), _Destination(["a", "b"]))

        assert "path" not in records[0]
        assert records[0]["subject"].startswith("QuerySource(")
        assert records[0]["count"] == 3
        assert records[0]["schema"] == "landing"
        assert records[0]["table"] == "records"
