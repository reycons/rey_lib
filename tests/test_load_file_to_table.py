"""Load one named file into one named table, with no configuration.

The direct answer to the request that started this work. What matters is not
that it loads -- the pipeline beneath it was already proven -- but that it is
the SAME pipeline, constructed from arguments instead of YAML.

Three properties, each of which would be easy to lose:

    no synthetic config   nothing is manufactured to stand in for a definition
    no movements          a direct load leaves the caller's file alone
    one pipeline          direct and configured loads run the same sequence
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import load_operation
from rey_lib.load import configured_load


class _Adapter:
    """Records what the load decided, instead of reaching a database."""

    def __init__(self, *, exists: bool = False) -> None:
        self._exists = exists
        self.created: list[tuple] = []
        self.inserted: list[tuple] = []

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native execution path, so the materialised one runs.

        Answered rather than absent: the loader asks every adapter
        this, and a double that omitted it would fail on the question
        rather than on what the test is about.
        """
        return False

    def table_exists(self, _conn, _schema, _table) -> bool:
        return self._exists

    def get_table_columns(self, _conn, _schema, _table) -> list[str]:
        return ["a", "b"] if self._exists else []

    def create_staging_table_if_not_exists(self, _conn, schema, table, defs):
        self.created.append((schema, table, defs))
        self._exists = True
        return True

    def bulk_insert(self, _conn, schema, table, rows, columns) -> int:
        self.inserted.append((schema, table, rows, columns))
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False


def _jsonl(tmp_path: Path, *records: dict, name: str = "asset.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def _load(tmp_path, monkeypatch, run_log, adapter, source=None,
          destination="testing.asset", moved=None, **kwargs) -> int:
    monkeypatch.setattr(load_operation, "_write_adapter", adapter)
    # Resolved from the target now, so it is substituted where it is resolved.
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )
    monkeypatch.setattr(
        load_operation, "execute_movements",
        lambda *a, **k: moved.append(a) if moved is not None else None,
    )
    return load_operation.load_file_to_table(
        SimpleNamespace(log_depth=0), run_log,
        source if source is not None else _jsonl(tmp_path, {"a": 1, "b": "x"}),
        destination, "a_connection", **kwargs,
    )


class TestItLoadsWithNoConfiguration:
    """The outcome the whole sequence was building toward."""

    def test_a_file_reaches_a_table_it_was_only_NAMED(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        adapter = _Adapter(exists=True)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter)

        assert loaded == 1
        schema, table, _rows, columns = adapter.inserted[0]
        assert (schema, table) == ("testing", "asset")
        assert columns == ["a", "b"]

    def test_the_destination_may_be_created_from_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        adapter = _Adapter(exists=False)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       create_destination=True)

        assert loaded == 1
        assert adapter.created[0][1] == "asset"

    def test_an_absent_destination_is_refused_without_create(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """A run-level fault, and it escapes rather than returning 0."""
        adapter = _Adapter(exists=False)

        with pytest.raises(ConfigError) as raised:
            _load(tmp_path, monkeypatch, run_log, adapter)

        assert "testing.asset" in str(raised.value)

    def test_the_schema_is_INFERRED_since_nothing_declares_it(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """There is no configured column list, so the records decide.

        The other half of step 5's contract: configuration is authoritative
        where it exists, and here it does not exist at all.
        """
        adapter = _Adapter(exists=False)

        _load(tmp_path, monkeypatch, run_log, adapter, create_destination=True,
              source=_jsonl(tmp_path, {"x": 1, "y": 2}))

        _schema, _table, defs = adapter.created[0]
        assert [name for name, _type in defs] == ["x", "y"]


class TestItMovesNothing:
    """A direct load has no movement policy, which is not an empty one."""

    def test_a_successful_load_moves_nothing(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The caller named a path and expects the file left where it is.

        Nothing was picked up from an inbox, so there is no processed folder
        it belongs in and no archive to move it to.
        """
        moved: list = []

        _load(tmp_path, monkeypatch, run_log, _Adapter(exists=True), moved=moved)

        assert moved == []

    def test_a_REJECTED_file_is_not_moved_either(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The harder half. A configured load routes a bad file to
        rejected_path; a direct load has no such place, and inventing one
        would move a file the caller still owns.
        """
        moved: list = []
        inconsistent = _jsonl(tmp_path, {"a": 1, "b": "x"}, {"a": 2, "c": "y"})

        loaded = _load(tmp_path, monkeypatch, run_log, _Adapter(exists=False),
                       source=inconsistent, create_destination=True,
                       moved=moved)

        assert loaded == 0          # refused, as a malformed file
        assert moved == []          # and left alone


class TestTheFormatIsDecidedTheSameWay:
    """One rule, whether it came from a suffix or an argument."""

    def test_the_suffix_decides_when_nothing_is_declared(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        assert _load(tmp_path, monkeypatch, run_log, _Adapter(exists=True)) == 1

    def test_a_declared_file_type_wins_over_the_suffix(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Which is why the argument exists: a suffix naming no known format
        is refused, so without this some files could not be loaded at all."""
        misnamed = _jsonl(tmp_path, {"a": 1, "b": "x"}, name="delivery.dat")

        loaded = _load(tmp_path, monkeypatch, run_log, _Adapter(exists=True),
                       source=misnamed, file_type="JSONL")

        assert loaded == 1

    def test_a_suffix_naming_no_format_is_still_refused(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Refused by name rather than guessed at."""
        mystery = _jsonl(tmp_path, {"a": 1}, name="delivery.dat")

        with pytest.raises(ValueError) as raised:
            _load(tmp_path, monkeypatch, run_log, _Adapter(exists=True),
                  source=mystery)

        assert ".dat" in str(raised.value)

class TestEncodingIsALibraryParameter:
    """Not exposed on any CLI, but carried to the file when passed here.

    file_type is a CLI option because a suffix naming no format makes a load
    impossible; encoding has a working default and no such failure, so it
    stays a library concern.
    """

    def test_it_reaches_the_DATA_FILE(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Asserted at the DataFile, which is where the parameter is owned.

        Whether each format's reader then honours it is that reader's
        answer, and one of them does not -- see the delimited case below and
        `jsonl_read_ignores_the_declared_encoding` on the backlog.
        """
        seen: list[str] = []
        real = configured_load.data_file_for

        def _record(path, **kwargs):
            source = real(path, **kwargs)
            seen.append(source.encoding)
            return source

        monkeypatch.setattr(configured_load, "data_file_for", _record)

        _load(tmp_path, monkeypatch, run_log, _Adapter(exists=True),
              encoding="latin-1")

        assert seen == ["latin-1"]

    def test_a_DELIMITED_file_is_decoded_with_it(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """End to end, on a format whose reader takes the encoding.

        The default would raise UnicodeDecodeError on these bytes, so this
        passes only because the declared encoding reached the read.
        """
        source = tmp_path / "latin.csv"
        source.write_bytes("a,b\ncafé,1\n".encode("latin-1"))
        adapter = _Adapter(exists=True)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       source=source, encoding="latin-1")

        assert loaded == 1
        assert adapter.inserted[0][2][0]["a"] == "café"
