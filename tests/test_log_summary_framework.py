"""Tests for the run-results finalization framework and RESULTS_SUMMARY builder.

Increment 2: schema boundary, .results.json file output, migration from RUN_SUMMARY,
idempotency/determinism, and successful/failed-run behavior
(SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.logs import finalize_run_log


def _write_log(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _ctx(path: Path) -> SimpleNamespace:
    return SimpleNamespace(run_log_path=str(path), run_id="r1",
                           run_timestamp="20260711_120000", owner_app_name="demo_app")


def _results_path(log: Path) -> Path:
    return log.parent / (log.stem + ".results.json")


def _completed_records(status: str = "success") -> list[dict]:
    return [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_120000", "run_started_at": "2026-07-11T12:00:00+00:00",
         "pipeline": "daily", "app": "pipeline_coordinator"},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "one", "step_name": "one", "step_sequence": 1},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "one", "status": "success", "duration_ms": 1200},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": status, "timestamp": "2026-07-11T12:00:03+00:00"},
    ]


def _doc(log: Path) -> dict:
    return json.loads(_results_path(log).read_text(encoding="utf-8"))


def test_results_summary_success_run(tmp_path: Path) -> None:
    """A successful run writes a RESULTS_SUMMARY .results.json with full accounting."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    result = finalize_run_log(_ctx(log))
    assert result["results_written"] is True
    assert result["results_path"] == str(_results_path(log))
    doc = _doc(log)
    assert doc["record_type"] == "RESULTS_SUMMARY" and doc["record_schema_version"] == 1
    assert doc["status"] == "success" and doc["execution"]["outcome"] == "success"
    assert doc["run"]["steps_total"] == 1 and doc["run"]["steps_succeeded"] == 1
    assert doc["run"]["duration_ms"] == 3000
    assert [s["step_id"] for s in doc["step_results"]] == ["one"]
    # A successful run needs no failure fields populated.
    assert doc["execution"]["failed_step_ids"] == []
    assert doc["diagnostics"]["full_error_output"] == ""


def test_results_summary_is_a_projection_not_a_jsonl_record(tmp_path: Path) -> None:
    """RESULTS_SUMMARY is a separate .results.json file, not appended to the JSONL."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    original = log.read_text if log.exists() else None
    _write_log(log, _completed_records())
    before_types = [json.loads(l)["record_type"] for l in log.read_text().splitlines() if l.strip()]
    finalize_run_log(_ctx(log))
    after_types = [json.loads(l)["record_type"] for l in log.read_text().splitlines() if l.strip()]
    # The execution JSONL is unchanged; no RESULTS_SUMMARY / RUN_SUMMARY record added.
    assert after_types == before_types
    assert "RESULTS_SUMMARY" not in after_types and "RUN_SUMMARY" not in after_types
    assert _results_path(log).exists()


def test_results_summary_requires_terminal_run(tmp_path: Path) -> None:
    """An incomplete log (no RUN_COMPLETE) is not finalized."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records()[:-1])
    result = finalize_run_log(_ctx(log))
    assert result["results_written"] is False
    assert result["skipped"] == ["no_terminal_record"]
    assert not _results_path(log).exists()


def test_results_summary_is_deterministic(tmp_path: Path) -> None:
    """Identical JSONL input produces identical content except the summary timestamp."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    finalize_run_log(_ctx(log))
    first = _doc(log)
    finalize_run_log(_ctx(log))
    second = _doc(log)
    first.pop("timestamp"), second.pop("timestamp")
    assert first == second


def test_results_summary_failed_run(tmp_path: Path) -> None:
    """A failed run records outcome, failed step, and full error output."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "a", "step_name": "a", "step_sequence": 1},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "a", "status": "success", "duration_ms": 100},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "b", "step_name": "b", "step_sequence": 2},
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "error_message": "boom", "stderr_summary": "Traceback: boom line 1"},
        {"record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
         "failed_step_id": "b", "failure_record_id": "f1"},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "b", "status": "failed", "duration_ms": 200},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "failed_step_id": "b", "failed_step_name": "b",
         "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    assert doc["status"] == "failed"
    assert doc["execution"]["outcome"] == "partial_failure"
    assert doc["execution"]["partial_success"] is True
    assert doc["execution"]["failed_step_ids"] == ["b"]
    assert doc["diagnostics"]["failed_step_id"] == "b"
    assert doc["diagnostics"]["failure_record_ids"] == ["f1"]
    assert "Traceback: boom line 1" in doc["diagnostics"]["full_error_output"]


def test_results_summary_preserves_full_error_output_verbatim(tmp_path: Path) -> None:
    """Large multi-line error output is preserved byte-for-byte (no sanitize/truncate)."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    big = "\n".join(f"2026-07-11 09:49:{i:02d} ERROR line {i} password=hunter2" for i in range(60))
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "full_error_output": big},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    # Verbatim: every line preserved, not sanitized (this increment adds no redaction).
    assert doc["diagnostics"]["full_error_output"] == big
    assert doc["diagnostics"]["error_output_truncated"] is False


def test_results_summary_marks_upstream_truncation_honestly(tmp_path: Path) -> None:
    """When a source record was already truncated, the summary reports it, not completeness."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "record_id": "e1", "stderr_summary": "...cut", "output_truncated": True},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    assert d["error_output_truncated"] is True
    assert d["truncated_source_record_ids"] == ["e1"]


def test_results_summary_step_results_enriched_by_execution_details(tmp_path: Path) -> None:
    """execution_details enriches step_results with app/exit_code without fabrication."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    details = {"kind": "pipeline", "pipeline": {"mode": "full", "aborted": False,
               "invoked_apps": ["rey_loader"],
               "steps": [{"name": "one", "app": "rey_loader", "status": "success",
                          "exit_code": 0, "finalizer": False}]}}
    finalize_run_log(_ctx(log), execution_details=details)
    step = _doc(log)["step_results"][0]
    assert step["app"] == "rey_loader" and step["exit_code"] == 0
    assert step["step_sequence"] == 1 and step["duration_ms"] == 1200
