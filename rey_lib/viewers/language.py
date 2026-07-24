"""Narrow text language classification for shared viewers.

This module classifies abstract content types only. It intentionally does not
return rendering-engine language identifiers.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

APPROVED_TEXT_LANGUAGES = frozenset({
    "sql",
    "yaml",
    "json",
    "jsonl",
    "markdown",
    "python",
    "csv",
    "log",
    "text",
    "unknown",
})

_SUFFIX_LANGUAGES = {
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonl": "jsonl",
    ".md": "markdown",
    ".markdown": "markdown",
    ".py": "python",
    ".csv": "csv",
    ".log": "log",
    ".err": "log",
    ".txt": "text",
    ".text": "text",
}

_MIME_LANGUAGES = {
    "application/json": "json",
    "application/x-jsonlines": "jsonl",
    "application/x-ndjson": "jsonl",
    "text/csv": "csv",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/x-python": "python",
    "application/x-python-code": "python",
    "text/plain": "text",
    "text/x-log": "log",
    "application/sql": "sql",
    "text/x-sql": "sql",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "text/x-yaml": "yaml",
}


def classify_text_language(
    path: str | Path = "",
    mime_type: str = "",
    content: str | None = None,
    display_name: str = "",
) -> str:
    """Return the approved abstract language for a text-like file reference.

    The return value is always one of ``APPROVED_TEXT_LANGUAGES``. This helper
    does not read files and does not return rendering-engine language names.
    """
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime in _MIME_LANGUAGES:
        return _MIME_LANGUAGES[normalized_mime]

    for candidate in (path, display_name):
        suffix = Path(str(candidate or "")).suffix.lower()
        if suffix in _SUFFIX_LANGUAGES:
            return _SUFFIX_LANGUAGES[suffix]

    return _classify_text_content(content)


def _classify_text_content(content: str | None) -> str:
    text = str(content or "").strip()
    if not text:
        return "unknown"

    if _looks_like_jsonl(text):
        return "jsonl"
    if _looks_like_json(text):
        return "json"
    if _looks_like_sql(text):
        return "sql"
    if _looks_like_explicit_markdown(text):
        return "markdown"
    if _looks_like_yaml(text):
        return "yaml"
    if _looks_like_markdown(text):
        return "markdown"
    if _looks_like_python(text):
        return "python"
    if _looks_like_csv(text):
        return "csv"
    if _looks_like_log(text):
        return "log"
    return "text"


def _looks_like_json(text: str) -> bool:
    if not text.startswith(("{", "[")):
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _looks_like_jsonl(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    try:
        return all(isinstance(json.loads(line), (dict, list)) for line in lines[:5])
    except json.JSONDecodeError:
        return False


def _looks_like_sql(text: str) -> bool:
    upper = text[:2000].upper()
    starters = (
        "SELECT ",
        "WITH ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "CREATE ",
        "ALTER ",
        "DROP ",
        "TRUNCATE ",
    )
    return upper.startswith(starters) or any(f"\n{starter}" in upper for starter in starters)


def _looks_like_yaml(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return False
    return sum(1 for line in lines[:12] if ":" in line and not line.lstrip().startswith(("{", "["))) >= 2


def _looks_like_explicit_markdown(text: str) -> bool:
    """Recognize Markdown structure before broad YAML key/value heuristics."""
    lines = text.splitlines()
    first_nonempty = next((line.strip() for line in lines if line.strip()), "")
    if first_nonempty.lower() in ("```markdown", "```md"):
        return True
    heading_count = sum(
        1
        for line in lines[:40]
        if line.startswith(("# ", "## ", "### ", "#### ", "##### ", "###### "))
    )
    return heading_count >= 2


def _looks_like_markdown(text: str) -> bool:
    lines = text.splitlines()
    return any(line.startswith(("# ", "## ", "### ", "- ", "* ", "> ", "```")) for line in lines[:20])


def _looks_like_python(text: str) -> bool:
    lines = text.splitlines()
    return any(
        line.startswith(("def ", "class ", "import ", "from ")) or line.startswith("    import ")
        for line in lines[:30]
    )


def _looks_like_csv(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or "," not in lines[0]:
        return False
    try:
        parsed = list(csv.reader(lines[:5]))
    except csv.Error:
        return False
    widths = {len(row) for row in parsed if row}
    return len(widths) == 1 and next(iter(widths), 0) > 1


def _looks_like_log(text: str) -> bool:
    first = text.splitlines()[0] if text.splitlines() else ""
    return any(token in first.upper() for token in (" ERROR ", " INFO ", " WARN ", " DEBUG "))
