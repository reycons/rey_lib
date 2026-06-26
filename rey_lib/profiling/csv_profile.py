"""
CSV profile enrichment for LLM artifact generation.

Takes the base profile produced by ``rey_lib.profiling.file_profiler.profile_rows``
and enriches it with deterministic, Python-detected facts so downstream LLM
steps (staging/final DDL, ``rey_loader`` YAML) have better facts and guess less.

Enrichment is purely additive: every existing profile key is preserved so the
current DDL/loader contracts keep working. The new information includes a
``profile_version``, a structured ``csv`` section, per-column hints (ordinal,
normalised/safe SQL names, ``type_hint``, value-pattern hints), and the
``loader_hints`` and ``llm_hints`` sections.

CSV-only by design — fixed-width and Excel are out of scope and are not touched.
This module performs no I/O and no LLM calls; pattern detection lives in
``rey_lib.profiling.value_patterns``.

Public API
----------
enrich_csv_profile   Return a CSV-enriched copy of a base profile.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from rey_lib.logs import get_logger
from rey_lib.profiling import value_patterns as vp

__all__ = ["enrich_csv_profile", "PROFILE_VERSION"]

_logger = get_logger(__name__)

PROFILE_VERSION = "csv_v1"

# Type hints the profile is allowed to assert (advisory only).
_ALLOWED_TYPE_HINTS = frozenset(
    {"text", "integer", "decimal", "date", "datetime", "boolean", "unknown"}
)

_DEFAULT_MAX_SAMPLE_VALUES = 10
_STAGING_SCHEMA = "trade_analysis_staging"


def enrich_csv_profile(
    base_profile: dict[str, Any],
    redacted_rows: list[dict[str, Any]],
    original_rows: list[dict[str, Any]],
    *,
    source_file: str,
    encoding: str,
    delimiter: str,
    has_header: bool = True,
    quote_char: str = '"',
    blank_line_count: int = 0,
    ragged_row_count: int = 0,
    max_sample_values: int = _DEFAULT_MAX_SAMPLE_VALUES,
) -> dict[str, Any]:
    """Return a CSV-enriched copy of ``base_profile``.

    Parameters
    ----------
    base_profile : dict[str, Any]
        Profile from ``profile_rows`` (already updated with delimiter/encoding/
        header by the layout handler).
    redacted_rows : list[dict[str, Any]]
        Sampled, redacted row dicts — used for sample values (never exposes
        redacted sensitive values beyond what redaction already produced).
    original_rows : list[dict[str, Any]]
        Sampled, unredacted row dicts — used for type/pattern detection only.
    source_file : str
        Source file name (e.g. ``"Fidelity_Transactions_v1.csv"``).
    encoding : str
        File encoding.
    delimiter : str
        Field delimiter.
    has_header : bool
        Whether the file has a header row.
    quote_char : str
        CSV quote character, when known.
    blank_line_count : int
        Count of blank lines observed after the header.
    ragged_row_count : int
        Count of sampled rows whose field count differs from the header.
    max_sample_values : int
        Maximum sample values to record per column.

    Returns
    -------
    dict[str, Any]
        A new profile dict with the existing keys plus the CSV enrichment.
    """
    profile = dict(base_profile)
    redacted_set = set(profile.get("redacted_columns") or [])
    columns = list(profile.get("columns") or [])
    profiled_row_count = int(profile.get("row_count", len(redacted_rows)))

    warnings: list[str] = []
    enriched_columns: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}

    for ordinal, col in enumerate(columns, start=1):
        raw_name = str(col.get("name", ""))
        redacted_values = [str(r.get(raw_name, "") or "") for r in redacted_rows]
        original_values = [str(r.get(raw_name, "") or "") for r in original_rows]
        enriched = _enrich_column(
            col,
            ordinal=ordinal,
            raw_name=raw_name,
            redacted_values=redacted_values,
            original_values=original_values,
            redacted=raw_name in redacted_set,
            seen_names=seen_names,
            max_sample_values=max_sample_values,
            warnings=warnings,
        )
        enriched_columns.append(enriched)

    if ragged_row_count:
        warnings.append(
            f"{ragged_row_count} sampled rows had a different column count than the header."
        )

    profile["profile_version"] = PROFILE_VERSION
    profile["columns"] = enriched_columns
    profile["csv"] = {
        "encoding": encoding,
        "delimiter": delimiter,
        "quote_char": quote_char,
        "has_header": has_header,
        "column_count": int(profile.get("column_count", len(columns))),
        "profiled_row_count": profiled_row_count,
        "blank_line_count": blank_line_count,
        "ragged_row_count": ragged_row_count,
    }
    profile["loader_hints"] = {
        "file_type": "CSV",
        "delimiter": delimiter,
        "encoding": encoding,
        "header": has_header,
    }
    source_stem = PurePosixPath(source_file).stem
    recommended_source = vp.normalize_name(source_stem)
    profile["llm_hints"] = {
        "recommended_source_name": recommended_source,
        "recommended_staging_table": f"{_STAGING_SCHEMA}.stg_{recommended_source}",
        "safe_to_generate_sql": not warnings,
        "warnings": warnings,
    }
    return profile


def _enrich_column(
    col: dict[str, Any],
    *,
    ordinal: int,
    raw_name: str,
    redacted_values: list[str],
    original_values: list[str],
    redacted: bool,
    seen_names: dict[str, int],
    max_sample_values: int,
    warnings: list[str],
) -> dict[str, Any]:
    """Return an enriched copy of one base column profile.

    Adds ordinal, normalised/safe names (with deterministic duplicate
    resolution), sample values, a refined ``type_hint``, and value-pattern
    hints. The base type column is left intact for backward compatibility.
    """
    enriched = dict(col)
    type_non_blank = [v for v in original_values if v.strip()]
    sample_source = redacted_values if redacted else original_values
    non_blank_sample = [v for v in sample_source if v.strip()]

    normalized = vp.normalize_name(raw_name)
    occurrence = seen_names.get(normalized, 0)
    seen_names[normalized] = occurrence + 1
    if occurrence:
        resolved = f"{normalized}_{occurrence + 1}"
        enriched["duplicate_of"] = normalized
    else:
        resolved = normalized

    enriched["ordinal"] = ordinal
    enriched["raw_name"] = raw_name
    enriched["normalized_name"] = resolved
    enriched["safe_sql_name"] = vp.safe_sql_name(resolved)
    enriched["non_blank_count"] = len(type_non_blank)
    enriched["sample_values"] = _sample_values(sample_source, max_sample_values)

    base_type = str(col.get("type", "text"))
    type_hint, hints = _detect_hints(raw_name, type_non_blank, base_type)
    if redacted:
        # Derived label hints (type, pattern, leading-zero) are safe to keep —
        # they do not reveal the masked values. Suppress only value-bearing
        # outputs so redacted content is never echoed back into the profile.
        for value_field in ("constant_value", "min_numeric", "max_numeric",
                            "min_date", "max_date"):
            hints.pop(value_field, None)
    enriched["type_hint"] = type_hint if type_hint in _ALLOWED_TYPE_HINTS else "text"
    enriched.update(hints)

    if enriched.get("is_empty"):
        # An empty column carries no values to pattern-detect; keep it as text.
        enriched["type_hint"] = "text"

    if hints.get("date_format_warning"):
        warnings.append(f"Column '{raw_name}': multiple date formats detected.")
    if hints.get("identifier_pattern_hint"):
        warnings.append(
            f"Column '{raw_name}': identifier-like numeric values detected; preserve as text."
        )
    if hints.get("negative_format") == "parentheses":
        warnings.append(f"Column '{raw_name}': accounting (parentheses) negative format detected.")
    return enriched


def _detect_hints(
    raw_name: str,
    type_non_blank: list[str],
    base_type: str,
) -> tuple[str, dict[str, Any]]:
    """Return ``(type_hint, hints)`` for a column from deterministic detection.

    Identifier detection overrides numeric inference (keeps the column text).
    Detection runs on the unredacted type values; the derived hints are labels
    (types/patterns), not raw values. The caller suppresses any value-bearing
    fields for redacted columns.
    """
    hints: dict[str, Any] = {}
    normalized = vp.normalize_name(raw_name)
    semantic = vp.semantic_hint_from_name(normalized)
    if semantic:
        hints["semantic_hint"] = semantic

    if not type_non_blank:
        hints["is_empty"] = True
        return "text", hints

    null_like = vp.detect_null_like([v for v in type_non_blank])
    if null_like:
        hints["null_like_values"] = null_like

    constant = vp.detect_constant(type_non_blank)
    hints.update(constant)
    hints.update(vp.uniqueness(type_non_blank))

    identifier = vp.detect_identifier(type_non_blank)
    if identifier:
        hints.update(identifier)
        if semantic is None:
            hints["semantic_hint"] = "identifier"
        return "text", hints

    boolean_hint = vp.detect_boolean(type_non_blank)
    if boolean_hint:
        hints["boolean_pattern_hint"] = boolean_hint
        return "boolean", hints

    date_hints = vp.detect_date_format(type_non_blank)
    hints.update(date_hints)
    if date_hints.get("date_format_hint"):
        _add_minmax(hints, type_non_blank, kind="date")
        return "date", hints
    if date_hints.get("date_format_warning"):
        return "text", hints

    percent = vp.detect_percent(type_non_blank)
    if percent:
        hints.update(percent)
        hints["semantic_hint"] = hints.get("semantic_hint") or "percent_or_rate"

    numeric = vp.detect_numeric_pattern(type_non_blank)
    if numeric:
        hints.update(numeric)
        hints.update(vp.sign_behavior(type_non_blank))
        hints.update(_numeric_digits(type_non_blank))
        _add_minmax(hints, type_non_blank, kind="numeric")
        has_decimal = "decimal" in numeric["numeric_pattern_hint"] or numeric.get(
            "contains_currency_symbol"
        )
        return ("decimal" if has_decimal else "integer"), hints

    return ("text" if base_type not in _ALLOWED_TYPE_HINTS else base_type), hints


def _sample_values(values: list[str], limit: int) -> list[str]:
    """Return up to ``limit`` distinct values preserving first-seen order."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for value in values:
        token = value.strip()
        if not token or token in seen_set:
            continue
        seen.append(token)
        seen_set.add(token)
        if len(seen) >= limit:
            break
    return seen


def _numeric_digits(values: list[str]) -> dict[str, Any]:
    """Return ``max_integer_digits``/``max_decimal_digits`` for numeric values."""
    max_int = 0
    max_dec = 0
    for value in values:
        core = value.strip()
        if core.startswith("(") and core.endswith(")"):
            core = core[1:-1]
        cleaned = "".join(ch for ch in core if ch.isdigit() or ch == ".")
        if not cleaned:
            continue
        whole, _, frac = cleaned.partition(".")
        max_int = max(max_int, len(whole))
        max_dec = max(max_dec, len(frac))
    return {"max_integer_digits": max_int, "max_decimal_digits": max_dec}


def _add_minmax(hints: dict[str, Any], values: list[str], *, kind: str) -> None:
    """Record cheap min/max for numeric or date columns when parseable."""
    if kind == "numeric":
        numbers: list[float] = []
        for value in values:
            core = value.strip()
            negative = core.startswith("(") and core.endswith(")")
            cleaned = core[1:-1] if negative else core
            cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch == ".")
            if not cleaned or cleaned == ".":
                continue
            try:
                num = float(cleaned)
            except ValueError:
                continue
            numbers.append(-num if negative else num)
        if numbers:
            hints["min_numeric"] = min(numbers)
            hints["max_numeric"] = max(numbers)
    elif kind == "date":
        dates = sorted(v.strip() for v in values if v.strip())
        if dates:
            hints["min_date"] = dates[0]
            hints["max_date"] = dates[-1]
