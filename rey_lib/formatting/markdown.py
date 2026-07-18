"""Shared Markdown formatting."""

from __future__ import annotations

from markdown_it import MarkdownIt

__all__ = ["markdown_to_html"]

_MARKDOWN = MarkdownIt("commonmark", {"html": False})


def markdown_to_html(markdown_text: str) -> str:
    """Return a semantic HTML fragment for application-neutral Markdown text."""
    return _MARKDOWN.render(markdown_text or "")
