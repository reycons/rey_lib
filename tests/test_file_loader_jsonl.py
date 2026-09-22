"""Loading a keyed source, where the column names are on every record.

A CSV names its columns once, in a header, so the loader can check the shape
before reading a row. JSONL names them on EVERY record, which moves both the
question and the moment: the check is per record, and it can only run once
the records exist.

The rule is EQUALITY, not coverage, and that is not a preference. Nothing
projects rows onto the destination: ``_load_one_file`` takes
``columns = list(rows[0].keys())`` and hands it to both the staging DDL and
``bulk_insert``. An extra key on the first record would BECOME a column. So a
superset is as wrong as a subset, and the two extra-key tests below are what
prove the rule is a property of the file rather than of whichever record
happens to be first.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files import file_loader
from rey_lib.files.file_utils import KEYED_FILE_TYPES, get_reader


def _ctx() -> SimpleNamespace:
    """The least a shared boundary needs: log_enter increments log_depth."""
    return SimpleNamespace(log_depth=0)


def _jsonl(tmp_path: Path, *records: dict) -> Path:
    """Write one JSON object per line, in the order given."""
    path = tmp_path / "source.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _load(tmp_path: Path, monkeypatch, run_log, *records: dict,
          columns=("a", "b")):
    """Run _load_one_file over a JSONL file, recording what reached the adapter.

    The adapter is a double: this asserts what the loader DECIDED, not what a
    database did with it.
    """
    seen: dict = {}

    def _bulk_insert(_conn, _schema, _table, rows, cols):
        seen["rows"] = rows
        seen["columns"] = cols
        return len(rows)

    monkeypatch.setattr(
        file_loader, "_db_adapter",
        SimpleNamespace(
            get_table_columns=lambda *_a, **_k: list(columns),
            create_staging_table_if_not_exists=lambda *_a, **_k: True,
            bulk_insert=_bulk_insert,
            is_truncation_error=lambda _exc: False,
        ),
    )
    monkeypatch.setattr(file_loader, "_execute_movements",
                        lambda *_a, **_k: None)

    loaded = file_loader._load_one_file(
        _ctx(), run_log,
        SimpleNamespace(commit=lambda: None, rollback=lambda: None),
        _jsonl(tmp_path, *records),
        SimpleNamespace(file_type="JSONL", encoding="utf-8"),
        SimpleNamespace(movements=SimpleNamespace(failure=[], success=[])),
        SimpleNamespace(), "schema", "table",
    )
    return loaded, seen


class TestTheKeyedSourceLoads:
    """The capability, and that it reuses the existing path."""

    def test_records_whose_keys_equal_the_destination_load(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The whole point: a JSONL file reaches bulk_insert."""
        loaded, seen = _load(tmp_path, monkeypatch, run_log,
                             {"a": 1, "b": "x"}, {"a": 2, "b": "y"})

        assert loaded == 2
        assert seen["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]

    def test_values_keep_their_json_types(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Nothing coerces them to strings, and nothing needs to.

        _build_column_defs and bulk_insert both take dict[str, Any]. A
        delimited reader has only text to offer; this format carries types,
        and discarding them here would be a loss the destination cannot undo.
        """
        _loaded, seen = _load(tmp_path, monkeypatch, run_log, {"a": 1, "b": "x"})

        assert seen["rows"][0]["a"] == 1
        assert not isinstance(seen["rows"][0]["a"], str)


class TestTheShapeRuleIsEquality:
    """Four cases. The last two are what a subset rule would get wrong."""

    def test_a_record_missing_a_destination_column_rejects_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Rejected, not filled with NULL.

        bulk_insert requires every row to carry every column, and this estate
        does not invent a value for an absent one.
        """
        loaded, seen = _load(tmp_path, monkeypatch, run_log,
                             {"a": 1, "b": "x"}, {"a": 2})

        assert loaded == 0
        assert "rows" not in seen          # never reached the adapter

    def test_an_extra_key_on_the_FIRST_record_rejects_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """A subset rule would accept this, and it would be wrong.

        columns is taken from rows[0].keys() with no projection, so the extra
        key would become a staging column and an insert column against a
        destination that has none. bulk_insert's "anything else is ignored"
        cannot discard it -- on this path it IS one of the columns.
        """
        loaded, seen = _load(tmp_path, monkeypatch, run_log,
                             {"a": 1, "b": "x", "c": "extra"}, {"a": 2, "b": "y"})

        assert loaded == 0
        assert "rows" not in seen

    def test_an_extra_key_on_a_LATER_record_also_rejects_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """So the outcome is a property of the FILE, not of record order.

        Here the extra key would be silently dropped by bulk_insert rather
        than becoming a column -- a different mechanism, the same verdict.
        Accepting this while rejecting the previous case would make the rule
        depend on which record happened to be written first.
        """
        loaded, seen = _load(tmp_path, monkeypatch, run_log,
                             {"a": 1, "b": "x"}, {"a": 2, "b": "y", "c": "extra"})

        assert loaded == 0
        assert "rows" not in seen

    def test_key_ORDER_does_not_matter(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The one deliberate departure from the CSV path, which compares order.

        A CSV header is an ordered artifact. A JSON object's key order is
        incidental, so rejecting a file for it would reject one differing
        only in how its producer serialised it.
        """
        loaded, _seen = _load(tmp_path, monkeypatch, run_log,
                              {"b": "x", "a": 1}, {"a": 2, "b": "y"})

        assert loaded == 2


class TestItReadsTheFileOnce:
    """The reason the check sits after the read rather than beside the header."""

    def test_the_records_are_parsed_exactly_once(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """A second parse would be invisible except here.

        _validate_load_header opens the file before the rows are read, which
        is cheap for a one-line header and is a whole extra parse for JSONL.
        Moving the keyed check back beside it would still produce correct
        results -- at twice the work on every load -- and nothing else in the
        suite would notice.
        """
        reads: list = []
        real_open = Path.open

        def _counting_open(self, mode="r", *args, **kwargs):
            # READS of the source only. write_text() opens it too, and the
            # run log is itself a .jsonl file -- counting either would make
            # this assert an arbitrary number instead of the one fact it is
            # about.
            if self.name == "source.jsonl" and "w" not in mode:
                reads.append(self.name)
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _counting_open)
        loaded, _seen = _load(tmp_path, monkeypatch, run_log, {"a": 1, "b": "x"})

        assert loaded == 1
        assert len(reads) == 1


class TestTheReaderDelegates:
    """One branch in the existing dispatch, not a second parser."""

    def test_get_reader_dispatches_every_keyed_type(self, tmp_path: Path) -> None:
        """The aliases are one set, shared with the loader.

        KEYED_FILE_TYPES decides both what get_reader dispatches and when the
        loader can check the shape, so the two cannot disagree about which
        formats carry their names per record.
        """
        path = tmp_path / "x.jsonl"
        path.write_text('{"a": 1}\n', encoding="utf-8")

        for file_type in KEYED_FILE_TYPES:
            assert list(get_reader(path, file_type=file_type)) == [{"a": 1}]
            assert list(get_reader(path, file_type=file_type.lower())) == [{"a": 1}]

    def test_an_unknown_type_is_still_refused_by_name(self, tmp_path: Path) -> None:
        """Adding a branch must not turn the dispatch into a fallback."""
        path = tmp_path / "x.jsonl"
        path.write_text('{"a": 1}\n', encoding="utf-8")

        with pytest.raises(ValueError) as raised:
            list(get_reader(path, file_type="NOT_A_FORMAT"))

        assert "NOT_A_FORMAT" in str(raised.value)
