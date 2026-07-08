"""Tests for shared run-log completeness validation."""

from __future__ import annotations

from rey_lib.logs import validate_run_log_completeness


def test_sparse_failed_run_is_reported_incomplete() -> None:
    """Sparse failed logs report missing failure and step-scoped file evidence."""
    records = [
        {"record_type": "RUN_START", "run_id": "r1", "run_timestamp": "20260708_000000"},
        {
            "record_type": "STEP_START",
            "run_id": "r1",
            "step_id": "prepare_trade_files",
            "step_name": "Prepare trade files",
        },
        {
            "record_type": "FILE_OPERATION",
            "run_id": "r1",
            "operation": "write",
            "target_path": "ctx.json",
            "step_id": "",
            "step_name": "",
        },
        {
            "record_type": "STEP_END",
            "run_id": "r1",
            "step_id": "prepare_trade_files",
            "step_name": "Prepare trade files",
            "status": "success",
        },
        {"record_type": "RUN_COMPLETE", "run_id": "r1", "status": "failed"},
    ]

    report = validate_run_log_completeness(records)

    assert report["valid"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "failed_run_missing_failure_evidence" in codes
    assert "step_file_operation_missing_step_id" in codes


def test_failed_run_with_referenced_error_evidence_is_valid() -> None:
    """Failed RUN_COMPLETE is complete when it references structured evidence."""
    records = [
        {"record_type": "RUN_START", "run_id": "r1", "run_timestamp": "20260708_000000"},
        {"record_type": "ERROR", "run_id": "r1", "error_id": "err-1", "message": "failed"},
        {
            "record_type": "RUN_COMPLETE",
            "run_id": "r1",
            "status": "failed",
            "failure_record_id": "err-1",
            "failed_step_id": "load",
            "failed_step_name": "Load",
            "failure_message": "failed",
        },
    ]

    report = validate_run_log_completeness(records)

    assert report == {"valid": True, "issue_count": 0, "issues": []}
