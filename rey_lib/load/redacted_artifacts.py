"""Content of redacted companion artifacts.

A configured output folder opts in with ``redacted_copy: true``. This module
owns only what a companion *contains*: values replaced through the shared
governed redaction boundary, with structure preserved. No redaction rule is
decided here and no model is consulted.

Naming and publication belong to the publication primitive
(:func:`rey_lib.files.redacted_companion_path` and
:func:`rey_lib.files.publish_file_set`), so every producer gets one naming rule
and one atomicity contract rather than inventing its own.

Copied from the legacy file_operator module (row 589, step 4); the legacy
module stays untouched as the reference.
"""

from __future__ import annotations

from typing import Sequence

from rey_lib.files.csv import render_delimited_line
from rey_lib.redaction.redactors import redact_delimited
from rey_lib.redaction.registry import RedactionRegistry

__all__ = ["redacted_csv_text"]


def redacted_csv_text(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    delimiter: str,
) -> str:
    """Render the same CSV with only its field values redacted.

    The header, the column order, the row order, and the row and column counts
    are those of the original; the shared governed boundary replaces values and
    nothing else.
    """
    if not columns:
        # No header was located, so there is no header line to write and no
        # column names to key by. The rows are still data and are still
        # redacted, by position. Writing nothing here would silently discard
        # every value in the file while reporting success.
        return _redacted_positional_text(rows, delimiter)

    # A repeated header is the header, not data. Redacting it rewrites the
    # column names in one occurrence and not the other, so the two stop
    # matching and the file appears to carry two different headers — which is
    # exactly what header detection must not see.
    identity = tuple(name.strip() for name in columns)
    # A row may carry more fields than the header names — a short header, or a
    # ragged export. Those columns are still data and are still redacted, keyed
    # by position past the named ones. Writing only as many values as the
    # header has names drops the rest of every row silently.
    width = max([len(columns), *(len(fields) for fields in rows)], default=0)
    keys = [
        columns[index] if index < len(columns) else f"column_{index}"
        for index in range(width)
    ]
    registry = RedactionRegistry(keys)
    mapped = [
        {
            key: fields[index] if index < len(fields) else ""
            for index, key in enumerate(keys)
        }
        for fields in rows
    ]
    redacted = redact_delimited(mapped, keys, registry)
    lines = [render_delimited_line(columns, delimiter)]
    for position, fields in enumerate(rows):
        if _is_repeated_header(fields, identity):
            lines.append(render_delimited_line(list(fields), delimiter))
            continue
        lines.append(render_delimited_line(
            [redacted[position][key] for key in keys[:len(fields)]],
            delimiter,
        ))
    return "".join(line + "\n" for line in lines)


def _is_repeated_header(
    fields: Sequence[str],
    identity: tuple[str, ...],
) -> bool:
    """Whether one row is the header written again.

    Compared on stripped values so a repeat differing only in spacing is still
    recognised as the same header.
    """
    stripped = tuple(str(value).strip() for value in fields)
    while stripped and not stripped[-1]:
        stripped = stripped[:-1]
    trimmed = identity
    while trimmed and not trimmed[-1]:
        trimmed = trimmed[:-1]
    return bool(trimmed) and stripped == trimmed


def _redacted_positional_text(
    rows: Sequence[Sequence[str]],
    delimiter: str,
) -> str:
    """Redact rows that have no header, keyed by column position."""
    width = max((len(fields) for fields in rows), default=0)
    if not width:
        return ""
    keys = [f"column_{position}" for position in range(width)]
    registry = RedactionRegistry(keys)
    mapped = [
        {
            keys[position]: fields[position] if position < len(fields) else ""
            for position in range(width)
        }
        for fields in rows
    ]
    redacted = redact_delimited(mapped, keys, registry)
    return "".join(
        render_delimited_line(
            [redacted[position][keys[index]] for index in range(len(fields))],
            delimiter,
        ) + "\n"
        for position, fields in enumerate(rows)
    )
