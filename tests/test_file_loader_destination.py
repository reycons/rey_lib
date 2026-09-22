"""Whether the loader may create the table it loads into.

No database is opened. What is asserted is what the loader DECIDED -- which
adapter calls it made and which it did not -- because the whole of this row is
a policy: a destination that should already exist and does not is a different
thing from one the loader was asked to create.

The three paths, and nothing else:

    exists                    -> validate, load, and issue NO creation DDL
    absent + create false     -> ConfigError naming the table, file untouched
    absent + create true      -> create from the file's shape, then load

**Before this, creation was unreachable.** An absent table answered no columns,
every shape check failed against them, and the file was rejected as a header
mismatch against nothing -- so the unconditional create call had never created
anything. That is why absent-and-not-declared is the DEFAULT: it is what the
loader already did, said out loud.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import file_loader

_COLUMNS = ["a", "b"]


class _Adapter:
    """An adapter that records which contract calls the loader made."""

    def __init__(self, *, exists: bool) -> None:
        self._exists = exists
        self.created: list[tuple] = []
        self.inserted: list[tuple] = []
        self.described = 0
        self.existence_checks = 0

    def table_exists(self, _conn, _schema, _table) -> bool:
        self.existence_checks += 1
        return self._exists

    def get_table_columns(self, _conn, _schema, _table) -> list[str]:
        self.described += 1
        return list(_COLUMNS) if self._exists else []

    def create_staging_table_if_not_exists(self, _conn, schema, table, defs) -> bool:
        self.created.append((schema, table, defs))
        self._exists = True
        return True

    def bulk_insert(self, _conn, schema, table, rows, columns) -> int:
        self.inserted.append((schema, table, rows, columns))
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "source.csv"
    path.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    return path


def _jsonl(tmp_path: Path, records=({"a": 1, "b": "x"},)) -> Path:
    path = tmp_path / "source.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def _load(tmp_path, monkeypatch, run_log, adapter, *, declared=None,
          keyed=False, moved=None, records=({"a": 1, "b": "x"},)):
    """Run _load_one_file with a recording adapter and a declared setting."""
    monkeypatch.setattr(file_loader, "_db_adapter", adapter)
    monkeypatch.setattr(
        file_loader, "_execute_movements",
        lambda *a, **k: moved.append(a) if moved is not None else None,
    )

    load_block = SimpleNamespace(connection="c", destination_table="s.t")
    if declared is not None:
        load_block.create_destination_table = declared

    return file_loader._load_one_file(
        SimpleNamespace(log_depth=0), run_log,
        SimpleNamespace(commit=lambda: None, rollback=lambda: None),
        _jsonl(tmp_path, records) if keyed else _csv(tmp_path),
        SimpleNamespace(file_type="JSONL" if keyed else "CSV", encoding="utf-8"),
        SimpleNamespace(name="my_load",
                        load=load_block,
                        movements=SimpleNamespace(failure=[], success=[])),
        SimpleNamespace(), "schema", "table",
    )


class TestTheDestinationIsThere:
    """The ordinary path, and the one existing configs are on."""

    def test_it_loads_and_issues_no_creation_ddl(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """THE POLICY, not merely its usual outcome.

        A test that only checked the rows landed would pass with the create
        call still unconditional. IF NOT EXISTS is not a defence: a table
        dropped between the check and that call would be recreated against a
        config that said not to.
        """
        adapter = _Adapter(exists=True)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter)

        assert loaded == 2
        assert adapter.created == []            # no DDL at all
        assert len(adapter.inserted) == 1

    def test_the_destination_is_inspected_ONCE_per_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Two decisions, one question.

        Whether to validate the file against the destination, and whether to
        create it, are both answered by whether it exists. Splitting the load
        across two objects made it easy for each to ask separately, which is
        a catalog query per file for an answer already in hand.
        """
        adapter = _Adapter(exists=True)

        _load(tmp_path, monkeypatch, run_log, adapter)

        assert adapter.existence_checks == 1

    def test_the_default_is_require_so_an_absent_key_changes_nothing(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """No config declares this key today, and none needs to.

        Absent means false because that is what the loader already did -- an
        absent destination was rejected long before creation was reached.
        """
        adapter = _Adapter(exists=True)

        assert _load(tmp_path, monkeypatch, run_log, adapter, declared=None) == 2
        assert adapter.created == []

    def test_a_header_mismatch_still_rejects_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Validation is gated on existence, not removed.

        The destination is there, so its columns are authoritative and a file
        that disagrees is still the file's fault.
        """
        adapter = _Adapter(exists=True)
        moved: list = []
        path = tmp_path / "source.csv"
        path.write_text("a,WRONG\n1,x\n", encoding="utf-8")
        monkeypatch.setattr(file_loader, "_db_adapter", adapter)
        monkeypatch.setattr(file_loader, "_execute_movements",
                            lambda *a, **k: moved.append(a))

        loaded = file_loader._load_one_file(
            SimpleNamespace(log_depth=0), run_log,
            SimpleNamespace(commit=lambda: None, rollback=lambda: None),
            path,
            SimpleNamespace(file_type="CSV", encoding="utf-8"),
            SimpleNamespace(name="my_load",
                            load=SimpleNamespace(destination_table="s.t"),
                            movements=SimpleNamespace(failure=[], success=[])),
            SimpleNamespace(), "schema", "table",
        )

        assert loaded == 0
        assert adapter.inserted == []
        assert moved                            # went down movements.failure


class TestTheDestinationIsAbsentAndNotDeclared:
    """A configuration fault, and treated as one."""

    def test_it_raises_and_names_the_table(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The message has to say WHICH table, and how to change the answer.

        Before this it reported a header mismatch and logged an empty expected
        column list -- a true statement about the wrong thing.
        """
        adapter = _Adapter(exists=False)

        with pytest.raises(ConfigError) as raised:
            _load(tmp_path, monkeypatch, run_log, adapter, declared=False)

        message = str(raised.value)
        assert "schema.table" in message
        assert "my_load" in message
        assert "create_destination_table" in message

    def test_the_file_is_left_alone(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The half a pytest.raises alone would not catch.

        Every other rejection in _load_one_file runs movements.failure because
        the FILE was wrong. Here the file is fine: moving it to rejected_path
        would strand a good file for a fault it did not cause, and every later
        file would fail identically and follow it.
        """
        adapter = _Adapter(exists=False)
        moved: list = []

        with pytest.raises(ConfigError):
            _load(tmp_path, monkeypatch, run_log, adapter, declared=False,
                  moved=moved)

        assert moved == []
        assert (tmp_path / "source.csv").exists()

    def test_nothing_is_created_and_nothing_is_inserted(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """It refuses before it reads, so no work is done on the way out."""
        adapter = _Adapter(exists=False)

        with pytest.raises(ConfigError):
            _load(tmp_path, monkeypatch, run_log, adapter, declared=False)

        assert adapter.created == []
        assert adapter.inserted == []


class TestTheDestinationIsAbsentAndCreationIsDeclared:
    """The capability this row adds, and which was previously unreachable."""

    def test_the_table_is_created_then_loaded_in_one_run(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The acceptance on the row, in order: create, then insert."""
        adapter = _Adapter(exists=False)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter, declared=True)

        assert loaded == 2
        assert len(adapter.created) == 1
        assert len(adapter.inserted) == 1
        schema, table, defs = adapter.created[0]
        assert (schema, table) == ("schema", "table")
        assert [name for name, _type in defs] == _COLUMNS

    def test_the_shape_comes_from_the_file(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """There is nothing else it could come from, and that is the point.

        No destination columns exist to validate against, so the file defines
        the shape -- which is what creating from it means. The mechanism is
        the existing one (rows[0].keys() into _build_column_defs); this row
        deliberately does not touch schema authority.
        """
        adapter = _Adapter(exists=False)

        _load(tmp_path, monkeypatch, run_log, adapter, declared=True)

        _schema, _table, defs = adapter.created[0]
        _s, _t, _rows, columns = adapter.inserted[0]
        assert [name for name, _type in defs] == columns

    def test_an_INCONSISTENT_file_is_refused_BEFORE_the_table_is_created(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The gap the create path had from the day it was added.

        With no destination there was nothing to validate against, so nothing
        was validated AT ALL. An inconsistent file therefore had its table
        created from the first record's keys and then failed inside
        bulk_insert with a missing-column DatabaseError -- a database error
        for what is a file defect, raised after DDL had already run.

        The file is now asked whether it is coherent with ITSELF, which is a
        question that needs no destination.
        """
        adapter = _Adapter(exists=False)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       declared=True, keyed=True,
                       records=({"a": 1, "b": "x"}, {"a": 2, "c": "y"}))

        assert loaded == 0
        assert adapter.created == [], "the table was created for a bad file"
        assert adapter.inserted == []

    def test_that_refusal_is_a_FILE_fault_not_a_run_fault(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """So the batch continues and the file is routed.

        A malformed delivery is one bad file. It must not stop a run the way
        a missing destination does -- the next file may be perfectly good.
        """
        adapter = _Adapter(exists=False)
        moved: list = []

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       declared=True, keyed=True, moved=moved,
                       records=({"a": 1}, {"b": 2}))

        assert loaded == 0          # returned, not raised
        assert moved                # and routed down movements.failure

    def test_a_CONSISTENT_file_still_creates_and_loads(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The capability is unchanged for files that are actually fine.

        This is the regression guard: the new check must reject malformed
        files without rejecting the ordinary ones the create path exists for.
        """
        adapter = _Adapter(exists=False)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       declared=True, keyed=True,
                       records=({"a": 1, "b": "x"}, {"b": "y", "a": 2}))

        assert loaded == 2
        assert len(adapter.created) == 1

    def test_a_keyed_source_is_not_rejected_for_a_table_that_is_not_there(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The keyed rule is gated on existence too.

        set(record.keys()) == set(destination columns) compares against an
        empty set when the table is absent, so every record would fail. Both
        shape checks had to be gated, not just the header.
        """
        adapter = _Adapter(exists=False)

        loaded = _load(tmp_path, monkeypatch, run_log, adapter,
                       declared=True, keyed=True)

        assert loaded == 1
        assert len(adapter.created) == 1


class TestTheSettingIsRead:
    """It governs DDL, so a value that is not a boolean is refused."""

    @pytest.mark.parametrize("declared", ["false", "no", 0, 1, "true"])
    def test_a_non_boolean_is_refused_rather_than_read_for_truthiness(
        self, tmp_path: Path, monkeypatch, run_log, declared
    ) -> None:
        """``"false"`` is a TRUTHY string in Python.

        Read for truthiness it would create a table for a config that spelled
        out the opposite, which is the exact failure the setting exists to
        prevent.
        """
        adapter = _Adapter(exists=True)

        with pytest.raises(ConfigError) as raised:
            _load(tmp_path, monkeypatch, run_log, adapter, declared=declared)

        assert "create_destination_table" in str(raised.value)

    def test_existence_is_asked_not_inferred_from_the_column_list(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """bool(get_table_columns(...)) must not be the test.

        get_table_columns answers about COLUMNS. Inferring existence from an
        empty answer conflates "there is no such table" with "I was given no
        columns", and this row is introducing table existence as policy.
        Here the table exists and reports no columns: inference would call it
        absent and refuse, asking gets it right.
        """
        adapter = _Adapter(exists=True)
        adapter.get_table_columns = lambda *_a, **_k: []

        loaded = _load(tmp_path, monkeypatch, run_log, adapter, declared=False)

        assert loaded == 0          # rejected on shape, NOT on existence
        assert adapter.created == []
