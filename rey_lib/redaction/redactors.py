"""
Layout-specific redaction functions.

Each redactor receives parsed rows (or raw lines) and a RedactionRegistry,
applies column-level redaction, and returns the redacted output in the same
structure.  Structural characteristics — delimiters, quoting, field widths,
line endings, blank values — are preserved exactly.

Public API
----------
redact_delimited      Redact named columns in a list of dicts (CSV rows).
redact_fixed_width    Redact character ranges in raw fixed-width lines.
redact_excel_rows     Redact named columns in a list of dicts (Excel rows).
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs import get_logger
from rey_lib.redaction.registry import RedactionRegistry

__all__ = ["redact_delimited", "redact_fixed_width", "redact_excel_rows"]

_logger = get_logger(__name__)


def redact_delimited(
    rows:     list[dict[str, Any]],
    columns:  list[str],
    registry: RedactionRegistry,
) -> list[dict[str, Any]]:
    """Redact named columns in a list of CSV row dicts.

    All other columns are passed through unchanged.  Blank values in a
    redacted column are preserved as blank.  Column names not present in a
    row are silently skipped.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        Parsed rows where each key is a column header.
    columns : list[str]
        Column names to redact.
    registry : RedactionRegistry
        Registry initialised with the same column names.

    Returns
    -------
    list[dict[str, Any]]
        Redacted rows — same structure and ordering as input.
    """
    redact_set = set(columns)
    result: list[dict[str, Any]] = []

    for row in rows:
        new_row: dict[str, Any] = {}
        for key, val in row.items():
            if key in redact_set:
                str_val = str(val) if val is not None else ""
                new_row[key] = registry.redact(key, str_val)
            else:
                new_row[key] = val
        result.append(new_row)

    return result


def redact_fixed_width(
    lines:    list[str],
    ranges:   list[tuple[int, int]],
    registry: RedactionRegistry,
    col_names: list[str],
) -> list[str]:
    """Redact character ranges in raw fixed-width lines.

    Positions are 1-based and inclusive per the contract.  Field width is
    preserved: replacements shorter than the field are right-padded with
    spaces; replacements longer are truncated with a warning logged.

    Parameters
    ----------
    lines : list[str]
        Raw text lines from the fixed-width file (line endings stripped).
    ranges : list[tuple[int, int]]
        List of (start, end) 1-based inclusive character positions to redact.
    registry : RedactionRegistry
        Registry initialised with ``col_names``.
    col_names : list[str]
        Column name for each range — must be same length as ``ranges`` and
        match names registered in ``registry``.

    Returns
    -------
    list[str]
        Redacted lines — same count and structure as input.
    """
    result: list[str] = []

    for line in lines:
        chars = list(line)

        for (start, end), col in zip(ranges, col_names):
            # Convert 1-based inclusive to 0-based Python slice.
            py_start = start - 1
            py_end   = end                          # end is already exclusive in slice

            field_width = end - py_start            # = end - start + 1
            original    = line[py_start:py_end]
            replacement = registry.redact(col, original.rstrip())

            if len(replacement) < field_width:
                replacement = replacement.ljust(field_width)
            elif len(replacement) > field_width:
                _logger.warning(
                    "Fixed-width replacement exceeds field width %d for column '%s' — truncating. "
                    "original=%r replacement=%r",
                    field_width, col, original, replacement,
                )
                replacement = replacement[:field_width]

            for i, ch in enumerate(replacement):
                if py_start + i < len(chars):
                    chars[py_start + i] = ch

        result.append("".join(chars))

    return result


def redact_excel_rows(
    rows:     list[dict[str, Any]],
    columns:  list[str],
    registry: RedactionRegistry,
) -> list[dict[str, Any]]:
    """Redact named columns in Excel rows (identical contract to delimited).

    Excel rows are represented as dicts after reading cell values.  This
    function is a thin alias over :func:`redact_delimited` for clarity at
    the call site.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        Parsed worksheet rows.
    columns : list[str]
        Column names to redact.
    registry : RedactionRegistry
        Registry initialised with the same column names.

    Returns
    -------
    list[dict[str, Any]]
        Redacted rows.
    """
    return redact_delimited(rows, columns, registry)
