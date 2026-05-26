"""Future database schema names for messaging persistence."""

from __future__ import annotations

__all__ = ["TABLES"]

TABLES = (
    "Message",
    "MessageEvent",
    "MessageDeliveryAttempt",
    "MessageAttachment",
    "MessageApproval",
    "MessageRecipient",
)
