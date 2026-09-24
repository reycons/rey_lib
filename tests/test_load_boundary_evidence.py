"""What the load boundary records about the source it was given.

The boundary opened with ``file_path = source.path`` and named every log line,
run-log row and rejection from it. That demanded a file primitive from an
object whose family may not have one, and it is what
``a_load_is_between_two_data_objects_not_a_file_and_a_table`` already forbade:
*"No path, schema or table. Those are the endpoints, and the endpoints are the
objects."*

**Nothing in this suite read the boundary's ``loaded_rows`` record**, so the
``path`` field could have vanished from every file load and every test would
still have passed. A green suite was therefore not evidence for that change,
which is the whole reason this module exists: it reads the recorded values
rather than the row count.

One of those values INTENTIONALLY CHANGED and is pinned here rather than left
to be discovered by whoever next reads a run log. ``subject`` was the file's
name and is now the source object's own representation, because the object
describes itself where evidence needs it and the boundary manufactures no
second identity for it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_file import data_file_for
from rey_lib.load import load_operation

_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="landing", name="records",
)


class _Adapter:
    """A destination that exists, takes rows, and offers no native path.

    ``supports_provider_capability`` answers rather than being absent: the
    loader asks every adapter this, and a double that omitted it would fail on
    the question instead of on what the test is about.
    """

    def supports_provider_capability(self, _conn, _capability) -> bool:
        return False

    def table_exists(self, *_args, **_kwargs) -> bool:
        return True

    def get_table_columns(self, *_args, **_kwargs) -> list[str]:
        return ["a", "b"]

    def bulk_insert(self, _conn, _schema, _table, rows, _columns) -> int:
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False


@pytest.fixture()
def recorded(tmp_path: Path, monkeypatch, run_log) -> dict:
    """Load one ordinary CSV through the boundary; return its ROW_COUNT row."""
    records: list[dict] = []

    monkeypatch.setattr(load_operation, "_db_adapter", _Adapter())
    monkeypatch.setattr(load_operation, "execute_movements",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(
        load_operation, "log_row_count",
        lambda _run_log, **kwargs: records.append(kwargs),
    )
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )

    source = tmp_path / "incoming.csv"
    source.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")

    loaded = load_operation._load_one_file(
        data_file_for(source, file_type="CSV", encoding="utf-8"),
        None,
        _TARGET,
        ctx=SimpleNamespace(log_depth=0), run_log=run_log,
        transform_cfg=None,
        load_cfg=SimpleNamespace(
            name="a_load",
            load=SimpleNamespace(destination_table="landing.records"),
            movements=None,
        ),
        paths=SimpleNamespace(),
    )

    assert loaded == 2, "the load itself must succeed for its record to mean anything"
    assert len(records) == 1
    return records[0]


class TestAFileLoadStillRecordsWhereItCameFrom:
    """A run log is read later, by someone asking why a delivery did not land."""

    def test_the_path_is_the_concrete_file(self, recorded: dict) -> None:
        """THE REGRESSION NOTHING ELSE WOULD CATCH.

        The boundary stopped taking a path for every load. This is what says
        the file family did not lose its path evidence in the process.
        """
        assert recorded["path"].endswith("incoming.csv")
        assert Path(recorded["path"]).is_absolute()

    def test_the_subject_is_the_source_objects_own_representation(
        self, recorded: dict
    ) -> None:
        """PINNED, because this text intentionally changed.

        It was ``incoming.csv`` -- the boundary naming the file from a path it
        had flattened out of the source. It is now what the object says it is.
        A reader of saved run logs sees a different value here from 2026-09-24,
        and that is a deliberate consequence of the source staying an object
        rather than becoming a name.
        """
        assert recorded["subject"] == "DelimitedHeaderFile('incoming.csv')"

    def test_the_destination_and_the_count_are_untouched(
        self, recorded: dict
    ) -> None:
        """Everything else about the record is exactly as it was."""
        assert recorded["count_name"] == "loaded_rows"
        assert recorded["count"] == 2
        assert recorded["schema"] == "landing"
        assert recorded["table"] == "records"
