"""
Tests for ambient run-log file-operation recording
(SGC_Rey_File_Utils_Ambient_Run_Log_File_Recording).

file_utils records a FILE_OPERATION to the bound run log for each file
operation, with no ctx threading, and does nothing when no run is bound.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.files.file_utils import (
    delete_file,
    move_file,
    read_bytes_file,
    read_text_file,
    write_file,
)
from rey_lib.logs import (
    bind_correlation,
    bind_run,
    bind_step,
    clear_correlation,
    clear_run,
    clear_step,
    current_run,
    record_file_operation,
)


def _ops(run_log: Path) -> list[dict]:
    """Return FILE_OPERATION records from a run log."""
    return [
        record
        for line in run_log.read_text(encoding="utf-8").splitlines() if line.strip()
        for record in [json.loads(line)]
        if record.get("record_type") == "FILE_OPERATION"
    ]


@pytest.fixture()
def bound_run(tmp_path: Path):
    """Bind a run log for the test and clear it afterwards."""
    run_log = tmp_path / "run_log.20260707_000000.jsonl"
    bind_run(run_log_path=str(run_log), run_id="r1", run_timestamp="20260707_000000")
    try:
        yield run_log
    finally:
        clear_run()


def test_write_records_file_operation(bound_run: Path, tmp_path: Path) -> None:
    """write_file records a FILE_OPERATION (write) to the bound run log."""
    write_file(tmp_path / "out.json", {"a": 1}, "JSON")
    ops = _ops(bound_run)
    assert any(o["operation"] == "write" and o["target_path"].endswith("out.json") for o in ops)


def test_move_records_file_operation(bound_run: Path, tmp_path: Path) -> None:
    """move_file records a FILE_OPERATION (move) with source and target."""
    src = tmp_path / "in.csv"
    src.write_text("x\n", encoding="utf-8")
    move_file(src, tmp_path / "done")
    op = next(o for o in _ops(bound_run) if o["operation"] == "move")
    assert op["source_path"].endswith("in.csv")
    assert op["target_path"].endswith("done/in.csv")


def test_file_operation_inherits_bound_step_context(bound_run: Path, tmp_path: Path) -> None:
    """Ambient FILE_OPERATION records inherit the separately bound step context."""
    bind_step(step_id="export_before", step_name="Export before", step_sequence=1)
    bind_correlation("corr-file-1")
    try:
        write_file(tmp_path / "ctx.json", {"ok": True}, "JSON")
    finally:
        clear_correlation()
        clear_step()

    op = next(o for o in _ops(bound_run) if o["operation"] == "write")
    assert op["step_id"] == "export_before"
    assert op["step_name"] == "Export before"
    assert op["step_sequence"] == 1
    assert op["correlation_id"] == "corr-file-1"


def test_read_records_file_operation(bound_run: Path, tmp_path: Path) -> None:
    """read helpers record a FILE_OPERATION (read)."""
    target = tmp_path / "r.txt"
    target.write_text("hello\n", encoding="utf-8")
    read_text_file(target)
    read_bytes_file(target)
    reads = [o for o in _ops(bound_run) if o["operation"] == "read"]
    assert len(reads) == 2


def test_delete_records_file_operation(bound_run: Path, tmp_path: Path) -> None:
    """delete_file records a FILE_OPERATION (delete)."""
    target = tmp_path / "gone.txt"
    target.write_text("x", encoding="utf-8")
    delete_file(target)
    assert any(o["operation"] == "delete" for o in _ops(bound_run))


def test_no_run_bound_records_nothing(tmp_path: Path) -> None:
    """With no run bound, file operations record nothing."""
    clear_run()
    assert current_run() is None
    write_file(tmp_path / "x.json", {"a": 1}, "JSON")
    move_file(_touch(tmp_path / "m.txt"), tmp_path / "dst")
    # No run log file is created by recording.
    assert not list(tmp_path.glob("run_log.*.jsonl"))


def test_bind_clear_current_run(tmp_path: Path) -> None:
    """bind_run/current_run/clear_run manage the ambient run."""
    clear_run()
    assert current_run() is None
    bind_run(run_log_path=str(tmp_path / "run_log.x.jsonl"), run_id="r9")
    assert current_run() == {"run_log_path": str(tmp_path / "run_log.x.jsonl"), "run_id": "r9"}
    # Binding without a durable path is a no-op (keeps the prior binding).
    bind_run(run_log_path="")
    assert current_run()["run_id"] == "r9"
    clear_run()
    assert current_run() is None


def test_recording_is_fail_safe(tmp_path: Path, monkeypatch) -> None:
    """A recording failure never raises into the file operation."""
    # Bind a run whose log dir cannot be created (a file blocks the path).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bind_run(run_log_path=str(blocker / "nested" / "run_log.jsonl"), run_id="r1")
    try:
        # The write itself must still succeed even though recording cannot.
        result = write_file(tmp_path / "safe.json", {"ok": True}, "JSON")
        assert result.exists()
        # And a direct record call is also swallowed.
        record_file_operation("write", target_path="/x")
    finally:
        clear_run()


def _touch(path: Path) -> Path:
    """Create an empty file and return its path."""
    path.write_text("", encoding="utf-8")
    return path
