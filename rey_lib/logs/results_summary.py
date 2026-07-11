"""Deterministic RESULTS_SUMMARY builder.

Builds the canonical run results document as a deterministic projection of a single
completed run log (one pipeline JSONL + canonical run_id). It replaces the earlier
RUN_SUMMARY record: the framework writes this as a pretty-printed
``<name>.<run_timestamp>.results.json`` file (not a JSONL record). The execution
JSONL remains the authoritative event log; this is a projection of it.

Increment 2 establishes the schema boundary and the run / execution / step /
validation / warning / diagnostics sections. Full item and artifact correlation is a
follow-up increment; those sections are present with a stable shape so consumers and
the schema are fixed now (SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs.run_summary import (
    _elapsed_ms,
    _failed_step_ids,
    _rtype,
    _status,
    _step_counts,
)

RESULTS_SUMMARY_SCHEMA_VERSION = 1

_STEP_START = "STEP_START"
_STEP_END = "STEP_END"
_STEP_FAILURE = "STEP_FAILURE"
_RUN_COMPLETE = "RUN_COMPLETE"
_ERROR = "ERROR"
_WARNING = "WARNING"
_APP_EXECUTION = "APP_EXECUTION"
_VALIDATION_RESULT = "VALIDATION_RESULT"


def build_results_summary(
    sections: dict[str, Any],
    identity: dict[str, Any],
    records: list[dict[str, Any]],
    execution_details: dict[str, Any] | None,
    *,
    timestamp: str,
) -> dict[str, Any]:
    """Return the canonical RESULTS_SUMMARY document (deterministic; no LLM).

    Parameters
    ----------
    sections, identity, records
        The ``read_run_log_sections`` / ``_run_log_identity`` projections and raw
        records of one completed run log.
    execution_details
        Execution-layer facts (workflow/pipeline domain), used to enrich step_results.
    timestamp
        The results document creation time (the only non-deterministic field).
    """
    execution_records = sections.get("execution", {}).get("records", [])
    counts = _step_counts(execution_records)
    failed_ids = _failed_step_ids(execution_records)
    step_results = _step_results(execution_records, execution_details)

    return {
        "record_type": "RESULTS_SUMMARY",
        "record_group": "results",
        "record_schema_version": RESULTS_SUMMARY_SCHEMA_VERSION,
        "run_id": identity["run_id"],
        "run_timestamp": identity["run_timestamp"],
        "timestamp": timestamp,
        "pipeline_name": identity["pipeline"] or identity["workflow"] or identity["app"],
        "status": identity["status"],
        "run": {
            "app": identity["app"],
            "execution_kind": _execution_kind(identity),
            "started_at": identity["run_started_at"],
            "ended_at": identity["run_completed_at"],
            "duration_ms": _elapsed_ms(identity["run_started_at"], identity["run_completed_at"]),
            "steps_total": counts["total"],
            "steps_succeeded": counts["succeeded"],
            "steps_failed": counts["failed"],
            "steps_warning": _warning_step_count(step_results),
            "steps_skipped": counts["skipped"],
            "steps_pending": 0,
        },
        "execution": {
            "outcome": _outcome(identity["status"], counts),
            "failed_step_ids": failed_ids,
            "partial_success": bool(failed_ids) and counts["succeeded"] > 0,
        },
        "step_results": step_results,
        # Item and artifact correlation land in the next increment; the sections are
        # present with a stable shape so the schema boundary is fixed now.
        "item_results": [],
        "validations": _validations(records),
        "warnings": _warnings(records),
        "artifacts": {
            "inputs": [],
            "created": [],
            "failed_or_missing": [],
            "diagnostics": [],
            "execution_context": [],
        },
        "diagnostics": _diagnostics(records),
    }


def _execution_kind(identity: dict[str, Any]) -> str:
    if identity.get("pipeline"):
        return "pipeline"
    if identity.get("workflow"):
        return "workflow"
    return "app"


def _outcome(status: str, counts: dict[str, int]) -> str:
    """Classify the run outcome: success, partial_failure, or failed."""
    if str(status).lower() == "success":
        return "success"
    if counts["succeeded"] > 0 and counts["failed"] > 0:
        return "partial_failure"
    return "failed"


def _warning_step_count(step_results: list[dict[str, Any]]) -> int:
    return sum(1 for s in step_results if s.get("status") == "warning")


def _step_results(
    execution_records: list[dict[str, Any]],
    execution_details: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Project one entry per executed step, in execution (sequence) order.

    Identity/sequence/duration come from the log's STEP_START/STEP_END records;
    app / operation / exit_code enrichment comes from execution_details when present
    (never fabricated). Steps are correlated by step id/name.
    """
    starts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in execution_records:
        if _rtype(record) != _STEP_START:
            continue
        key = str(record.get("step_id") or record.get("step_name") or "")
        starts[key] = record
        order.append(key)
    ends = {str(r.get("step_name") or r.get("step_id") or ""): r
            for r in execution_records if _rtype(r) == _STEP_END}

    enrich = _execution_detail_steps(execution_details)

    results: list[dict[str, Any]] = []
    for key in order:
        start = starts.get(key, {})
        end = ends.get(key, {})
        detail = enrich.get(key, {})
        entry: dict[str, Any] = {
            "step_id": key,
            "step_name": str(start.get("step_name") or key),
            "step_sequence": start.get("step_sequence"),
            "app": detail.get("app", ""),
            "operation": detail.get("operation", ""),
            "status": str(end.get("status") or detail.get("status") or ""),
            "duration_ms": end.get("duration_ms"),
        }
        if "exit_code" in detail:
            entry["exit_code"] = detail["exit_code"]
        results.append(entry)
    return results


def _execution_detail_steps(
    execution_details: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Index execution_details steps by id/name for step enrichment (never fabricated)."""
    if not execution_details:
        return {}
    kind = str(execution_details.get("kind") or "").lower()
    domain = execution_details.get(kind) if isinstance(execution_details.get(kind), dict) else {}
    steps = domain.get("steps") if isinstance(domain, dict) else None
    if not isinstance(steps, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = str(step.get("id") or step.get("name") or "")
        if not key:
            continue
        entry: dict[str, Any] = {
            "app": str(step.get("app") or ""),
            "operation": str(step.get("process") or step.get("operation") or ""),
            "status": str(step.get("status") or ""),
        }
        if "exit_code" in step:
            entry["exit_code"] = step["exit_code"]
        indexed[key] = entry
    return indexed


def _validations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project VALIDATION_RESULT records, in log order."""
    out: list[dict[str, Any]] = []
    for record in records:
        if _rtype(record) != _VALIDATION_RESULT:
            continue
        entry = {
            "validation_name": str(record.get("validation_name") or ""),
            "status": str(record.get("status") or ""),
        }
        source = record.get("source_file") or record.get("current_file") or record.get("path")
        if source:
            entry["source_file"] = str(source)
        out.append(entry)
    return out


def _warnings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project WARNING records (retry/extraction history), in log order."""
    out: list[dict[str, Any]] = []
    for record in records:
        if _rtype(record) != _WARNING:
            continue
        entry: dict[str, Any] = {"message": str(record.get("message") or "")}
        source = record.get("source_file") or record.get("current_file")
        if source:
            entry["source_file"] = str(source)
        if record.get("attempt") is not None:
            entry["attempt"] = record["attempt"]
        out.append(entry)
    return out


def _diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble failure diagnostics incl. the complete assembled error output.

    The error output is assembled verbatim from the error-bearing records in log
    order — never sanitized or truncated by this builder. If an upstream record was
    already flagged truncated, that is reported honestly.
    """
    complete = next((r for r in reversed(records) if _rtype(r) == _RUN_COMPLETE), {})
    failure_ids = [str(r.get("failure_record_id") or r.get("record_id") or "")
                   for r in records if _rtype(r) == _STEP_FAILURE]
    error_ids = [str(r.get("error_id") or r.get("record_id") or "")
                 for r in records if _rtype(r) == _ERROR]

    parts: list[str] = []
    truncated = False
    truncated_source_ids: list[str] = []
    for record in records:
        if _rtype(record) not in (_ERROR, _APP_EXECUTION, _STEP_FAILURE):
            continue
        for field in ("full_error_output", "stderr_summary", "stdout_summary",
                      "sanitized_traceback", "traceback_summary", "error_message", "message"):
            value = record.get(field)
            if value:
                parts.append(str(value))
        if record.get("output_truncated") or record.get("truncated"):
            truncated = True
            rid = str(record.get("record_id") or record.get("error_id") or "")
            if rid:
                truncated_source_ids.append(rid)

    diagnostics: dict[str, Any] = {
        "failed_step_id": str(complete.get("failed_step_id") or ""),
        "failed_step_name": str(complete.get("failed_step_name") or ""),
        "failure_record_ids": [i for i in failure_ids if i],
        "error_record_ids": [i for i in error_ids if i],
        "error_output_source": "application_subprocess",
        "error_output_truncated": truncated,
        "full_error_output": "\n".join(parts),
    }
    if truncated_source_ids:
        diagnostics["truncated_source_record_ids"] = truncated_source_ids
    return diagnostics
