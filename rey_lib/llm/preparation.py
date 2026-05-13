"""
Contract-driven data preparation pipeline.

Transforms raw SourceData into prompt-ready PreparedInput via a deterministic
sequence of stages:

  1. Column filtering  — restrict rows to ``allowed_columns`` (empty = all)
  2. Row filtering     — apply ``required_filters`` as Python-level predicates
  3. Sampling          — reduce to ``max_rows`` via configured strategy
  4. Column redaction  — mask sensitive column values in-place
  5. Profiling         — compute statistics for the audit record
  6. Rendering         — serialise to prompt-ready markdown text

Text sources (``SourceData.raw_text`` populated) skip stages 1–4 and only
apply text-level redaction via ``RedactionFilter`` if one is supplied.

Supported sampling methods
--------------------------
head      First N rows (deterministic, default).
tail      Last N rows (deterministic).
random    Random sample.  Requires ``sampling_seed`` for reproducibility.

Supported filter operators
--------------------------
==  eq     Equality.
!=  ne     Inequality.
>   gt     Greater than.
>=  gte    Greater than or equal.
<   lt     Less than.
<=  lte    Less than or equal.
in         Value is in a list.
not_in     Value is not in a list.

Public API
----------
DataProfile
    Basic statistics computed after sampling — stored in the audit record.
PreparedInput
    Result of the full preparation pipeline.
prepare(source_data, ...)
    Run all preparation stages and return a PreparedInput.
"""

from __future__ import annotations

import hashlib
import random as _random
from dataclasses import dataclass, field
from typing import Any, Optional

from rey_lib.llm.datasource import SourceData

__all__ = ["DataProfile", "PreparedInput", "prepare"]

# Map of operator strings to comparison callables.
_OPS: dict[str, Any] = {
    "==":     lambda a, b: a == b,
    "eq":     lambda a, b: a == b,
    "!=":     lambda a, b: a != b,
    "ne":     lambda a, b: a != b,
    ">":      lambda a, b: a > b,
    "gt":     lambda a, b: a > b,
    ">=":     lambda a, b: a >= b,
    "gte":    lambda a, b: a >= b,
    "<":      lambda a, b: a < b,
    "lt":     lambda a, b: a < b,
    "<=":     lambda a, b: a <= b,
    "lte":    lambda a, b: a <= b,
    "in":     lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


@dataclass(frozen=True)
class DataProfile:
    """Basic statistics about the data seen by the LLM.

    Computed from the sampled, redacted rows — reflects what was actually
    sent to the provider, not the raw source.

    Attributes
    ----------
    columns : list[str]
        Column names included in the prepared input.
    rows_extracted : int
        Rows returned by the DataSource before any preparation.
    rows_after_filter : int
        Rows remaining after required_filters were applied.
    rows_sampled : int
        Rows sent to the LLM (after sampling).
    truncated : bool
        True when the source or sampling reduced the row count.
    null_counts : dict[str, int]
        Number of null/None values per column in the sampled rows.
    columns_redacted : list[str]
        Column names that had redaction rules applied.
    sampling_method : str
        Sampling strategy applied (head, tail, random).
    """

    columns:           list[str]
    rows_extracted:    int
    rows_after_filter: int
    rows_sampled:      int
    truncated:         bool
    null_counts:       dict[str, int]
    columns_redacted:  list[str]
    sampling_method:   str


@dataclass(frozen=True)
class PreparedInput:
    """Result of the full data preparation pipeline.

    Attributes
    ----------
    rendered_text : str
        Prompt-ready text representation of the data.
    input_hash : str
        SHA-256 of ``rendered_text`` — stored in the execution record.
    profile : DataProfile
        Statistics about what the LLM will see.
    source_ref : str
        Human-readable label from the originating DataSource.
    source_hash : str
        SHA-256 of the raw extracted data — unchanged by preparation.
    """

    rendered_text: str
    input_hash:    str
    profile:       DataProfile
    source_ref:    str
    source_hash:   str


def prepare(
    source_data:      SourceData,
    *,
    allowed_columns:  list[str]              = (),
    required_filters: list[dict[str, Any]]   = (),
    max_rows:         int                    = 200,
    sampling_method:  str                    = "head",
    sampling_seed:    Optional[int]          = None,
    redaction_rules:  list[dict[str, str]]   = (),
) -> PreparedInput:
    """Run all preparation stages on extracted source data.

    Parameters
    ----------
    source_data : SourceData
        Raw data returned by a DataSource.
    allowed_columns : list[str]
        Columns permitted to reach the LLM.  Empty list means all columns.
    required_filters : list[dict[str, Any]]
        Row-level predicates.  Each dict must have ``column``, ``operator``,
        and ``value`` keys.  Applied as Python comparisons — not SQL.
    max_rows : int
        Maximum rows sent to the LLM.  Sampling reduces to this count.
    sampling_method : str
        Row selection strategy: ``head``, ``tail``, or ``random``.
    sampling_seed : Optional[int]
        Random seed for ``random`` sampling.  None = non-deterministic.
    redaction_rules : list[dict[str, str]]
        Column-level masking rules.  Each dict has ``column`` and ``mask``.

    Returns
    -------
    PreparedInput
        Rendered text, hash, and full preparation metadata.
    """
    # Text sources skip all tabular stages.
    if source_data.raw_text:
        return _prepare_text(source_data, redaction_rules)

    rows = list(source_data.rows)
    rows_extracted = len(rows)

    rows = _filter_columns(rows, list(allowed_columns))
    rows = _apply_filters(rows, list(required_filters))
    rows_after_filter = len(rows)

    rows, effective_method = _sample(rows, max_rows, sampling_method, sampling_seed)
    rows_sampled  = len(rows)
    was_truncated = source_data.truncated or (rows_after_filter > max_rows)

    null_counts       = _count_nulls(rows)
    cols_used         = list(rows[0].keys()) if rows else list(source_data.columns)
    redacted_cols     = _apply_redaction(rows, list(redaction_rules))

    rendered   = _render_tabular(rows, source_ref=source_data.source_ref, truncated=was_truncated)
    input_hash = _sha256(rendered)

    profile = DataProfile(
        columns           = cols_used,
        rows_extracted    = rows_extracted,
        rows_after_filter = rows_after_filter,
        rows_sampled      = rows_sampled,
        truncated         = was_truncated,
        null_counts       = null_counts,
        columns_redacted  = redacted_cols,
        sampling_method   = effective_method,
    )

    return PreparedInput(
        rendered_text = rendered,
        input_hash    = input_hash,
        profile       = profile,
        source_ref    = source_data.source_ref,
        source_hash   = source_data.source_hash,
    )


# ---------------------------------------------------------------------------
# Private — tabular stages
# ---------------------------------------------------------------------------

def _filter_columns(
    rows:            list[dict[str, Any]],
    allowed_columns: list[str],
) -> list[dict[str, Any]]:
    """Drop columns not in ``allowed_columns``.  No-op when list is empty."""
    if not allowed_columns:
        return rows
    allowed = set(allowed_columns)
    return [{k: v for k, v in row.items() if k in allowed} for row in rows]


def _apply_filters(
    rows:    list[dict[str, Any]],
    filters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only rows that satisfy all required_filters."""
    if not filters:
        return rows

    result: list[dict[str, Any]] = []
    for row in rows:
        if _row_passes(row, filters):
            result.append(row)
    return result


def _row_passes(row: dict[str, Any], filters: list[dict[str, Any]]) -> bool:
    """Return True if a row satisfies every filter predicate."""
    for f in filters:
        col      = f.get("column", "")
        operator = str(f.get("operator", "=="))
        value    = f.get("value")
        compare  = _OPS.get(operator)
        if compare is None:
            continue  # Unknown operator — skip rather than block all rows.
        cell = row.get(col)
        try:
            if not compare(cell, value):
                return False
        except TypeError:
            return False
    return True


def _sample(
    rows:   list[dict[str, Any]],
    n:      int,
    method: str,
    seed:   Optional[int],
) -> tuple[list[dict[str, Any]], str]:
    """Reduce rows to at most ``n`` using the requested strategy.

    Returns the sampled rows and the effective method name.
    """
    if len(rows) <= n:
        return rows, method

    if method == "tail":
        return rows[-n:], "tail"

    if method == "random":
        rng = _random.Random(seed)
        return rng.sample(rows, n), "random"

    # "head" is the safe default.
    return rows[:n], "head"


def _apply_redaction(
    rows:  list[dict[str, Any]],
    rules: list[dict[str, str]],
) -> list[str]:
    """Mask column values in-place.  Returns list of redacted column names."""
    if not rules:
        return []

    redacted: list[str] = []
    for rule in rules:
        col  = rule.get("column", "")
        mask = rule.get("mask", "[REDACTED]")
        if not col:
            continue
        redacted.append(col)
        for row in rows:
            if col in row:
                row[col] = mask

    return redacted


def _count_nulls(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count None/null values per column across sampled rows."""
    counts: dict[str, int] = {}
    for row in rows:
        for col, val in row.items():
            if val is None:
                counts[col] = counts.get(col, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Private — rendering
# ---------------------------------------------------------------------------

def _render_tabular(
    rows:       list[dict[str, Any]],
    source_ref: str,
    truncated:  bool,
) -> str:
    """Render sampled rows as a markdown table with a source header."""
    if not rows:
        return f"Source: {source_ref}\n\nTotal rows: 0\n\n(no data)"

    headers = list(rows[0].keys())
    head    = "| " + " | ".join(str(h) for h in headers) + " |"
    sep     = "| " + " | ".join("---" for _ in headers) + " |"
    body    = "\n".join(
        "| " + " | ".join(_cell(row.get(h)) for h in headers) + " |"
        for row in rows
    )
    note = (
        f"\n\n_Showing {len(rows)} rows (source has more — truncated)._"
        if truncated
        else f"\n\nTotal rows: {len(rows)}"
    )
    return f"Source: {source_ref}\n\n{head}\n{sep}\n{body}{note}"


def _cell(value: Any) -> str:
    """Format a single cell value for a markdown table."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _prepare_text(
    source_data:     SourceData,
    redaction_rules: list[dict[str, str]],
) -> PreparedInput:
    """Prepare a text (non-tabular) source."""
    text = source_data.raw_text

    # Apply any text-level redaction rules using the mask as a literal replacement.
    for rule in redaction_rules:
        col  = rule.get("column", "")
        mask = rule.get("mask", "[REDACTED]")
        if col:
            text = text.replace(col, mask)

    input_hash = _sha256(text)
    profile    = DataProfile(
        columns           = [],
        rows_extracted    = 0,
        rows_after_filter = 0,
        rows_sampled      = 0,
        truncated         = False,
        null_counts       = {},
        columns_redacted  = [],
        sampling_method   = "n/a",
    )
    return PreparedInput(
        rendered_text = text,
        input_hash    = input_hash,
        profile       = profile,
        source_ref    = source_data.source_ref,
        source_hash   = source_data.source_hash,
    )


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
