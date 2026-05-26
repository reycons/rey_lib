"""Channel-safe message rendering."""

from __future__ import annotations

from rey_lib.messaging.models import Message
from rey_lib.messaging.rendering.html import markdown_to_html, text_to_html
from rey_lib.messaging.rendering.markdown import markdown_to_text

__all__ = ["render_message"]


def render_message(message: Message) -> Message:
    """Render generated content into channel-safe body formats."""
    content = message.content
    if message.request.channel == "email":
        if not content.body_text and content.body_markdown:
            content.body_text = markdown_to_text(content.body_markdown)
        if not content.body_html:
            content.body_html = (
                markdown_to_html(content.body_markdown)
                if content.body_markdown
                else text_to_html(content.body_text)
            )
    elif message.request.channel == "text":
        content.body_text = content.body_text or markdown_to_text(content.body_markdown)
        content.body_markdown = ""
        content.body_html = ""
    message.status = "rendered"
    return message
