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

    Reads only up to max_rows data rows — the file is not fully loaded into
    memory regardless of size.

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
    rows: list[dict[str, Any]] = []
    truncated = False
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(dict(row))
    return _rows_to_markdown(rows, source=path.name, truncated=truncated)


def from_excel(
    path:     Path,
    sheet:    Optional[str] = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> tuple[str, str]:
    """
    Load an Excel file and format it as a markdown table.

    Iterates rows lazily using openpyxl read-only mode — the full sheet is
    not materialised in memory.  Reads up to max_rows data rows after the
    detected header.  Pre-header rows (metadata, blank rows) are buffered
    only until the header is found.

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

    headers:        Optional[list[str]]         = None
    pre_header_buf: list[tuple]                 = []
    rows:           list[dict[str, Any]]        = []
    truncated = False

    for raw_row in ws.iter_rows(values_only=True):
        if headers is None:
            pre_header_buf.append(raw_row)
            # Proper header: every non-None cell is a non-empty string.
            if raw_row and all(
                isinstance(c, str) and c.strip()
                for c in raw_row
                if c is not None
            ):
                headers = [str(c).strip() if c is not None else "" for c in raw_row]
        else:
            if any(c is not None for c in raw_row):
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(
                    {headers[j]: (raw_row[j] if j < len(raw_row) else None)
                     for j in range(len(headers))}
                )

    wb.close()

    # Fallback: use first non-blank row in the pre-header buffer as the header,
    # then collect the remaining buffered rows as data.
    if headers is None:
        for i, buf_row in enumerate(pre_header_buf):
            if any(c is not None for c in buf_row):
                headers = [str(c).strip() if c is not None else "" for c in buf_row]
                for data_row in pre_header_buf[i + 1:]:
                    if any(c is not None for c in data_row):
                        if len(rows) >= max_rows:
                            truncated = True
                            break
                        rows.append(
                            {headers[j]: (data_row[j] if j < len(data_row) else None)
                             for j in range(len(headers))}
                        )
                break

    if headers is None:
        text = f"Source: {path.name}\n\n(empty sheet)"
        return text, _hash(text)

    return _rows_to_markdown(rows, source=path.name, truncated=truncated)


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
        Query result rows. Sliced to max_rows before formatting.
    max_rows : int
        Maximum rows to include.
    label : str
        Label shown in the header line.

    Returns
    -------
    tuple[str, str]
        (markdown_table, sha256_hex)
    """
    truncated = len(rows) > max_rows
    return _rows_to_markdown(rows[:max_rows], source=label, truncated=truncated)


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
    rows:      list[dict[str, Any]],
    source:    str,
    truncated: bool = False,
) -> tuple[str, str]:
    """Format a pre-sliced list of row dicts as a markdown table with a source header.

    Callers are responsible for slicing to max_rows before calling.
    truncated=True appends a note indicating the source was cut short.
    """
    if not rows:
        text = f"Source: {source}\n\nTotal rows: 0\n\n(no data)"
        return text, _hash(text)

    headers = list(rows[0].keys())
    sep  = "| " + " | ".join("---" for _ in headers) + " |"
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    body = "\n".join(
        "| " + " | ".join(_cell(row.get(h)) for h in headers) + " |"
        for row in rows
    )
    note = (
        f"\n\n_Showing {len(rows)} rows (truncated — source has more)._"
        if truncated else
        f"\n\nTotal rows: {len(rows)}"
    )
    text = f"Source: {source}\n\n{head}\n{sep}\n{body}{note}"
    return text, _hash(text)


def _cell(value: Any) -> str:
    """Format a single cell value for a markdown table."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
