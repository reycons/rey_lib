"""
Strict generic JSONL file reading for rey_lib.

Reads a JSONL file sequentially, preserving each record's physical one-based
line number, with optional exact-value row filtering and optional field
projection (SGC_Shared_Strict_JSONL_File_Reader).

This reader is deliberately strict and is *not* the display-oriented projection
in ``rey_lib.logs.evidence_projection.read_jsonl_records``. That one is supplied
file content by its caller, tolerates malformed lines by collecting them, and
truncates to a record limit — all correct for a viewer. This one opens the file
itself, fails on the first malformed line, returns no partial result, and has no
record limit, because its callers use the result for control flow rather than
display.

It knows nothing about logs, evidence, manifests, lifecycle records, pipelines,
workflows, or any application's record shapes, and it is not an authorization
boundary: the caller resolves and authorizes the path before calling.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jmespath
from jmespath.exceptions import JMESPathError

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import open_text_file

__all__ = [
    "JsonlReadError",
    "JsonlRecord",
    "JsonlSearchResult",
    "read_jsonl_file",
    "search_jsonl_file",
]


class JsonlReadError(AppError):
    """Raised when a JSONL file cannot be read or contains invalid JSONL."""


@dataclass(frozen=True)
class JsonlRecord:
    """One parsed JSONL row and the physical line it came from."""

    line_number: int
    record: Mapping[str, Any]


@dataclass(frozen=True)
class JsonlSearchResult:
    """One JMESPath result and the physical JSONL line it came from."""

    line_number: int
    value: Any


def read_jsonl_file(
    path: Path | str,
    *,
    filters: Mapping[str, Any] | None = None,
    fields: Sequence[str] | None = None,
) -> list[JsonlRecord]:
    """
    Read a JSONL file strictly, in physical file order.

    Blank and whitespace-only lines are skipped but still advance the physical
    line number, so a returned ``line_number`` always addresses the row in the
    file. Filtering and projection never reorder or renumber records.

    Parameters
    ----------
    path : Path | str
        Resolved filesystem path to an existing regular file. The caller is
        responsible for resolving installation tokens and authorizing the path.
    filters : Mapping[str, Any] | None
        Exact top-level field equality. Every entry must match for a record to
        be returned; a record missing a filtered field never matches. Booleans
        and numbers are compared with their JSON types kept distinct, so
        ``True`` never matches ``1``. An empty or omitted mapping filters
        nothing.
    fields : Sequence[str] | None
        Top-level field names to project, in the requested order. A requested
        field the record does not have is omitted rather than inserted as null.
        Omitted entirely, the complete parsed object is returned.

    Returns
    -------
    list[JsonlRecord]
        Matching records in physical file order.

    Raises
    ------
    JsonlReadError
        If the arguments are invalid, the path is missing, is not a regular
        file, cannot be read, or the file contains a line that is not exactly
        one JSON object. Parsing stops at the first invalid line and no partial
        result is returned.
    """
    file_path = _validated_path(path)
    selected = _validated_filters(filters)
    projection = _validated_fields(fields)

    records: list[JsonlRecord] = []
    for line_number, parsed in _iter_jsonl_objects(file_path):
        if not _matches(parsed, selected):
            continue
        records.append(
            JsonlRecord(
                line_number=line_number,
                record=_project(parsed, projection),
            )
        )

    return records


def search_jsonl_file(
    path: Path | str,
    expression: str,
) -> list[JsonlSearchResult]:
    """
    Apply one standard JMESPath expression independently to every JSONL record.

    The expression is compiled once before the path is opened. Every parsed
    nonblank record contributes exactly one result, including results equal to
    ``False``, ``0``, an empty string, an empty collection, or ``None``.
    Results retain the source record's physical one-based line number and file
    order.

    The caller remains responsible for resolving installation tokens and
    authorizing the supplied path.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise JsonlReadError("JMESPath expression must be a non-empty string.")
    try:
        compiled = jmespath.compile(expression)
    except JMESPathError as exc:
        raise JsonlReadError(
            f"Invalid JMESPath expression '{expression}': {exc}"
        ) from exc

    file_path = _validated_path(path)
    results: list[JsonlSearchResult] = []
    for line_number, parsed in _iter_jsonl_objects(file_path):
        results.append(
            JsonlSearchResult(
                line_number=line_number,
                value=compiled.search(parsed),
            )
        )
    return results


def _iter_jsonl_objects(file_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield strict JSON objects with their physical one-based line numbers."""
    try:
        with open_text_file(file_path) as handle:
            # Iterating the handle streams the file, so the complete raw
            # content is never held in memory at once.
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                yield (
                    line_number,
                    _parse_line(file_path, line_number, line),
                )
    except OSError as exc:
        raise JsonlReadError(f"Cannot read JSONL file '{file_path}': {exc}") from exc


def _validated_path(path: Path | str) -> Path:
    """Return the path as an existing regular file, or fail."""
    if not isinstance(path, (Path, str)):
        raise JsonlReadError(
            f"JSONL path must be a Path or str, not {type(path).__name__}."
        )
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise JsonlReadError(f"JSONL file does not exist: '{file_path}'.")
    if not file_path.is_file():
        raise JsonlReadError(f"JSONL path is not a regular file: '{file_path}'.")
    return file_path


def _validated_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the validated exact-match filters, empty when none were supplied."""
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise JsonlReadError(
            f"JSONL filters must be a mapping, not {type(filters).__name__}."
        )
    for key in filters:
        if not isinstance(key, str) or not key:
            raise JsonlReadError("JSONL filter keys must be non-empty strings.")
    return dict(filters)


def _validated_fields(fields: Sequence[str] | None) -> tuple[str, ...] | None:
    """Return the validated projection field names, or None for no projection."""
    if fields is None:
        return None
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise JsonlReadError(
            f"JSONL fields must be a sequence of names, not {type(fields).__name__}."
        )
    seen: set[str] = set()
    for name in fields:
        if not isinstance(name, str) or not name:
            raise JsonlReadError("JSONL field names must be non-empty strings.")
        if name in seen:
            raise JsonlReadError(f"JSONL field name is requested twice: '{name}'.")
        seen.add(name)
    return tuple(fields)


def _parse_line(file_path: Path, line_number: int, line: str) -> dict[str, Any]:
    """Parse exactly one JSON object from one physical line, or fail."""
    try:
        # json.loads rejects trailing content, so two JSON values on one line
        # fail here rather than silently yielding the first.
        parsed = json.loads(line)
    except ValueError as exc:
        raise JsonlReadError(
            f"Invalid JSONL in '{file_path}' at line {line_number}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise JsonlReadError(
            f"Invalid JSONL in '{file_path}' at line {line_number}: expected a "
            f"JSON object, found {type(parsed).__name__}."
        )
    return parsed


def _matches(record: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """Return whether the record satisfies every exact-value filter."""
    for key, expected in filters.items():
        if key not in record:
            return False
        if not _equal(record[key], expected):
            return False
    return True


def _equal(actual: Any, expected: Any) -> bool:
    """Compare two JSON values, keeping booleans distinct from numbers."""
    # Python treats True == 1, which would let a boolean filter match an
    # integer field and the reverse. JSON keeps them distinct, so this does too.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual is expected
    return actual == expected


def _project(
    record: dict[str, Any],
    fields: tuple[str, ...] | None,
) -> Mapping[str, Any]:
    """Return the requested fields in requested order, or the whole object."""
    if fields is None:
        return record
    # A requested field the record does not carry is omitted rather than
    # inserted as null, so absence stays distinguishable from a null value.
    return {name: record[name] for name in fields if name in record}
