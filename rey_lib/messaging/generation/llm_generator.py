"""Constrained message draft generation."""

from __future__ import annotations

from string import Template
from typing import Any

from rey_lib.messaging.generation.contracts import ALLOWED_GENERATION_MODES
from rey_lib.messaging.models import Message, MessageContent, MessageRequest, utc_now
from rey_lib.messaging.errors import MessageValidationError

__all__ = ["generate_message"]


def generate_message(
    ctx: Any,
    request: MessageRequest,
) -> Message:
    """Create a deterministic draft from templates or static text."""
    if request.generation_mode not in ALLOWED_GENERATION_MODES:
        raise MessageValidationError(f"Unsupported generation mode: {request.generation_mode}")

    content = _template_content(request)

    message = Message(request=request, status="generated", content=content)
    message.updated_at = utc_now()
    return message


def _template_content(request: MessageRequest) -> MessageContent:
    """Render deterministic request content with Template substitution."""
    data = {key: str(value) for key, value in request.data.items()}
    subject = _safe_substitute(request.subject, data)
    body_text = _safe_substitute(request.body, data)
    body_markdown = _safe_substitute(request.markdown, data)
    body_html = _safe_substitute(request.html, data)
    if not body_text and body_markdown:
        body_text = body_markdown
    return MessageContent(
        subject=subject,
        body_text=body_text,
        body_markdown=body_markdown,
        body_html=body_html,
    )


def _safe_substitute(text: str, data: dict[str, str]) -> str:
    """Substitute template variables without failing on missing values."""
    return Template(text or "").safe_substitute(data)
