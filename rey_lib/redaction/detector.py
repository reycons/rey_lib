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

from collections import Counter

__all__: list[str] = ["SAMPLE_SIZE", "detect_mask_type"]

SAMPLE_SIZE: int = 100
_THRESHOLD:  float = 0.70


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
        A *proposed* profiling datatype, or ``"text"`` when no specific type
        is detected.

    Notes
    -----
    This answers *what kind of data is this*, in profiling's vocabulary. That
    is not the masking vocabulary and is not required to agree with it:
    ``detect_datatype`` also returns ``alpha``, ``alphanumeric``, ``boolean``
    and ``blank``, none of which names a mask.

    The proposal is resolved on the masking side, which honours it only when
    ``KNOWN_MASKS`` holds it and uses generic redaction otherwise. An
    unresolvable proposal therefore means generic redaction -- never no
    redaction.
    """
    from rey_lib.profiling.file_profiler import detect_datatype

    samples = [v.strip() for v in values if v and v.strip()][:SAMPLE_SIZE]
    if not samples:
        return "text"

    n = len(samples)
    counts = Counter(detect_datatype(value) for value in samples)
    datatype, hits = counts.most_common(1)[0]
    if hits / n >= _THRESHOLD:
        return datatype

    return "text"
