"""Deterministic HTML rendering for messages."""

from __future__ import annotations

from html import escape

__all__ = ["text_to_html"]


def text_to_html(text: str) -> str:
    """Render plain text as simple HTML paragraphs."""
    paragraphs = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(f"<p>{escape(line)}</p>" for line in paragraphs)
