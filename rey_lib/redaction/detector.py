"""
Column PII type detector for file_operator.

Samples values from a column and returns the mask type that best describes
the data.  Detection is regex-based with a match-rate threshold — at least
70 % of non-empty sampled values must match a pattern for that type to win.
Patterns are evaluated in priority order; the first type to reach the
threshold is returned.  Unrecognised columns fall back to ``"text"``.

Public API
----------
SAMPLE_SIZE     Number of non-empty values used per column.
detect_mask_type    Infer the mask type for one column from sampled values.
"""

from __future__ import annotations

import re

__all__: list[str] = ["SAMPLE_SIZE", "detect_mask_type"]

SAMPLE_SIZE: int = 100
_THRESHOLD:  float = 0.70


# ---------------------------------------------------------------------------
# Patterns — evaluated in priority order
# ---------------------------------------------------------------------------
# Each entry is (mask_type, compiled_pattern).  Use fullmatch so the entire
# value must match, not just a substring.

_RULES: list[tuple[str, re.Pattern[str]]] = [
    # email — has @ with domain; checked first because it's unambiguous
    ("email",   re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")),

    # SSN — ddd-dd-dddd or 9-digit run
    ("ssn",     re.compile(r"\d{3}-\d{2}-\d{4}|\d{9}")),

    # phone — common North American formats
    ("phone",   re.compile(
        r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4}"
        r"|\d{10}"
        r"|\+?1[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4}"
    )),

    # date — ISO, US, and common separator variants
    ("date",    re.compile(
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"     # YYYY-MM-DD
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"  # MM/DD/YY or MM/DD/YYYY
    )),

    # decimal — optional sign, commas, and required fractional component
    ("decimal", re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d+")),

    # integer — optional sign and commas
    ("integer", re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)")),

    # ZIP — 5-digit or ZIP+4. Explicit redact_masks can still request zip.
    ("zip",     re.compile(r"\d{5}(-\d{4})?")),

    # account — 8-20 consecutive digits. Explicit redact_masks can still request account.
    ("account", re.compile(r"\d{8,20}")),

    # name — 1-4 title-case words (first/last name or full name)
    ("name",    re.compile(r"[A-Z][a-zA-Z'\-]+([ ][A-Z][a-zA-Z'\-]+){0,3}")),
]


def detect_mask_type(values: list[str]) -> str:
    """Infer the mask type that best describes a column's values.

    Samples up to ``SAMPLE_SIZE`` non-empty values.  The first mask type
    whose pattern matches at least ``_THRESHOLD`` of the sample wins.  If no
    type reaches the threshold the column is classified as ``"text"``.

    Parameters
    ----------
    values : list[str]
        Raw string values from a single column (may include blanks).

    Returns
    -------
    str
        One of the mask type strings from ``KNOWN_MASKS``, or ``"text"``
        when no specific type is detected.
    """
    samples = [v.strip() for v in values if v and v.strip()][:SAMPLE_SIZE]
    if not samples:
        return "text"

    n = len(samples)
    for mask_type, pattern in _RULES:
        hit_count = sum(1 for v in samples if pattern.fullmatch(v))
        if hit_count / n >= _THRESHOLD:
            return mask_type

    return "text"
