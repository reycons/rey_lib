"""Shared run lifecycle helpers for app-level operations."""

from __future__ import annotations

from typing import Any

__all__ = ["run_app_operation"]


def run_app_operation(ctx: Any, operation: str, func: Any) -> Any:
    """Run one app command inside the shared append-only run lifecycle.

    Applications supply only the operation name, context, and callable. This
    helper owns the run-log lifecycle and re-raises callable exceptions so the
    caller's existing exit-code behavior remains unchanged.
    """
    from rey_lib.config.config_utils import record_config_file_references
    from rey_lib.errors.error_utils import build_safe_error_payload
    from rey_lib.logs.log_utils import (
        bind_run,
        clear_run,
        log_error,
        log_run_complete,
        log_run_start,
        log_run_summary,
    )

    log_run_start(ctx, operation=operation)
    bind_run(ctx)
    record_config_file_references(ctx)
    try:
        result = func()
    except Exception as exc:
        error_payload = build_safe_error_payload(
            exc,
            message=f"app operation '{operation}' failed",
            failed_step_id=operation,
            failed_step_name=operation,
        )
        error_record = log_error(ctx, **error_payload)
        failure_id = str(error_record.get("error_id") or "")
        failure_message = str(error_record.get("error_message") or str(exc))
        log_run_complete(
            ctx,
            "failed",
            message=failure_message,
            failure_record_id=failure_id,
            failed_step_id=operation,
            failed_step_name=operation,
            failure_message=failure_message,
        )
        log_run_summary(ctx, {
            "operation": operation,
            "status": "failed",
            "failure_record_id": failure_id,
        })
        raise
    else:
        log_run_complete(ctx, "success")
        log_run_summary(ctx, {
            "operation": operation,
            "status": "success",
        })
        return result
    finally:
        clear_run()
