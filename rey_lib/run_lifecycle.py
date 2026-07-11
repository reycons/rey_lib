"""Shared run lifecycle helpers for app-level operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["run_app_operation", "pipeline_run_dir", "pipeline_run_ctx_path"]


def pipeline_run_dir(
    state_dir: Path,
    run_id: str,
    pipeline_name: str,
) -> Path:
    """Return the canonical directory for a single pipeline run.

    All of a run's per-step artifacts live directly under this directory, kept
    under ``<state_dir>/pipeline_runs`` and namespaced by run id and pipeline name
    to preserve run isolation.

    Parameters
    ----------
    state_dir : Path
        The resolved application state directory.
    run_id : str
        The pipeline run identifier.
    pipeline_name : str
        The pipeline name.

    Returns
    -------
    Path
        The run directory (not created by this function).
    """
    # Compose the run-scoped directory; callers create it as needed.
    return state_dir / "pipeline_runs" / run_id / pipeline_name


def pipeline_run_ctx_path(
    state_dir: Path,
    run_id: str,
    pipeline_name: str,
    step_id: str,
) -> Path:
    """Return the flat step-context snapshot path for a pipeline step.

    A step that persists only its execution context stores it as a single
    ``<step_id>.ctx.json`` file directly under the pipeline run directory, so no
    per-step directory is created for the context alone. The filename itself
    identifies the step; this helper is the single source of truth for the path.

    Parameters
    ----------
    state_dir : Path
        The resolved application state directory.
    run_id : str
        The pipeline run identifier.
    pipeline_name : str
        The pipeline name.
    step_id : str
        The step identifier (its name), used verbatim as the filename stem.

    Returns
    -------
    Path
        The ``<step_id>.ctx.json`` path under the pipeline run directory.
    """
    # The filename carries the step identity — no per-step directory is created.
    return pipeline_run_dir(
        state_dir=state_dir,
        run_id=run_id,
        pipeline_name=pipeline_name,
    ) / f"{step_id}.ctx.json"


def run_app_operation(ctx: Any, operation: str, func: Any) -> Any:
    """Run one app command inside the shared append-only run lifecycle.

    Applications supply only the operation name, context, and callable. This
    helper owns the run-log lifecycle and re-raises callable exceptions so the
    caller's existing exit-code behavior remains unchanged.
    """
    from rey_lib.config.config_utils import record_config_file_references
    from rey_lib.errors.error_utils import build_error_record_payload, build_safe_error_payload
    from rey_lib.logs.log_utils import (
        bind_run,
        clear_run,
        finalize_run_log,
        log_error,
        log_run_complete,
        log_run_start,
        log_step_failure,
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
        raise
    else:
        if _is_failed_result(result):
            failure_message = (
                f"app operation '{operation}' returned nonzero result {result}."
            )
            error_record = log_error(
                ctx,
                **build_error_record_payload(
                    message=failure_message,
                    error_type="AppOperationFailed",
                    failed_step_id=operation,
                    failed_step_name=operation,
                    result=result,
                ),
            )
            failure_record_id = str(error_record.get("error_id") or "")
            failure_id = log_step_failure(
                ctx,
                failed_step_id=operation,
                failed_step_name=operation,
                message=failure_message,
                failure_record_id=failure_record_id,
                error_id=failure_record_id,
            )
            log_run_complete(
                ctx,
                "failed",
                message=failure_message,
                failure_record_id=failure_record_id or failure_id,
                failed_step_id=operation,
                failed_step_name=operation,
                failure_message=failure_message,
            )
            return result
        log_run_complete(ctx, "success")
        return result
    finally:
        # After the terminal RUN_COMPLETE is durably written, the shared framework
        # appends the canonical RUN_SUMMARY (apps contribute no execution_details).
        # Runs before clear_run so the run-bound append target is still active.
        finalize_run_log(ctx)
        clear_run()


def _is_failed_result(result: Any) -> bool:
    """Return whether a callable result represents a failed app exit code."""
    return isinstance(result, int) and not isinstance(result, bool) and result != 0
