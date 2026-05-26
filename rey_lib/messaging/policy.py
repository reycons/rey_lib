"""Deterministic messaging policy checks."""

from __future__ import annotations

from typing import Any

from rey_lib.messaging.models import MessageRequest

__all__ = ["approval_required"]


def approval_required(ctx: Any, request: MessageRequest) -> bool:
    """Return whether a message requires approval."""
    if request.approval_required is not None:
        return bool(request.approval_required)

    cfg = getattr(ctx, "messaging", None)
    approvals = getattr(cfg, "approvals", None) if cfg else None
    if not approvals:
        return False

    audiences = set(getattr(approvals, "required_audiences", []) or [])
    message_types = set(getattr(approvals, "required_message_types", []) or [])
    channels = set(getattr(approvals, "required_channels", []) or [])

    return (
        request.audience in audiences
        or request.message_type in message_types
        or request.channel in channels
    )
