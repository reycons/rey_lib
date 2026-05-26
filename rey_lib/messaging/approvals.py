"""Approval state helpers."""

from __future__ import annotations

from rey_lib.messaging.models import Message, MessageApproval, utc_now

__all__ = ["approve_message", "mark_approval_required"]


def mark_approval_required(message: Message, required: bool) -> Message:
    """Apply approval policy state to a message."""
    message.approval = MessageApproval(status="required" if required else "not_required")
    message.status = "approval_required" if required else message.status
    message.updated_at = utc_now()
    return message


def approve_message(message: Message, reviewer: str = "", reason: str = "") -> Message:
    """Mark a message approved."""
    message.approval = MessageApproval(
        status="approved",
        reviewer=reviewer,
        reason=reason,
        approved_at=utc_now(),
    )
    message.status = "approved"
    message.updated_at = utc_now()
    return message
