"""Pipeline-log summary messaging helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.files.file_utils import read_text_file
from rey_lib.logs import get_logger, read_jsonl_records
from rey_lib.messaging.router import create_message, send_message

__all__ = ["send_pipeline_summary"]

_logger = get_logger(__name__)


def send_pipeline_summary(
    ctx: Any,
    *,
    log_file: Path,
    pipeline_name: str,
    pipeline_status: str,
    run_id: str,
    batch_id: str = "",
    message_contract: str = "",
    message_type: str = "pipeline_run_summary",
    recipient_group: str = "",
    channel: str = "email",
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Create and deliver a pipeline summary message from a shared JSONL log."""
    if not log_file:
        raise ValueError("log_file is required.")
    path = Path(log_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Pipeline log file not found: {path}")

    content = read_text_file(path, errors="replace")
    parsed = read_jsonl_records(path, content, max_records=_record_limit(ctx))
    summary = _summarize_records(parsed.get("records", []), pipeline_status)
    resolved_dry_run = _dry_run(ctx) if dry_run is None else dry_run

    subject_template = _summary_config_value(ctx, "subject")
    body_template = _summary_config_value(ctx, "body")

    message = create_message(
        ctx,
        message_type=message_type,
        audience=str(_summary_config_value(ctx, "audience")),
        channel=channel,
        recipient_group=recipient_group,
        subject=subject_template,
        markdown=body_template,
        data={
            "pipeline_name": pipeline_name,
            "pipeline_status": pipeline_status,
            "run_id": run_id,
            "batch_id": batch_id,
            "log_file": str(path),
            "message_contract": message_contract,
            **summary,
        },
        dry_run=resolved_dry_run,
        metadata={
            "pipeline_name": pipeline_name,
            "pipeline_status": pipeline_status,
            "pipeline_run_id": run_id,
            "batch_id": batch_id,
            "log_file": str(path),
            "message_contract": message_contract,
        },
    )
    result = send_message(ctx, message)
    _logger.info(
        "Pipeline summary message %s via %s dry_run=%s",
        result.status,
        result.channel,
        result.dry_run,
        extra={
            "event_type": "message_delivery_result",
            "message_id": result.message_id,
            "message_channel": result.channel,
            "message_provider": result.provider,
            "message_status": result.status,
            "message_dry_run": result.dry_run,
            "pipeline_name": pipeline_name,
            "pipeline_run_id": run_id,
        },
    )
    return {
        "message_id": message.message_id,
        "message_status": message.status,
        "delivery_status": result.status,
        "dry_run": result.dry_run,
        "warning_count": summary["warning_count"],
        "error_count": summary["error_count"],
    }


def _summarize_records(records: list[dict[str, Any]], pipeline_status: str) -> dict[str, Any]:
    """Return deterministic summary fields from parsed JSONL records."""
    warnings: list[str] = []
    errors: list[str] = []
    failed_steps: list[str] = []

    for record in records:
        level = str(record.get("level") or record.get("levelname") or "").upper()
        status = str(record.get("status") or "")
        message = str(record.get("message") or "")
        if level == "WARNING" or status == "warning":
            warnings.append(_compact_line(record, message))
        if level in {"ERROR", "CRITICAL"} or status == "failed":
            errors.append(_compact_line(record, message))
        if record.get("event_type") == "pipeline_step_completed" and status == "failed":
            failed_steps.append(str(record.get("pipeline_step_name") or record.get("app") or "unknown"))

    return {
        "warning_count": str(len(warnings)),
        "error_count": str(len(errors)),
        "failed_step_count": str(len(failed_steps)),
        "failed_steps": ", ".join(dict.fromkeys(failed_steps)) or "None",
        "warning_summary": _bullet_list(warnings) or "- None",
        "error_summary": _bullet_list(errors) or "- None",
        "overall_summary": _overall_summary(pipeline_status, len(warnings), len(errors)),
    }


def _compact_line(record: dict[str, Any], message: str) -> str:
    """Return one stable human-readable event line."""
    step = record.get("pipeline_step_name") or record.get("app") or record.get("source") or record.get("name")
    prefix = f"{step}: " if step else ""
    return f"{prefix}{message}".strip()


def _bullet_list(items: list[str], limit: int = 10) -> str:
    """Render summary lines as Markdown bullets."""
    selected = items[:limit]
    lines = [f"- {item}" for item in selected if item]
    if len(items) > limit:
        lines.append(f"- {len(items) - limit} additional events omitted")
    return "\n".join(lines)


def _overall_summary(pipeline_status: str, warning_count: int, error_count: int) -> str:
    """Return a deterministic plain-language overall summary."""
    if pipeline_status == "success" and not warning_count and not error_count:
        return "Pipeline completed successfully with no warnings or errors."
    return (
        f"Pipeline finished with status {pipeline_status}; "
        f"{warning_count} warning(s), {error_count} error/failure event(s)."
    )


def _summary_config_value(ctx: Any, key: str) -> str:
    """Read pipeline summary messaging config without inventing paths."""
    cfg = getattr(getattr(ctx, "messaging", None), "pipeline_summary", None)
    if not cfg or not hasattr(cfg, key):
        raise ValueError(f"messaging.pipeline_summary.{key} is required.")
    value = getattr(cfg, key)
    if value in (None, ""):
        raise ValueError(f"messaging.pipeline_summary.{key} is required.")
    return str(value)


def _record_limit(ctx: Any) -> int:
    """Return configured log record limit."""
    cfg = getattr(getattr(ctx, "messaging", None), "pipeline_summary", None)
    if not cfg or not hasattr(cfg, "record_limit"):
        raise ValueError("messaging.pipeline_summary.record_limit is required.")
    return int(getattr(cfg, "record_limit"))


def _dry_run(ctx: Any) -> bool:
    """Return configured dry-run setting."""
    cfg = getattr(getattr(ctx, "messaging", None), "delivery", None)
    if cfg and hasattr(cfg, "dry_run"):
        return bool(getattr(cfg, "dry_run"))
    raise ValueError("messaging.delivery.dry_run is required.")
