"""SQL execution record helpers for shared run logs."""

from __future__ import annotations

from typing import Any

from rey_lib.logs.record_enrichment import log_run_record


def log_sql_execution(ctx: Any, *, connection_name: str = "", database: str = "",
                      schema: str = "", sql_path: str = "", sql_label: str = "",
                      operation: str = "", status: str = "",
                      duration_ms: int | None = None,
                      error_message: str = "",
                      safe_to_preview: bool | None = None,
                      **fields: Any) -> None:
    """Append SQL_EXECUTION evidence for generated or executed SQL work."""
    if error_message:
        from rey_lib.errors.error_utils import build_error_record_payload
        error_message = str(
            build_error_record_payload(message=error_message).get("error_message") or ""
        )
    payload: dict[str, Any] = {
        "connection_name": connection_name,
        "database": database,
        "schema": schema,
        "sql_path": sql_path,
        "sql_label": sql_label,
        "operation": operation,
        "status": status,
        "error_message": error_message,
        **fields,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if safe_to_preview is not None:
        payload["safe_to_preview"] = bool(safe_to_preview)
    log_run_record(ctx, "SQL_EXECUTION", **payload)
