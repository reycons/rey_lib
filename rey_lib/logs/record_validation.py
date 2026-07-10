"""Validation helpers for shared run-log records."""

from __future__ import annotations

from typing import Any


def _validate_run_record(record: dict[str, Any]) -> None:
    """Validate typed record invariants enforced by the shared logging layer."""
    if str(record.get("record_type") or "").upper() == "RUN_COMPLETE":
        status = str(record.get("status") or "").lower()
        if status == "failed":
            missing = [
                key for key in (
                    "failure_record_id",
                    "failed_step_id",
                    "failed_step_name",
                    "failure_message",
                )
                if not record.get(key)
            ]
            if missing:
                raise ValueError(
                    "RUN_COMPLETE status='failed' requires structured failure "
                    f"evidence fields: {', '.join(missing)}."
                )


def _validate_run_record_fields(record_type: str, fields: dict[str, Any]) -> None:
    """Validate invariants that must raise before fail-safe append handling."""
    if str(record_type or "").upper() != "RUN_COMPLETE":
        return
    status = str(fields.get("status") or "").lower()
    if status != "failed":
        return
    missing = [
        key for key in (
            "failure_record_id",
            "failed_step_id",
            "failed_step_name",
            "failure_message",
        )
        if not fields.get(key)
    ]
    if missing:
        raise ValueError(
            "RUN_COMPLETE status='failed' requires structured failure "
            f"evidence fields: {', '.join(missing)}."
        )


def validate_run_log_completeness(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return shared run-log completeness findings without mutating records.

    This validator is intentionally evidence-based. It reports sparse or
    contradictory lifecycle records so consumers can reject or flag incomplete
    runs without inventing missing facts.
    """
    issues: list[dict[str, Any]] = []
    record_types = {
        str(record.get("record_type") or "").upper()
        for record in records
    }

    if "RUN_START" not in record_types:
        issues.append({
            "code": "missing_run_start",
            "severity": "error",
            "message": "Run log has no RUN_START record.",
        })
    if "RUN_COMPLETE" not in record_types:
        issues.append({
            "code": "missing_run_complete",
            "severity": "error",
            "message": "Run log has no RUN_COMPLETE record.",
        })

    error_ids = {
        str(record.get("error_id") or "")
        for record in records
        if str(record.get("record_type") or "").upper() == "ERROR"
    }
    failure_ids = {
        str(record.get("failure_record_id") or "")
        for record in records
        if str(record.get("record_type") or "").upper() == "STEP_FAILURE"
    }

    active_step: dict[str, Any] | None = None
    for index, record in enumerate(records, start=1):
        record_type = str(record.get("record_type") or "").upper()
        if record_type == "STEP_START":
            active_step = record
        if record_type == "RUN_COMPLETE" and str(record.get("status") or "").lower() == "failed":
            evidence_id = str(record.get("failure_record_id") or "")
            has_failure_fields = all(
                record.get(key)
                for key in ("failed_step_id", "failed_step_name", "failure_message")
            )
            has_referenced_evidence = bool(
                evidence_id and (evidence_id in error_ids or evidence_id in failure_ids)
            )
            if not has_failure_fields or not has_referenced_evidence:
                issues.append({
                    "code": "failed_run_missing_failure_evidence",
                    "severity": "error",
                    "line": index,
                    "message": "RUN_COMPLETE status=failed lacks structured failure evidence.",
                })
        if record_type == "FILE_OPERATION" and _is_step_scoped_file_operation(record, active_step):
            if not record.get("step_id"):
                issues.append({
                    "code": "step_file_operation_missing_step_id",
                    "severity": "error",
                    "line": index,
                    "message": "Step-scoped FILE_OPERATION has a blank step_id.",
                })
            if not record.get("step_name"):
                issues.append({
                    "code": "step_file_operation_missing_step_name",
                    "severity": "warning",
                    "line": index,
                    "message": "Step-scoped FILE_OPERATION has a blank step_name.",
                })
        if record_type in ("STEP_END", "STEP_FAILURE"):
            active_step = None

    return {
        "valid": not any(issue["severity"] == "error" for issue in issues),
        "issue_count": len(issues),
        "issues": issues,
    }


def _is_step_scoped_file_operation(
    record: dict[str, Any],
    active_step: dict[str, Any] | None = None,
) -> bool:
    """Return whether a file operation appears to have happened inside a step."""
    if active_step is not None:
        return True
    return any(
        record.get(key)
        for key in ("step_id", "step_name", "step_sequence", "correlation_id")
    )
