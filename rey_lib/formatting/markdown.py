"""Shared Markdown formatting."""

from __future__ import annotations

from markdown_it import MarkdownIt

__all__ = ["markdown_to_html"]

_MARKDOWN = MarkdownIt("commonmark", {"html": False})


def markdown_to_html(markdown_text: str) -> str:
    """Return a semantic HTML fragment for application-neutral Markdown text."""
    return _MARKDOWN.render(_unwrap_outer_markdown_fence(markdown_text or ""))


def _unwrap_outer_markdown_fence(markdown_text: str) -> str:
    """Remove one model-supplied fence around an entire Markdown document."""
    lines = markdown_text.splitlines(keepends=True)
    if len(lines) < 2 or lines[0].strip().lower() not in ("```markdown", "```md"):
        return markdown_text
    if lines[-1].strip() != "```":
        return markdown_text
    return "".join(lines[1:-1])
