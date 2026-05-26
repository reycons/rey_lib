"""Deterministic HTML rendering for messages."""

from __future__ import annotations

from html import escape

__all__ = ["markdown_to_html", "text_to_html"]


def text_to_html(text: str) -> str:
    """Render plain text as simple HTML paragraphs."""
    paragraphs = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(f"<p>{escape(line)}</p>" for line in paragraphs)


def markdown_to_html(markdown: str) -> str:
    """Render a safe subset of Markdown as HTML."""
    lines: list[str] = []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            lines.append(f"<h1>{escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            lines.append(f"<h2>{escape(line[3:].strip())}</h2>")
        elif line.startswith("- "):
            lines.append(f"<p>&bull; {escape(line[2:].strip())}</p>")
        else:
            lines.append(f"<p>{escape(line)}</p>")
    return "\n".join(lines)
