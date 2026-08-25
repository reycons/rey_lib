"""RunLog owns run logging: its state, its transitions, and its writing.

The test that stood here covered a compatibility shim and a locator that took
``ctx``. Both are gone, so this covers the owner instead: it holds what a record
needs, its own methods change what changes during a run, and nothing is read
from an application context.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rey_lib.logs.run_log import RunLog
from tests.conftest import make_db_run_log


def _rows(run_log: RunLog) -> list[dict]:
    return [json.loads(line)
            for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class TestItTakesNoContext:
    """The property the whole migration exists for."""

    def test_the_module_never_reads_a_context(self) -> None:
        import ast

        import rey_lib.logs.run_log as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        reads = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and n.id == "ctx"]
        assert reads == []

    def test_a_record_is_built_from_owned_state(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path, app="rey_loader", workflow="transform")

        run_log.append("ROW_COUNT", count_name="loaded", count=7)

        row = _rows(run_log)[0]
        assert row["app"] == "rey_loader"
        assert row["workflow_name"] == "transform"
        assert row["run_id"] == run_log.run_id


class TestItOwnsTheTransitions:
    """State that changes during a run changes through RunLog."""

    def test_binding_a_workflow_changes_later_records(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)
        run_log.append("ROW_COUNT", count_name="before", count=1)

        run_log.bind_workflow("transform_load")
        run_log.append("ROW_COUNT", count_name="after", count=2)

        before, after = _rows(run_log)
        assert "workflow_name" not in before
        assert after["workflow_name"] == "transform_load"

    def test_binding_a_pipeline_changes_later_records(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)
        run_log.bind_pipeline("daily")

        run_log.append("ROW_COUNT", count_name="a", count=1)

        assert _rows(run_log)[0]["pipeline_name"] == "daily"

    def test_lineage_is_bound_and_stamped(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)
        run_log.bind_lineage(parent_run_id="R-parent", subject_type="workflow")

        run_log.append("ROW_COUNT", count_name="a", count=1)

        row = _rows(run_log)[0]
        assert row["parent_run_id"] == "R-parent"
        assert row["subject_type"] == "workflow"

    def test_a_semantic_base_sets_the_nesting_level(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)

        assert run_log.set_nest_level("app") == 3
        assert run_log.nest_level() == 3

    def test_enter_and_exit_move_within_the_scope(self, tmp_path: Path) -> None:
        """A base sets the floor at base + 1, so a return holds there.

        Relative nesting cannot escape into or below its own base; only
        set_nest_level establishes a new one.
        """
        run_log = make_db_run_log(tmp_path)
        run_log.set_nest_level("workflow")   # 4, floor 5

        assert run_log.enter() == 5
        assert run_log.exit() == 5

    def test_exit_never_rises_above_the_established_minimum(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)
        run_log.set_nest_level("pipeline")

        for _ in range(5):
            run_log.exit()

        assert run_log.nest_level() >= 1


class TestSequencing:
    """Unchanged by the ownership move."""

    def test_the_sequence_advances_per_record(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)

        ids = [run_log.append("ROW_COUNT", count_name=f"n{i}", count=i)
               for i in range(4)]

        assert ids == [1, 2, 3, 4]

    def test_a_failed_write_never_raises(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)

        with patch("rey_lib.files.primitive_file_io.append_jsonl",
                   side_effect=OSError("disk gone")):
            assert run_log.append("ROW_COUNT", count_name="a", count=1) is None

class TestDestination:
    """RunLog decides where a record goes."""

    def test_jsonl_writes_only_the_file(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path, destination="jsonl")

        assert (run_log.writes_jsonl, run_log.writes_db) == (True, False)

    def test_db_writes_only_the_control_database(self, tmp_path: Path) -> None:
        class FakeControl:
            def __init__(self) -> None:
                self.written: list[dict] = []

            def write_run_log_record(self, **values):
                self.written.append(values)

        control = FakeControl()
        run_log = make_db_run_log(tmp_path, destination="db", control=control)

        run_log.append("ROW_COUNT", count_name="a", count=1)

        assert [row["record_type"] for row in control.written] == ["ROW_COUNT"]
        # The envelope is passed by name; what the caller supplied for this
        # record type goes to that type's own jsonb column, selected by
        # record_type. There is no generic `fields` bucket to hide a stamped
        # field in, and every other payload column stays null.
        payloads = control.written[0]["payloads"]
        assert payloads["row_count"] == {"count_name": "a", "count": 1}
        assert payloads["sql_execution"] is None
        # The database mints the identity, so the writer sends the parent it
        # is under -- null at the root -- and receives the row's id back.
        assert control.written[0]["parent_run_log_id"] is None
        assert not list(Path(tmp_path).glob("*.jsonl"))

    def test_a_missing_control_is_a_write_fault_not_a_crash(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path, destination="db", control=None)

        assert run_log.append("ROW_COUNT", count_name="a", count=1) is None


class TestLifecycle:
    """Closed by runtime collection, once."""

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        run_log = make_db_run_log(tmp_path)

        run_log.close()
        run_log.close()

        assert run_log.is_closed is True
