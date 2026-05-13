"""
Document loading for LLM workflow inputs.

Converts various input types into a plain-text representation suitable
for inclusion in an LLM prompt, plus a SHA-256 hash of the content for
the audit trail.

All loaders return a 2-tuple: (text, hash). The text is what gets sent
to the LLM. The hash is stored in EvaluationResult.input_hash.

Public API
----------
from_csv(path, max_rows)
    Load a CSV file and return a markdown table + hash.
from_excel(path, sheet, max_rows)
    Load an Excel file (auto-detects header row) + hash.
from_query_result(rows, max_rows)
    Format a list of row dicts as a markdown table + hash.
from_text(path)
    Read a plain text or markdown file + hash.
from_string(text)
    Hash and return an already-built string (for programmatic inputs).
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "from_csv",
    "from_excel",
    "from_query_result",
    "from_text",
    "from_string",
]

# Maximum rows sent to the LLM when no limit is specified.
_DEFAULT_MAX_ROWS = 200


def from_csv(
    path:     Path,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> tuple[str, str]:
    """
    Load a CSV file and format it as a markdown table.

    Parameters
    ----------
    path : Path
        Path to the CSV file.
    max_rows : int
        Maximum data rows to include. A note is appended when truncated.

    Returns
    -------
    tuple[str, str]
        (markdown_table, sha256_hex)
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    return _rows_to_markdown(rows, max_rows, source=path.name)


def from_excel(
    path:     Path,
    sheet:    Optional[str] = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> tuple[str, str]:
    """
    Load an Excel file and format it as a markdown table.

    Scans for the first row that looks like a header (all non-empty string
    values). Skips leading blank rows and metadata rows automatically.

    Parameters
    ----------
    path : Path
        Path to the .xlsx or .xls file.
    sheet : Optional[str]
        Sheet name. Defaults to the first sheet.
    max_rows : int
        Maximum data rows to include.

    Returns
    -------
    tuple[str, str]
        (markdown_table, sha256_hex)
    """
    import openpyxl  # noqa: PLC0415 — optional dep, lazy import

    path = Path(path)
    wb   = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws   = wb[sheet] if sheet else wb.active

    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Find header row: first row where every cell is a non-empty string.
    header_idx = None
    for i, row in enumerate(all_rows):
        if row and all(isinstance(c, str) and c.strip() for c in row if c is not None):
            header_idx = i
            break

    if header_idx is None:
        # Fall back to first non-blank row.
        for i, row in enumerate(all_rows):
            if any(c is not None for c in row):
                header_idx = i
                break

    if header_idx is None:
        text = f"Source: {path.name}\n\n(empty sheet)"
        return text, _hash(text)

    headers     = [str(c).strip() if c is not None else "" for c in all_rows[header_idx]]
    data_rows   = all_rows[header_idx + 1:]
    dict_rows   = [
        {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        for row in data_rows
        if any(c is not None for c in row)
    ]

    return _rows_to_markdown(dict_rows, max_rows, source=path.name)


def from_query_result(
    rows:     list[dict[str, Any]],
    max_rows: int = _DEFAULT_MAX_ROWS,
    label:    str = "Query result",
) -> tuple[str, str]:
    """
    Format a list of row dicts as a markdown table.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        Query result rows.
    max_rows : int
        Maximum rows to include.
    label : str
        Label shown in the header line.

    Returns
    -------
    tuple[str, str]
        (markdown_table, sha256_hex)
    """
    return _rows_to_markdown(rows, max_rows, source=label)


def from_text(path: Path) -> tuple[str, str]:
    """
    Read a plain text or markdown file as-is.

    Parameters
    ----------
    path : Path
        Path to the file.

    Returns
    -------
    tuple[str, str]
        (file_content, sha256_hex)
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return text, _hash(text)


def from_string(text: str) -> tuple[str, str]:
    """
    Hash and return an already-built string.

    Used when the caller constructs the input programmatically rather
    than loading from a file.

    Parameters
    ----------
    text : str
        Input text.

    Returns
    -------
    tuple[str, str]
        (text, sha256_hex)
    """
    return text, _hash(text)


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    """Return the SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rows_to_markdown(
    rows:     list[dict[str, Any]],
    max_rows: int,
    source:   str,
) -> tuple[str, str]:
    """Format a list of row dicts as a markdown table with a source header."""
    if not rows:
        text = f"Source: {source}\n\nTotal rows: 0\n\n(no data)"
        return text, _hash(text)

    total   = len(rows)
    sample  = rows[:max_rows]
    headers = list(sample[0].keys())

    # Markdown table
    sep  = "| " + " | ".join("---" for _ in headers) + " |"
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    body = "\n".join(
        "| " + " | ".join(_cell(row.get(h)) for h in headers) + " |"
        for row in sample
    )

    note = (
        f"\n\n_Showing {len(sample)} of {total} rows._"
        if total > max_rows else
        f"\n\nTotal rows: {total}"
    )

    text = f"Source: {source}\n\n{head}\n{sep}\n{body}{note}"
    return text, _hash(text)


def _cell(value: Any) -> str:
    """Format a single cell value for a markdown table."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
