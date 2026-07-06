"""
Tests for the append-only typed run log
(SGC_Rey_Workflow_Pipeline_Automatic_Control_Batch_Logging).

Cover the centralized run-log record API in log_utils: run identity on every
record, execution vs run-result grouping, append-only accumulation, fail-closed
open without a durable log path, and fail-safe appends.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    log_artifact_reference,
    log_run_complete,
    log_run_start,
    log_run_summary,
    log_step_end,
    log_step_start,
    open_run_log,
)


def _ctx(tmp_path: Path) -> SimpleNamespace:
    """A context whose log directory is tmp_path (log_file established)."""
    return SimpleNamespace(
        log_file=str(tmp_path / "app.scan.jsonl"),
        owner_app_name="rey_loader",
        workflow_name="transform_load",
    )


def _read(path: Path) -> list[dict]:
    """Read all JSONL records from a run-log file."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_log_named_with_run_timestamp(tmp_path: Path) -> None:
    """The run log is run_log.<run_timestamp>.jsonl in the log directory."""
    ctx = _ctx(tmp_path)
    path = open_run_log(ctx)
    assert path.name == f"run_log.{ctx.run_timestamp}.jsonl"
    assert path.parent == tmp_path


def test_records_carry_run_id_and_group(tmp_path: Path) -> None:
    """Every record includes run_id; types are grouped execution vs run-result."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    log_step_start(ctx, "load_data", 1, step_type="loader")
    log_step_end(ctx, "load_data", "success")
    log_run_summary(ctx, {"steps": 1, "status": "success"})
    log_run_complete(ctx, "success")

    records = _read(Path(ctx.run_log_path))
    assert all(r["run_id"] == ctx.run_id for r in records)
    by_type = {r["record_type"]: r for r in records}
    assert by_type["RUN_START"]["record_group"] == "execution"
    assert by_type["STEP_END"]["status"] == "success"
    assert by_type["RUN_SUMMARY"]["record_group"] == "run_result"
    assert by_type["RUN_SUMMARY"]["summary"] == {"steps": 1, "status": "success"}


def test_append_only_accumulates(tmp_path: Path) -> None:
    """Records accumulate; the log is never rewritten."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    log_artifact_reference(ctx, str(tmp_path / "out.csv"), role="output")
    log_run_complete(ctx, "success")
    assert len(_read(Path(ctx.run_log_path))) == 3


def test_open_run_log_fails_closed_without_log_path() -> None:
    """Without a durable log path, opening the run log raises (fail closed)."""
    ctx = SimpleNamespace()
    with pytest.raises(ValueError):
        open_run_log(ctx)


def test_record_append_is_fail_safe(tmp_path: Path) -> None:
    """A record whose value is not JSON-serialisable is still written (default=str)."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx, weird=object())
    records = _read(Path(ctx.run_log_path))
    assert records[0]["record_type"] == "RUN_START"
