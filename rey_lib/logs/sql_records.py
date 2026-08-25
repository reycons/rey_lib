"""SQL execution record helpers for shared run logs."""

from __future__ import annotations

from typing import Any

from rey_lib.logs.record_enrichment import log_run_record


def log_sql_execution(run_log: 'RunLog', *, connection_name: str = "", database: str = "",
                      schema: str = "", sql_path: str = "", sql_label: str = "",
                      operation: str = "", status: str = "",
                      duration_ms: int | None = None,
                      error_message: str = "",
                      safe_to_preview: bool | None = None,
                      **fields: Any) -> None:
    """Append SQL_EXECUTION evidence for generated or executed SQL work.

    ``run_log`` is None only where there is genuinely no run log to record
    against -- Control's optional capabilities, which predate run-log
    persistence and are not part of it. SQL with no owner is not recorded
    rather than recorded somewhere invented for it.
    """
    if run_log is None:
        return
    error_payload: dict[str, Any] | None = None
    if error_message:
        from rey_lib.errors.error_utils import build_error_record_payload

        # The column carries the failure object, so the canonical payload is
        # kept whole rather than reduced to its message.
        error_payload = build_error_record_payload(message=error_message)
    payload: dict[str, Any] = {
        "connection_name": connection_name,
        "database": database,
        "schema": schema,
        "sql_path": sql_path,
        "sql_label": sql_label,
        "operation": operation,
        "status": status,
        "error_message": error_payload,
        **fields,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if safe_to_preview is not None:
        payload["safe_to_preview"] = bool(safe_to_preview)
    run_log.append("SQL_EXECUTION", **payload)
