"""RunLog owns writing a run's records; log_run_record is its shim.

Introducing the owner deliberately changes nothing. Path resolution,
destination selection, enrichment, stamping and the fail-safe all behave
exactly as they did, because the sequencing model underneath is not yet proven
safe to change -- see test_run_log_writer_concurrency.

These assert the ownership, not new behaviour: that the object exists, that the
old entry point delegates to it, that one RunLog serves a run, and that no
semantics moved with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.logs import log_run_record
from rey_lib.logs.run_log import RunLog, run_log_for
from rey_lib.run.identity import establish_run_identity


def _ctx(tmp_path: Path, run_store: str = "jsonl") -> SimpleNamespace:
    """A launched context writing to tmp_path."""
    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.run.jsonl"),
        owner_app_name="rey_loader",
        app_name="rey_loader",
        logging=SimpleNamespace(run_store=run_store, db_connection="control"),
    )
    establish_run_identity(ctx)
    return ctx


def _rows(ctx: Any) -> list[dict]:
    return [json.loads(line)
            for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class TestOneRunLogPerRun:
    """The object is made once and reused."""

    def test_the_same_run_yields_the_same_object(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        assert run_log_for(ctx) is run_log_for(ctx)

    def test_different_runs_get_different_objects(self, tmp_path: Path) -> None:
        first, second = _ctx(tmp_path), _ctx(tmp_path)

        assert run_log_for(first) is not run_log_for(second)

    def test_a_context_that_cannot_be_cached_still_gets_one(self) -> None:
        """Writing a record never mutated the context; caching is not the contract."""
        bare = object()

        assert isinstance(run_log_for(bare), RunLog)

    def test_nothing_is_opened_until_the_first_write(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        run_log = run_log_for(ctx)

        assert run_log.path is None
        assert not list(tmp_path.glob("*.jsonl"))


class TestTheShimDelegates:
    """log_run_record keeps its name and hands the work over."""

    def test_log_run_record_routes_through_the_object(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        with patch.object(RunLog, "append", return_value=7) as append:
            result = log_run_record(run_log, "ROW_COUNT", count_name="loaded", count=1)

        assert result == 7
        assert append.call_args.args[0] == "ROW_COUNT"
        assert append.call_args.kwargs["count_name"] == "loaded"

    def test_the_shim_and_the_method_write_the_same_record(self, tmp_path: Path) -> None:
        via_shim = _ctx(tmp_path)
        log_run_record(via_shim, "ROW_COUNT", count_name="a", count=1)

        via_object = _ctx(tmp_path / "other")
        (tmp_path / "other").mkdir()
        run_log_for(via_object).append("ROW_COUNT", count_name="a", count=1)

        shim_row, object_row = _rows(via_shim)[0], _rows(via_object)[0]
        ignored = {"run_id", "run_timestamp", "timestamp", "run_started_at"}
        assert ({k: v for k, v in shim_row.items() if k not in ignored}
                == {k: v for k, v in object_row.items() if k not in ignored})


class TestNoSemanticsMoved:
    """What the object must not have changed."""

    def test_destinations_come_from_the_run_store(self, tmp_path: Path) -> None:
        assert run_log_for(_ctx(tmp_path, "jsonl")).destinations() == (True, False)
        assert run_log_for(_ctx(tmp_path, "db")).destinations() == (False, True)
        assert run_log_for(_ctx(tmp_path, "both")).destinations() == (True, True)

    def test_the_sequence_still_advances_per_record(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        run_log = run_log_for(ctx)

        ids = [run_log.append("ROW_COUNT", count_name=f"n{i}", count=i) for i in range(4)]

        assert ids == [1, 2, 3, 4]

    def test_a_failed_write_never_raises(self, tmp_path: Path) -> None:
        """Logging must not mask execution -- unchanged by the move."""
        ctx = _ctx(tmp_path)

        with patch("rey_lib.files.primitive_file_io.append_jsonl",
                   side_effect=OSError("disk gone")):
            assert run_log_for(ctx).append("ROW_COUNT", count_name="a", count=1) is None

    def test_a_failed_write_does_not_advance_the_sequence(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        run_log = run_log_for(ctx)
        run_log.append("ROW_COUNT", count_name="first", count=1)

        with patch("rey_lib.files.primitive_file_io.append_jsonl",
                   side_effect=OSError("disk gone")):
            run_log.append("ROW_COUNT", count_name="lost", count=2)

        assert run_log.append("ROW_COUNT", count_name="next", count=3) == 2
