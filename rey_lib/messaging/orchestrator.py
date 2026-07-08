"""Message set orchestration — resolve, build, and execute messaging workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.files.file_utils import read_text_file
from rey_lib.logs import get_logger, log_artifact_reference, read_jsonl_records
from rey_lib.messaging.body_builder import build_body
from rey_lib.messaging.config import (
    delivery_dry_run,
    message_archive_path,
    resolve_message_definition,
    resolve_message_set,
    resolve_template_definition,
)
from rey_lib.messaging.errors import MessagingError
from rey_lib.messaging.router import create_message, send_message

__all__ = ["execute_message_set"]

_logger = get_logger(__name__)

# Maximum records read from a JSONL log before passing to body builders.
# Body builders apply their own per-filter limits.
_JSONL_READ_LIMIT = 10_000


def execute_message_set(
    ctx: Any,
    message_set_name: str,
    context_file: Path,
    context_type: str,
    *,
    recipient_group: str = "",
    channel: str = "",
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Resolve and execute all enabled messages in a named message set.

    Parameters
    ----------
    ctx : Any
        App context with ``messaging`` configuration.
    message_set_name : str
        Name of the message set to execute (must match ``messaging.message_sets``).
    context_file : Path
        Context file whose content is passed to body builders.
    context_type : str
        How to interpret the context file: ``jsonl_log``, ``json``,
        ``markdown_file``, or ``static_text``.
    recipient_group : str
        Optional per-call override for the message's configured recipient_group.
    channel : str
        Optional per-call override for the message's configured channel.
    dry_run : bool | None
        Optional override for delivery dry_run; falls back to
        ``messaging.delivery.dry_run`` when ``None``.

    Returns
    -------
    list[dict[str, Any]]
        One result dict per message: message_name, message_id, status,
        dry_run, channel, provider. Skipped messages include a ``reason`` key.
    """
    if not message_set_name:
        raise MessagingError("message_set_name is required.")
    if not context_file:
        raise MessagingError("context_file is required.")
    if not context_type:
        raise MessagingError("context_type is required.")

    resolved_dry_run = delivery_dry_run(ctx) if dry_run is None else dry_run
    path = Path(context_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Context file not found: {path}")

    _logger.info(
        "messaging execute_message_set=%s context_type=%s dry_run=%s",
        message_set_name,
        context_type,
        resolved_dry_run,
        extra={
            "event_type": "message_set_resolved",
            "message_set": message_set_name,
            "context_type": context_type,
            "dry_run": resolved_dry_run,
        },
    )

    raw_text, records = _load_context_file(path, context_type)
    message_names = resolve_message_set(ctx, message_set_name)

    results: list[dict[str, Any]] = []
    for message_name in message_names:
        result = _execute_one(
            ctx,
            message_name=message_name,
            raw_text=raw_text,
            records=records,
            context_type=context_type,
            context_file=path,
            recipient_group_override=recipient_group,
            channel_override=channel,
            dry_run=resolved_dry_run,
        )
        results.append(result)

    # Record the message archive as a grounded messaging artifact so the console
    # groups it under the messaging producer
    # (SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector). Emission is
    # fail-safe and never affects delivery.
    archive = message_archive_path(ctx)
    if archive and Path(archive).exists():
        log_artifact_reference(
            ctx, str(archive), role="message_archive", event="written",
            producer="messaging", artifact_type="message_archive",
            source_path=str(path), viewer_type="file", safe_to_preview=True,
        )

    return results


def _load_context_file(
    path: Path,
    context_type: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Load raw text and parsed records from the context file."""
    raw_text = read_text_file(path, errors="replace")
    records: list[dict[str, Any]] = []
    if context_type == "jsonl_log":
        parsed = read_jsonl_records(path, raw_text, max_records=_JSONL_READ_LIMIT)
        records = parsed.get("records", [])
    return raw_text, records


def _execute_one(
    ctx: Any,
    *,
    message_name: str,
    raw_text: str,
    records: list[dict[str, Any]],
    context_type: str,
    context_file: Path,
    recipient_group_override: str,
    channel_override: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Execute a single message definition."""
    defn = resolve_message_definition(ctx, message_name)

    if not getattr(defn, "enabled", True):
        _logger.info(
            "messaging skip message=%s reason=disabled",
            message_name,
            extra={
                "event_type": "message_definition_resolved",
                "message_name": message_name,
                "enabled": False,
            },
        )
        return {"message_name": message_name, "status": "skipped", "reason": "disabled"}

    channel = channel_override or str(getattr(defn, "channel", "") or "")
    if not channel:
        raise MessagingError(
            f"message '{message_name}' is missing required field 'channel'."
        )

    resolved_recipient_group = recipient_group_override or str(
        getattr(defn, "recipient_group", "") or ""
    )
    if not resolved_recipient_group:
        raise MessagingError(
            f"message '{message_name}' is missing required field 'recipient_group'."
        )

    template_name = str(getattr(defn, "template", "") or "")
    template_defn = resolve_template_definition(ctx, template_name) if template_name else None
    template_subject = str(getattr(template_defn, "subject", "") or "") if template_defn else ""
    template_body = str(getattr(template_defn, "body", "") or "") if template_defn else ""

    body_builder_config = getattr(defn, "body_builder", None)
    base_data: dict[str, Any] = {"context_file": str(context_file)}
    body_data, body_markdown = build_body(
        body_builder_config,
        records=records,
        raw_text=raw_text,
        data=base_data,
    )

    _logger.info(
        "messaging message_definition_resolved message=%s channel=%s recipient_group=%s",
        message_name,
        channel,
        resolved_recipient_group,
        extra={
            "event_type": "message_definition_resolved",
            "message_name": message_name,
            "channel": channel,
            "recipient_group": resolved_recipient_group,
            "dry_run": dry_run,
        },
    )

    message = create_message(
        ctx,
        message_type=message_name,
        audience=str(getattr(defn, "audience", "internal") or "internal"),
        channel=channel,
        recipient_group=resolved_recipient_group,
        subject=template_subject,
        markdown=body_markdown or template_body,
        data=body_data,
        dry_run=dry_run,
    )
    result = send_message(ctx, message)

    return {
        "message_name": message_name,
        "message_id": message.message_id,
        "status": result.status,
        "dry_run": result.dry_run,
        "channel": result.channel,
        "provider": result.provider,
    }
