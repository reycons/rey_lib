"""Structured messaging audit logging."""

from __future__ import annotations

from typing import Any

from rey_lib.logs import get_logger
from rey_lib.messaging.models import Message

__all__ = ["audit_event", "audit_payload"]

_logger = get_logger(__name__)


def audit_payload(message: Message, event_type: str, error: str = "") -> dict[str, Any]:
    """Return the required JSONL audit payload for one messaging action."""
    request = message.request
    return {
        "event_type": event_type,
        "message_id": message.message_id,
        "message_type": request.message_type,
        "message_channel": request.channel,
        "message_provider": message.provider,
        "message_status": message.status,
        "message_recipients": list(request.recipients),
        "message_subject": message.content.subject or request.subject,
        "message_template": request.template,
        "message_generation_mode": request.generation_mode,
        "message_approval_status": message.approval.status,
        "message_dry_run": request.dry_run,
        "message_error": error or message.error,
    }


def audit_event(message: Message, event_type: str, error: str = "") -> None:
    """Write a structured messaging event to the configured JSONL logger."""
    payload = audit_payload(message, event_type, error)
    level = _logger.error if error else _logger.info
    level("messaging %s message_id=%s status=%s", event_type, message.message_id, message.status, extra=payload)
