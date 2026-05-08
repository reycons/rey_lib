"""
Generic field-level parse helpers for CSV row post-processing.

Used by app-specific DB persistence callbacks to convert raw string
values from transformed CSV rows into Python types before inserting
into the database.

These functions treat blank and whitespace-only strings as absent values
and return None rather than raising — matching the common pattern of
optional fields in broker export files.

Public API
----------
parse_iso_date(value)   Parse an ISO-8601 date string to date; None when blank.
parse_float(value)      Parse a float string to float; None when blank.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

__all__ = ["parse_iso_date", "parse_float"]


def parse_iso_date(value: str) -> Optional[date]:
    """
    Parse an ISO-8601 date string to a ``date`` object.

    Returns ``None`` for blank or whitespace-only input rather than raising,
    so optional date fields in CSV rows are handled uniformly.

    Parameters
    ----------
    value : str
        Raw string from a CSV row (e.g. ``"2024-01-15"`` or ``""``).

    Returns
    -------
    Optional[date]
        Parsed date, or ``None`` if the value is blank.

    Raises
    ------
    ValueError
        If the string is non-blank but not a valid ISO-8601 date.
    """
    # Treat blank/whitespace as absent — do not raise.
    text = (value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)


def parse_float(value: str) -> Optional[float]:
    """
    Parse a float string to a ``float``.

    Returns ``None`` for blank or whitespace-only input rather than raising,
    so optional numeric fields in CSV rows are handled uniformly.

    Parameters
    ----------
    value : str
        Raw string from a CSV row (e.g. ``"1234.56"`` or ``""``).

    Returns
    -------
    Optional[float]
        Parsed float, or ``None`` if the value is blank.

    Raises
    ------
    ValueError
        If the string is non-blank but cannot be cast to float.
    """
    # Treat blank/whitespace as absent — do not raise.
    text = (value or "").strip()
    if not text:
        return None
    return float(text)
