"""Deterministic RUN_SUMMARY common-field builder.

Builds the execution-neutral *common* fields of the canonical RUN_SUMMARY from a
completed run log's structured records — deterministic Python only, no LLM and no
fabrication. Execution-specific facts (``execution_details``) are contributed by the
execution layer and merged by the summary framework (see :mod:`rey_lib.logs.summary`);
this module never inspects workflow or pipeline internals
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_STEP_END = "STEP_END"
_STEP_FAILURE = "STEP_FAILURE"
_RUN_COMPLETE = "RUN_COMPLETE"

# STEP_END / outcome status vocabularies are normalized here so app, workflow, and
# pipeline logs (which use slightly different words) roll up into one common count.
_SUCCEEDED = frozenset({"success", "succeeded", "ok"})
_FAILED = frozenset({"failed", "failure", "error"})
_SKIPPED = frozenset({"skipped", "skip"})


def _rtype(record: dict[str, Any]) -> str:
    """Return a record's upper-cased record type."""
    return str(record.get("record_type") or "").upper()


def _status(record: dict[str, Any]) -> str:
    """Return a record's lower-cased status."""
    return str(record.get("status") or "").strip().lower()


def build_common_run_summary(
    sections: dict[str, Any],
    identity: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the common (execution-neutral) RUN_SUMMARY fields from log evidence.

    Parameters
    ----------
    sections : dict[str, Any]
        The ``read_run_log_sections`` projection (execution/files/results groups).
    identity : dict[str, Any]
        The ``_run_log_identity`` projection (ids, status, counts, timestamps).
    records : list[dict[str, Any]]
        All parsed run-log records (for step/terminal derivation).

    Returns
    -------
    dict[str, Any]
        The common summary fields. Missing evidence yields zero/empty/None per the
        existing schema behavior — never invented values.
    """
    execution_records = sections.get("execution", {}).get("records", [])
    steps = _step_counts(execution_records)
    return {
        "run_id": identity["run_id"],
        "run_timestamp": identity["run_timestamp"],
        "execution_kind": _execution_kind(identity),
        "app": identity["app"],
        "workflow": identity["workflow"],
        "pipeline": identity["pipeline"],
        "status": identity["status"],
        "started_at": identity["run_started_at"],
        "ended_at": identity["run_completed_at"],
        "elapsed_ms": _elapsed_ms(identity["run_started_at"], identity["run_completed_at"]),
        "steps_total": steps["total"],
        "steps_succeeded": steps["succeeded"],
        "steps_failed": steps["failed"],
        "steps_skipped": steps["skipped"],
        "warning_count": identity["warning_count"],
        "error_count": identity["error_count"],
        "artifact_count": identity["artifact_count"],
        "file_operation_count": identity["file_operation_count"],
        "failed_step_ids": _failed_step_ids(execution_records),
        "terminal_outcome": _terminal_outcome(records),
    }


def _execution_kind(identity: dict[str, Any]) -> str:
    """Classify the run as pipeline, workflow, or app from the log identity only."""
    if identity.get("pipeline"):
        return "pipeline"
    if identity.get("workflow"):
        return "workflow"
    return "app"


def _step_counts(execution_records: list[dict[str, Any]]) -> dict[str, int]:
    """Count STEP_END outcomes by normalized status (succeeded/failed/skipped)."""
    total = succeeded = failed = skipped = 0
    for record in execution_records:
        if _rtype(record) != _STEP_END:
            continue
        total += 1
        status = _status(record)
        if status in _SUCCEEDED:
            succeeded += 1
        elif status in _FAILED:
            failed += 1
        elif status in _SKIPPED:
            skipped += 1
    return {"total": total, "succeeded": succeeded, "failed": failed, "skipped": skipped}


def _failed_step_ids(execution_records: list[dict[str, Any]]) -> list[str]:
    """Return failed step identifiers from STEP_FAILURE evidence, in first-seen order."""
    ids: list[str] = []
    seen: set[str] = set()
    for record in execution_records:
        if _rtype(record) != _STEP_FAILURE:
            continue
        step_id = str(record.get("failed_step_id") or record.get("failed_step_name") or "")
        if step_id and step_id not in seen:
            seen.add(step_id)
            ids.append(step_id)
    return ids


def _terminal_outcome(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the terminal RUN_COMPLETE outcome (status + failure linkage), from the log."""
    complete = next(
        (r for r in reversed(records) if _rtype(r) == _RUN_COMPLETE),
        {},
    )
    outcome: dict[str, Any] = {"status": str(complete.get("status") or "")}
    for key in ("failure_record_id", "failure_message"):
        value = complete.get(key)
        if value:
            outcome[key] = str(value)
    return outcome


def _elapsed_ms(started_at: str, ended_at: str) -> int | None:
    """Return elapsed milliseconds between two ISO timestamps, or None if unknown."""
    start = _parse_iso(started_at)
    end = _parse_iso(ended_at)
    if start is None or end is None:
        return None
    delta_ms = int((end - start).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None when absent or malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
