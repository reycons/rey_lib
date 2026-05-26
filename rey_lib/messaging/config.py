"""Configuration helpers for messaging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.messaging.errors import MessageValidationError

__all__ = [
    "delivery_dry_run",
    "message_archive_path",
    "message_provider",
    "messaging_config",
    "resolve_message_definition",
    "resolve_message_set",
    "resolve_recipient_group",
    "resolve_template_definition",
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


def resolve_message_set(ctx: Any, name: str) -> list[str]:
    """Return the ordered list of message names belonging to a named message set.

    Expected YAML shape::

        messaging:
          message_sets:
            - name: pipeline_run_complete
              messages:
                - pipeline_run_email_summary
                - pipeline_run_sms_alert

    Raises
    ------
    MessageValidationError
        If the message set is not found or has no messages.
    """
    if not name:
        raise MessageValidationError("message_set name is required.")
    cfg = messaging_config(ctx)
    sets = getattr(cfg, "message_sets", []) if cfg else []
    for entry in sets or []:
        if _value(entry, "name") != name:
            continue
        messages = _list_value(entry, "messages")
        if not messages:
            raise MessageValidationError(
                f"Message set '{name}' is configured but has no messages."
            )
        return messages
    raise MessageValidationError(f"Message set not found: '{name}'.")


def resolve_message_definition(ctx: Any, name: str) -> Any:
    """Return the message definition entry for a named message.

    Expected YAML shape::

        messaging:
          messages:
            - name: pipeline_run_email_summary
              enabled: true
              channel: email
              recipient_group: internal_ops
              template: pipeline_run_summary
              body_builder:
                type: llm_log_summary
                ...

    Raises
    ------
    MessageValidationError
        If the message definition is not found.
    """
    if not name:
        raise MessageValidationError("message name is required.")
    cfg = messaging_config(ctx)
    messages = getattr(cfg, "messages", []) if cfg else []
    for entry in messages or []:
        if _value(entry, "name") == name:
            return entry
    raise MessageValidationError(f"Message definition not found: '{name}'.")


def resolve_template_definition(ctx: Any, name: str) -> Any | None:
    """Return the template definition entry for a named template, or None.

    Expected YAML shape::

        messaging:
          templates:
            - name: pipeline_run_summary
              subject: "Run Summary: $source_name — $status"
              body: |
                # $source_name
                Status: $status
                $body
    """
    if not name:
        return None
    cfg = messaging_config(ctx)
    templates = getattr(cfg, "templates", []) if cfg else []
    for entry in templates or []:
        if _value(entry, "name") == name:
            return entry
    return None


def delivery_dry_run(ctx: Any) -> bool:
    """Return the configured dry_run flag from ``messaging.delivery.dry_run``.

    Raises
    ------
    MessageValidationError
        If ``messaging.delivery.dry_run`` is not configured.
    """
    cfg = messaging_config(ctx)
    delivery = getattr(cfg, "delivery", None) if cfg else None
    if delivery is None or not hasattr(delivery, "dry_run"):
        raise MessageValidationError("messaging.delivery.dry_run is required.")
    return bool(getattr(delivery, "dry_run"))


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
