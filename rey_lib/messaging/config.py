"""Configuration helpers for messaging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.messaging.errors import MessageValidationError

__all__ = [
    "message_archive_path",
    "message_provider",
    "messaging_config",
    "resolve_recipient_group",
]


def messaging_config(ctx: Any) -> Any:
    """Return ``ctx.messaging`` when configured, otherwise an empty object."""
    return getattr(ctx, "messaging", None)


def message_archive_path(ctx: Any) -> Path | None:
    """Return configured file-backed message archive path."""
    cfg = messaging_config(ctx)
    raw = getattr(cfg, "archive_path", None) if cfg else None
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def message_provider(ctx: Any, channel: str, requested: str = "") -> str:
    """Return provider configured for a channel, falling back to the request."""
    if requested:
        return requested
    cfg = messaging_config(ctx)
    providers = getattr(cfg, "providers", None) if cfg else None
    channel_cfg = getattr(providers, channel, None) if providers else None
    return str(getattr(channel_cfg, "provider", "") or "")


def resolve_recipient_group(ctx: Any, group_name: str, channel: str) -> dict[str, Any]:
    """Resolve a named recipient group for a delivery channel.

    Expected YAML shape:

    messaging:
      recipient_groups:
        - name: internal_ops
          email:
            to: [...]
            cc: [...]
            bcc: [...]
            reply_to: ""
    """
    if not group_name:
        return {}

    cfg = messaging_config(ctx)
    groups = getattr(cfg, "recipient_groups", []) if cfg else []
    for group in groups or []:
        if _value(group, "name") != group_name:
            continue
        channel_cfg = _value(group, channel, {})
        if channel == "email":
            return {
                "recipients": _list_value(channel_cfg, "to"),
                "cc": _list_value(channel_cfg, "cc"),
                "bcc": _list_value(channel_cfg, "bcc"),
                "reply_to": str(_value(channel_cfg, "reply_to", "") or ""),
            }
        if channel in {"text", "phone"}:
            return {"recipients": _list_value(channel_cfg, "to")}
        if channel == "slack":
            return {
                "channels": _list_value(channel_cfg, "channels"),
                "users": _list_value(channel_cfg, "users"),
            }
        return dict(channel_cfg) if isinstance(channel_cfg, dict) else {}

    raise MessageValidationError(f"Recipient group not found: {group_name}")


def _value(source: Any, key: str, default: Any = None) -> Any:
    """Read a value from a dict or Namespace-like object."""
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _list_value(source: Any, key: str) -> list[str]:
    """Read a list field from a dict or Namespace-like object."""
    value = _value(source, key, [])
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]
