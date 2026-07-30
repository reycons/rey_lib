"""Execution lifecycle record helpers for shared run logs."""

from __future__ import annotations

import uuid
from typing import Any

from rey_lib.logs.record_enrichment import log_run_record


def log_run_start(ctx: Any, **fields: Any) -> None:
    """Append a RUN_START execution record marking the start of the run."""
    log_run_record(ctx, "RUN_START", **fields)


def log_execution_plan(ctx: Any, *, total_steps: int,
                       steps: list[dict[str, Any]], **fields: Any) -> None:
    """Append one EXECUTION_PLAN execution record describing the ordered plan.

    Emitted once per run, after RUN_START and before the first STEP_START, so the
    run log is the durable source of truth for step count, order, and identity
    (SGC_Pipeline_Coordinator_Execution_Plan_Record). ``run_id``, ``run_timestamp``,
    and ``pipeline_name`` are enriched by the shared logging layer.
    """
    log_run_record(
        ctx, "EXECUTION_PLAN",
        total_steps=total_steps, steps=list(steps), **fields,
    )


def log_step_start(ctx: Any, step_name: str, step_sequence: int,
                   step_type: str = "", step_id: str = "", **fields: Any) -> None:
    """Append a STEP_START execution record for one step."""
    log_run_record(
        ctx, "STEP_START",
        step_name=step_name, step_sequence=step_sequence, step_type=step_type,
        step_id=step_id or fields.pop("step_id", ""),
        **fields,
    )


def log_step_end(ctx: Any, step_name: str, status: str, *,
                 message: str = "", **fields: Any) -> None:
    """Append a STEP_END execution record with the step status (success/failure/skipped)."""
    log_run_record(
        ctx, "STEP_END",
        step_name=step_name, status=status, message=message, **fields,
    )


def log_run_complete(ctx: Any, status: str, *, message: str = "", **fields: Any) -> None:
    """Append a RUN_COMPLETE execution record with the final run status."""
    log_run_record(ctx, "RUN_COMPLETE", status=status, message=message, **fields)


def log_run_summary(ctx: Any, summary: dict[str, Any]) -> None:
    """Append a deterministic RUN_SUMMARY run-result record (no LLM required)."""
    log_run_record(ctx, "RUN_SUMMARY", summary=summary)


def log_step_failure(
    ctx: Any,
    *,
    failed_step_id: str,
    failed_step_name: str,
    message: str,
    error_type: str = "",
    error_message: str = "",
    sanitized_exception: str = "",
    sanitized_traceback: str = "",
    exit_code: int | None = None,
    related_path: str = "",
    related_artifact_id: str = "",
    traceback_summary: str = "",
    **fields: Any,
) -> str:
    """Append STEP_FAILURE evidence and return its failure record id."""
    failure_record_id = str(fields.pop("failure_record_id", "") or uuid.uuid4())
    payload: dict[str, Any] = {
        "failure_record_id": failure_record_id,
        "status": "failed",
        "failed_step_id": failed_step_id,
        "failed_step_name": failed_step_name,
        "message": message,
        "error_type": error_type,
        "error_message": error_message,
        "sanitized_exception": sanitized_exception,
        "sanitized_traceback": sanitized_traceback,
        "related_path": related_path,
        "related_artifact_id": related_artifact_id,
        "traceback_summary": traceback_summary,
        **fields,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    log_run_record(ctx, "STEP_FAILURE", **payload)
    return failure_record_id


def log_error(ctx: Any, *, message: str, error_type: str = "",
              sanitized_exception: str = "", **fields: Any) -> dict[str, Any]:
    """Append a structured ERROR record from an error_utils canonical payload."""
    from rey_lib.errors.error_utils import build_error_record_payload

    if sanitized_exception:
        fields["sanitized_exception"] = sanitized_exception
    payload = build_error_record_payload(
        message=message, error_type=error_type, **fields
    )
    record_fields = dict(payload)
    record_message = str(record_fields.pop("message", "") or message)
    log_run_record(ctx, "ERROR", message=record_message, **record_fields)
    return payload


def log_app_execution(
    ctx: Any,
    *,
    app: str,
    entrypoint: str = "",
    arguments_redacted: list[Any] | None = None,
    working_directory: str = "",
    status: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    stdout_summary: str = "",
    stderr_summary: str = "",
    **fields: Any,
) -> None:
    """Append APP_EXECUTION evidence for one app or external process invocation."""
    payload: dict[str, Any] = {
        "app": app,
        "entrypoint": entrypoint,
        "arguments_redacted": list(arguments_redacted or []),
        "working_directory": working_directory,
        "status": status,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        **fields,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    log_run_record(ctx, "APP_EXECUTION", **payload)


def log_row_count(ctx: Any, *, count_name: str, count: int,
                  subject: str = "", **fields: Any) -> None:
    """Append a ROW_COUNT record for run evidence."""
    log_run_record(
        ctx, "ROW_COUNT", count_name=count_name, count=count,
        subject=subject, **fields,
    )


def log_validation_result(ctx: Any, *, validation_name: str, status: str,
                          message: str = "", **fields: Any) -> None:
    """Append a VALIDATION_RESULT record for run evidence."""
    log_run_record(
        ctx, "VALIDATION_RESULT", validation_name=validation_name,
        status=status, message=message, **fields,
    )
