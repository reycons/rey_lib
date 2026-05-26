"""Small deterministic Markdown-to-text helpers."""

from __future__ import annotations

import re

__all__ = ["markdown_to_text"]


def markdown_to_text(markdown: str) -> str:
    """Return a conservative plain-text rendering of Markdown."""
    text = markdown or ""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    return text.strip()
