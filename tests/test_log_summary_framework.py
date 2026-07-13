"""Behavior tests for explicit RESULTS_SUMMARY creation in the run JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.logs import create_results_summary


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ctx(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_log_path=str(path), run_id="r1", run_timestamp="20260711_120000",
        owner_app_name="demo_app",
    )


def _completed(status: str = "success") -> list[dict]:
    return [
        {
            "record_type": "RUN_START", "record_group": "execution",
            "run_id": "r1", "run_timestamp": "20260711_120000",
            "run_started_at": "2026-07-11T12:00:00+00:00", "app": "demo_app",
        },
        {
            "record_type": "STEP_START", "record_group": "execution",
            "run_id": "r1", "step_id": "one", "step_name": "one",
            "step_sequence": 1,
        },
        {
            "record_type": "STEP_END", "record_group": "execution",
            "run_id": "r1", "step_id": "one", "step_name": "one",
            "status": status, "duration_ms": 1200,
        },
        {
            "record_type": "RUN_COMPLETE", "record_group": "execution",
            "run_id": "r1", "status": status,
            "timestamp": "2026-07-11T12:00:03+00:00",
        },
    ]


def test_results_summary_is_appended_to_completed_run_log(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write(log, _completed())
    result = create_results_summary(_ctx(log))

    records = _records(log)
    assert result["action"] == "created"
    assert records[-1]["record_type"] == "RESULTS_SUMMARY"
    assert records[-1]["status"] == "success"
    assert records[-1]["run_id"] == "r1"


def test_results_summary_requires_terminal_run(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write(log, _completed()[:-1])
    result = create_results_summary(_ctx(log))
    assert result["action"] is None
    assert result["skipped"] == ["no_terminal_record"]
    assert not any(record["record_type"] == "RESULTS_SUMMARY" for record in _records(log))


def test_results_summary_is_idempotent(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write(log, _completed())
    first = create_results_summary(_ctx(log))
    second = create_results_summary(_ctx(log))
    summaries = [record for record in _records(log) if record["record_type"] == "RESULTS_SUMMARY"]
    assert first["action"] == "created"
    assert second["action"] == "existing"
    assert len(summaries) == 1


def test_replace_existing_keeps_one_terminal_summary(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write(log, _completed())
    create_results_summary(_ctx(log))
    result = create_results_summary(
        _ctx(log), execution_details={"kind": "workflow", "workflow": {"mode": "full"}},
        replace_existing=True,
    )
    records = _records(log)
    assert result["action"] == "replaced"
    assert records[-1]["record_type"] == "RESULTS_SUMMARY"
    assert sum(record["record_type"] == "RESULTS_SUMMARY" for record in records) == 1


def test_failed_summary_preserves_failure_evidence(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    records = _completed("failed")
    records.insert(-1, {
        "record_type": "ERROR", "record_group": "execution", "run_id": "r1",
        "error_id": "error-1", "error_message": "boom",
    })
    records.insert(-1, {
        "record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
        "failure_record_id": "error-1", "failed_step_id": "one",
        "failed_step_name": "one", "failure_message": "boom",
    })
    records[-1].update({
        "failure_record_id": "error-1", "failed_step_id": "one",
        "failed_step_name": "one", "failure_message": "boom",
    })
    _write(log, records)
    create_results_summary(_ctx(log))
    summary = _records(log)[-1]
    assert summary["record_type"] == "RESULTS_SUMMARY"
    assert summary["status"] == "failed"
    assert "error-1" in summary["diagnostics"]["failure_record_ids"]


def test_explicit_log_path_works_without_context(tmp_path: Path) -> None:
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write(log, _completed())
    result = create_results_summary(log_path=log)
    assert result["action"] == "created"
    assert _records(log)[-1]["record_type"] == "RESULTS_SUMMARY"
