"""
File structure profiler.

Produces a structured profile dict describing the layout, column statistics,
inferred types, and representative value samples for a set of rows.  The
profile is layout-agnostic — the caller is responsible for parsing the file
into rows before calling this module.

Public API
----------
profile_rows     Build a profile dict from a list of row dicts.
infer_col_type   Infer the dominant data type for a column's values.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from rey_lib.logs import get_logger

__all__ = ["profile_rows", "infer_col_type", "infer_sql_type"]

_logger = get_logger(__name__)

# Number of representative distinct values to include per column.
_SAMPLE_DISTINCT_VALUES: int = 5


def infer_sql_type(values: list[str]) -> str | None:
    """Return the most precise SQL type for a column's values.

    Uses ``infer_col_type`` for classification then derives precision from
    the observed data so callers avoid the generic ``DECIMAL(20, 8)`` fallback.

    Parameters
    ----------
    values : list[str]
        Non-blank string values from a single column.

    Returns
    -------
    str | None
        ``'INTEGER'``, ``'DECIMAL(p, s)'``, ``'DATE'``, or ``None`` when
        VARCHAR is the appropriate fallback.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return None

    col_type = infer_col_type(non_blank)

    if col_type == "integer":
        return "INTEGER"

    if col_type == "decimal":
        scale     = max((_decimal_places(v) for v in non_blank), default=0)
        int_digs  = max((_integer_digits(v) for v in non_blank), default=1)
        precision = max(int_digs + scale, 1)
        return f"DECIMAL({precision}, {scale})"

    if col_type == "date":
        return "DATE"

    return None


def profile_rows(
    rows:        list[dict[str, Any]],
    source_name: str,
    layout:      str,
    *,
    redacted_columns: list[str] | None = None,
    type_rows:        list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured profile from a list of parsed rows.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        Parsed rows — may be pre-redacted.
    source_name : str
        Original source file name (for metadata).
    layout : str
        One of ``delimited``, ``fixed_width``, ``excel``.
    redacted_columns : list[str] | None
        Column names that were redacted — noted in the profile.
    type_rows : list[dict[str, Any]] | None
        Optional unredacted rows used only for type inference. This keeps
        representative samples redacted while preserving integer/decimal/date
        inference.

    Returns
    -------
    dict[str, Any]
        Structured profile suitable for JSON serialisation.
    """
    redacted_set = set(redacted_columns or [])

    if not rows:
        return {
            "source":          source_name,
            "layout":          layout,
            "row_count":       0,
            "column_count":    0,
            "columns":         [],
            "redacted_columns": list(redacted_set),
        }

    columns = list(rows[0].keys())
    col_profiles: list[dict[str, Any]] = []
    type_source = type_rows or rows

    for col in columns:
        # All profile FACTS are derived from the unredacted ``type_source`` so
        # masking never distorts type, blank counts, or lengths (which drive
        # DDL sizing). Only the displayed ``distinct_sample`` is taken from the
        # redacted ``rows`` so sensitive values are never written to the JSON.
        type_values    = [str(row.get(col, "") or "") for row in type_source]
        display_values = [str(row.get(col, "") or "") for row in rows]
        type_non_blank = [v for v in type_values if v.strip()]
        display_blank  = [v for v in display_values if v.strip()]
        blank_count = len(type_values) - len(type_non_blank)
        lengths     = [len(v) for v in type_non_blank] if type_non_blank else [0]

        col_type = infer_col_type(type_non_blank)
        distinct  = _distinct_sample(display_blank)
        numeric_profile = _numeric_profile(type_non_blank, col_type)

        col_profile = {
            "name":          col,
            "redacted":      col in redacted_set,
            "type":          col_type,
            "row_count":     len(type_values),
            "blank_count":   blank_count,
            "min_length":    min(lengths),
            "max_length":    max(lengths),
            "distinct_sample": distinct,
        }
        col_profile.update(numeric_profile)
        col_profiles.append(col_profile)

    return {
        "source":           source_name,
        "layout":           layout,
        "row_count":        len(rows),
        "column_count":     len(columns),
        "columns":          col_profiles,
        "redacted_columns": list(redacted_set),
    }


def infer_col_type(values: list[str]) -> str:
    """Infer the dominant data type from a list of non-blank string values.

    Returns one of: ``integer``, ``decimal``, ``date``, ``datetime``,
    ``boolean``, ``text``.  Applies majority voting — a column is typed by
    whichever type matches more than 80% of non-blank values.  Falls back to
    ``text`` when no type dominates.

    Parameters
    ----------
    values : list[str]
        Non-blank string values from a single column.

    Returns
    -------
    str
        Inferred type label.
    """
    if not values:
        return "text"

    checkers = [
        ("integer",  _is_integer),
        ("decimal",  _is_decimal),
        ("date",     _is_date),
        ("datetime", _is_datetime),
        ("boolean",  _is_boolean),
    ]

    threshold = 0.8
    total     = len(values)

    for type_name, checker in checkers:
        matches = sum(1 for v in values if checker(v))
        if matches / total >= threshold:
            return type_name

    return "text"


# ---------------------------------------------------------------------------
# Private — type checkers
# ---------------------------------------------------------------------------

def _is_integer(value: str) -> bool:
    """Return True if value is a whole number (optionally signed)."""
    v = value.strip().lstrip("+-")
    return v.isdigit() and bool(v)


def _is_decimal(value: str) -> bool:
    """Return True if value is a decimal number."""
    v = value.strip().lstrip("+-").replace(",", "")
    parts = v.split(".")
    if len(parts) == 2:
        return parts[0].isdigit() and parts[1].isdigit()
    return False


def _is_date(value: str) -> bool:
    """Return True if value looks like a date string (no time component)."""
    import re
    patterns = [
        r"^\d{4}-\d{2}-\d{2}$",            # yyyy-MM-dd
        r"^\d{2}/\d{2}/\d{4}$",            # MM/dd/yyyy
        r"^\d{2}/\d{2}/\d{2}$",            # MM/dd/yy
        r"^\d{8}$",                          # yyyyMMdd
        r"^\d{1,2}-[A-Za-z]{3}-\d{4}$",    # d-MMM-yyyy
        r"^\d{1,2} [A-Za-z]{3} \d{4}$",    # d MMM yyyy
    ]
    return any(re.match(p, value.strip()) for p in patterns)


def _is_datetime(value: str) -> bool:
    """Return True if value looks like a datetime string."""
    import re
    patterns = [
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",
        r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}",
    ]
    return any(re.match(p, value.strip()) for p in patterns)


def _is_boolean(value: str) -> bool:
    """Return True if value is a common boolean representation."""
    return value.strip().lower() in {"true", "false", "yes", "no", "y", "n", "1", "0", "t", "f"}


def _distinct_sample(values: list[str]) -> list[str]:
    """Return up to N distinct non-blank values preserving first-seen order."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for v in values:
        if v not in seen_set:
            seen.append(v)
            seen_set.add(v)
        if len(seen) >= _SAMPLE_DISTINCT_VALUES:
            break
    return seen


def _numeric_profile(values: list[str], col_type: str) -> dict[str, Any]:
    """Return precision metadata for integer and decimal columns."""
    if col_type not in {"integer", "decimal"}:
        return {}

    numeric_values = [v for v in values if v.strip()]
    decimal_places = [_decimal_places(v) for v in numeric_values]
    integer_digits = [_integer_digits(v) for v in numeric_values]
    digit_counts = Counter(integer_digits)

    return {
        "min_decimal_places": min(decimal_places) if decimal_places else 0,
        "max_decimal_places": max(decimal_places) if decimal_places else 0,
        "min_integer_digits": min(integer_digits) if integer_digits else 0,
        "max_integer_digits": max(integer_digits) if integer_digits else 0,
        "common_integer_digits": _most_common_integer_digits(digit_counts),
        "integer_digit_counts": {
            str(digits): count for digits, count in sorted(digit_counts.items())
        },
        "has_leading_zero": any(_has_leading_zero(v) for v in numeric_values),
        "has_negative": any(_has_negative(v) for v in numeric_values),
    }


def _most_common_integer_digits(digit_counts: Counter[int]) -> int:
    """Return the most common integer digit width, preferring the wider tie."""
    if not digit_counts:
        return 0
    return max(digit_counts.items(), key=lambda item: (item[1], item[0]))[0]


def _decimal_places(value: str) -> int:
    """Return the number of digits after the decimal point."""
    cleaned = value.strip()
    if "." not in cleaned:
        return 0
    return len(cleaned.rsplit(".", 1)[1])


def _integer_digits(value: str) -> int:
    """Return digit count before the decimal point, ignoring sign and commas."""
    cleaned = value.strip().lstrip("+-")
    whole = cleaned.split(".", 1)[0]
    return sum(1 for ch in whole if ch.isdigit())


def _has_leading_zero(value: str) -> bool:
    """Return True when the whole-number part starts with a meaningful zero."""
    whole_digits = _whole_digits(value)
    return len(whole_digits) > 1 and whole_digits.startswith("0")


def _has_negative(value: str) -> bool:
    """Return True when the numeric value is explicitly negative."""
    return value.strip().startswith("-")


def _whole_digits(value: str) -> str:
    """Return digits before the decimal point, ignoring sign and separators."""
    cleaned = value.strip().lstrip("+-")
    whole = cleaned.split(".", 1)[0]
    return "".join(ch for ch in whole if ch.isdigit())
