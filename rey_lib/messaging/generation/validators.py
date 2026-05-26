"""Message validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.messaging.errors import MessageValidationError
from rey_lib.messaging.models import Message

__all__ = ["validate_message"]


def validate_message(ctx: Any, message: Message) -> Message:
    """Validate a generated message before approval, rendering, or delivery."""
    request = message.request
    if not request.message_type:
        raise MessageValidationError("message_type is required.")
    if request.channel not in {"email", "text", "webhook"}:
        raise MessageValidationError(f"Unsupported message channel: {request.channel}")
    if not request.recipients:
        raise MessageValidationError("At least one recipient is required.")
    if request.channel == "email" and not message.content.subject:
        raise MessageValidationError("Email subject is required.")
    if not any((message.content.body_text, message.content.body_markdown, message.content.body_html)):
        raise MessageValidationError("Message body is required.")
    if request.channel == "text":
        _validate_text_message(ctx, message)
    _validate_attachments(message)
    message.status = "validated"
    return message


def _validate_text_message(ctx: Any, message: Message) -> None:
    """Validate SMS-safe content."""
    max_length = int(getattr(getattr(ctx, "messaging", None), "text_max_length", 160) or 160)
    body = message.content.body_text
    if len(body) > max_length:
        raise MessageValidationError(f"Text message exceeds {max_length} characters.")
    if message.request.attachments:
        raise MessageValidationError("Text messages do not support attachments.")
    if message.content.body_html or message.content.body_markdown:
        raise MessageValidationError("Text messages support plain text only.")


def _validate_attachments(message: Message) -> None:
    """Validate attachment paths without opening file content."""
    for attachment in message.request.attachments:
        path = Path(attachment.path).expanduser()
        if not path.exists() or not path.is_file():
            raise MessageValidationError(f"Attachment not found: {path}")
