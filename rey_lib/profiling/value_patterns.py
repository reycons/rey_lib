"""
Deterministic value-pattern detectors for CSV column profiling.

Pure, dependency-free helpers that inspect a column's non-blank string values
and report cheap, deterministic patterns the LLM can use to generate safer
staging/final DDL and ``rey_loader`` YAML — without guessing. Detection is
conservative: when a signal is ambiguous the helpers report ``None`` (or a
warning) rather than forcing an aggressive guess.

This module performs no I/O and no LLM calls. It is layout-agnostic but is
intended for delimited (CSV-like) profiling only.

Public API
----------
normalize_name        Normalise a raw column name to snake_case.
safe_sql_name         Derive a SQL-safe identifier from a normalised name.
detect_null_like      Null/blank marker values present in a column.
detect_numeric_pattern Currency/accounting numeric pattern + flags.
detect_date_format    Dominant date format hint and parse-success ratio.
detect_boolean        Flag/boolean value-set pattern.
detect_identifier     Identifier-like (keep-as-text) detection.
detect_percent        Percentage/rate pattern.
detect_constant       Constant-column detection.
sign_behavior         Positive/negative presence and negative format.
uniqueness            Distinct count, unique ratio, possible-key hint.
semantic_hint_from_name Conservative semantic hint from a column name.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "normalize_name",
    "safe_sql_name",
    "detect_null_like",
    "detect_numeric_pattern",
    "detect_date_format",
    "detect_boolean",
    "detect_identifier",
    "detect_percent",
    "detect_constant",
    "sign_behavior",
    "uniqueness",
    "semantic_hint_from_name",
]

# Common null/blank markers (compared case-insensitively). ``0`` is deliberately
# excluded — a zero is a value, not a null, per the contract.
_NULL_LIKE = {"null", "n/a", "na", "--", "none", "nil", "(null)", "\\n"}

# Boolean/flag value sets. Each entry is a frozenset of the lowered value pair.
_BOOLEAN_SETS: list[tuple[str, frozenset[str]]] = [
    ("Y/N", frozenset({"y", "n"})),
    ("Yes/No", frozenset({"yes", "no"})),
    ("true/false", frozenset({"true", "false"})),
    ("0/1", frozenset({"0", "1"})),
    ("T/F", frozenset({"t", "f"})),
    ("A/I", frozenset({"a", "i"})),
    ("Active/Inactive", frozenset({"active", "inactive"})),
]

# Date format hints keyed by an anchored regex. Order matters: most specific
# first. ``parse_success_ratio`` is computed against the matched format.
_DATE_FORMATS: list[tuple[str, str]] = [
    (r"^\d{4}-\d{2}-\d{2}$", "YYYY-MM-DD"),
    (r"^\d{2}/\d{2}/\d{4}$", "MM/DD/YYYY"),
    (r"^\d{2}-\d{2}-\d{4}$", "MM-DD-YYYY"),
    (r"^\d{2}/\d{2}/\d{2}$", "MM/DD/YY"),
    (r"^\d{8}$", "YYYYMMDD"),
    (r"^\d{1,2}-[A-Za-z]{3}-\d{4}$", "DD-MON-YYYY"),
    (r"^\d{1,2} [A-Za-z]{3} \d{4}$", "DD MON YYYY"),
]

# Conservative column-name -> semantic hint mapping. Each entry maps a tuple of
# candidate tokens/phrases to a hint; matched against the normalised name.
_SEMANTIC_NAME_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("trade_date",), "trade_date"),
    (("settlement_date", "settle_date"), "settlement_date"),
    (("transaction_date", "txn_date"), "transaction_date"),
    (("run_date",), "run_date"),
    (("cusip",), "cusip"),
    (("isin",), "isin"),
    (("sedol",), "sedol"),
    (("symbol", "ticker"), "symbol"),
    (("account_number", "account_no", "account", "acct"), "account"),
    (("quantity", "shares"), "quantity"),
    (("price",), "price"),
    (("amount",), "amount"),
    (("commission",), "commission"),
    (("fees", "fee"), "fees"),
    (("security_description", "description", "desc"), "description"),
    (("action", "transaction_type", "txn_type"), "transaction_type"),
]


def normalize_name(raw_name: str) -> str:
    """Return a snake_case normalisation of a raw column name.

    Lowercases, replaces non-alphanumeric runs with a single underscore, and
    strips leading/trailing underscores. An empty or symbol-only name yields
    ``"column"``.

    Parameters
    ----------
    raw_name : str
        Original source column name.

    Returns
    -------
    str
        Normalised snake_case name.
    """
    lowered = (raw_name or "").strip().lower()
    collapsed = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return collapsed or "column"


def safe_sql_name(normalized: str) -> str:
    """Return a SQL-safe identifier derived from a normalised name.

    Ensures the identifier does not start with a digit (prefixes ``col_``) and
    is non-empty.

    Parameters
    ----------
    normalized : str
        A normalised snake_case name.

    Returns
    -------
    str
        SQL-safe identifier.
    """
    name = normalized or "column"
    if name[0].isdigit():
        name = f"col_{name}"
    return name


def detect_null_like(values: list[str]) -> list[str]:
    """Return the distinct null-like marker values present in ``values``.

    Parameters
    ----------
    values : list[str]
        Raw string values (blanks allowed).

    Returns
    -------
    list[str]
        Distinct markers found, in first-seen order. An empty string is
        reported as ``""`` when blanks are present.
    """
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = (value or "").strip()
        key = token.lower()
        marker = "" if token == "" else token
        if (token == "" or key in _NULL_LIKE) and marker not in seen:
            seen.add(marker)
            found.append(marker)
    return found


def detect_numeric_pattern(values: list[str]) -> dict[str, Any]:
    """Return a numeric/currency pattern hint and flags for a column.

    Inspects non-blank values for commas, currency symbols, and parentheses
    negatives. Returns an empty dict when fewer than 80% of non-blank values
    look numeric (after stripping formatting).

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    dict[str, Any]
        Keys: ``numeric_pattern_hint``, ``contains_commas``,
        ``contains_currency_symbol``, ``negative_format``. Empty when the
        column is not predominantly numeric.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {}

    numeric_like = 0
    contains_commas = False
    contains_currency = False
    has_paren_negative = False
    has_minus_negative = False
    has_decimal = False

    for value in non_blank:
        if "," in value:
            contains_commas = True
        if any(sym in value for sym in ("$", "€", "£", "¥")):
            contains_currency = True
        core = value
        if core.startswith("(") and core.endswith(")"):
            has_paren_negative = True
            core = core[1:-1]
        elif core.startswith("-"):
            has_minus_negative = True
        stripped = re.sub(r"[,$€£¥\s]", "", core).lstrip("+-")
        if "." in stripped:
            has_decimal = True
        digits = stripped.replace(".", "", 1)
        if digits.isdigit() and digits:
            numeric_like += 1

    if numeric_like / len(non_blank) < 0.8:
        return {}

    if has_paren_negative:
        negative_format: str | None = "parentheses"
    elif has_minus_negative:
        negative_format = "leading_minus"
    else:
        negative_format = None

    if contains_currency:
        hint = "currency_parentheses_negative" if has_paren_negative else "currency_with_commas"
    elif contains_commas:
        hint = "comma_grouped"
    elif has_decimal:
        hint = "signed_decimal" if has_minus_negative else "decimal"
    else:
        hint = "signed_integer" if has_minus_negative else "integer"

    return {
        "numeric_pattern_hint": hint,
        "contains_commas": contains_commas,
        "contains_currency_symbol": contains_currency,
        "negative_format": negative_format,
    }


def detect_date_format(values: list[str]) -> dict[str, Any]:
    """Return a date-format hint and parse-success ratio for a column.

    Detects the dominant anchored date format. When multiple incompatible
    formats appear with no clear majority, returns a warning and no hint.

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    dict[str, Any]
        Keys: ``date_format_hint`` and ``date_parse_success_ratio`` when a
        format dominates; ``date_format_warning`` when formats are mixed;
        empty when the column does not look like dates.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {}

    counts: dict[str, int] = {}
    for value in non_blank:
        for pattern, label in _DATE_FORMATS:
            if re.match(pattern, value):
                counts[label] = counts.get(label, 0) + 1
                break

    matched = sum(counts.values())
    if matched == 0 or matched / len(non_blank) < 0.8:
        return {}

    if len(counts) > 1:
        return {"date_format_warning": "Multiple date formats detected in sample."}

    label, hits = next(iter(counts.items()))
    return {
        "date_format_hint": label,
        "date_parse_success_ratio": round(hits / len(non_blank), 4),
    }


def detect_boolean(values: list[str]) -> str | None:
    """Return a boolean/flag pattern hint, or None when not a flag column.

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    str | None
        A pattern label (e.g. ``"Y/N"``) when the distinct value set matches a
        known flag pair, else None.
    """
    distinct = {v.strip().lower() for v in values if v and v.strip()}
    # A flag needs both states present; a single constant value (e.g. all "0")
    # is not a boolean column.
    if len(distinct) != 2:
        return None
    for label, value_set in _BOOLEAN_SETS:
        if distinct == value_set:
            return label
    return None


def detect_identifier(values: list[str]) -> dict[str, Any]:
    """Return identifier-like detection for a numeric-looking text column.

    Detects values that should stay text even though they look numeric —
    notably numbers with meaningful leading zeroes. Identifier detection is
    intended to override numeric type inference.

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    dict[str, Any]
        Keys ``preserve_leading_zeroes`` and ``identifier_pattern_hint`` when a
        leading-zero numeric pattern is found; empty otherwise.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {}

    digit_like = [v for v in non_blank if v.isdigit()]
    if not digit_like:
        return {}

    leading_zero = [v for v in digit_like if len(v) > 1 and v.startswith("0")]
    if leading_zero and len(digit_like) / len(non_blank) >= 0.8:
        return {
            "preserve_leading_zeroes": True,
            "identifier_pattern_hint": "leading_zero_numeric",
        }
    return {}


def detect_percent(values: list[str]) -> dict[str, Any]:
    """Return a percent/rate pattern hint when a column contains percentages.

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    dict[str, Any]
        Keys ``contains_percent_symbol`` and ``numeric_pattern_hint`` when a
        ``%`` symbol is present on most values; empty otherwise.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {}
    with_percent = sum(1 for v in non_blank if v.endswith("%"))
    if with_percent and with_percent / len(non_blank) >= 0.8:
        return {"contains_percent_symbol": True, "numeric_pattern_hint": "percent"}
    return {}


def detect_constant(values: list[str]) -> dict[str, Any]:
    """Return constant-column detection.

    Parameters
    ----------
    values : list[str]
        Non-blank string values.

    Returns
    -------
    dict[str, Any]
        ``{"is_constant": True, "constant_value": <value>}`` when every
        non-blank value is identical (and at least one exists); empty otherwise.
    """
    distinct = {v.strip() for v in values if v and v.strip()}
    if len(distinct) == 1:
        return {"is_constant": True, "constant_value": next(iter(distinct))}
    return {}


def sign_behavior(values: list[str]) -> dict[str, Any]:
    """Return positive/negative presence and the negative format for a column.

    Parameters
    ----------
    values : list[str]
        Non-blank numeric-looking string values.

    Returns
    -------
    dict[str, Any]
        Keys ``has_positive_values``, ``has_negative_values``,
        ``negative_format`` when numeric signs are present; empty otherwise.
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {}

    has_negative = False
    negative_format: str | None = None
    has_positive = False
    for value in non_blank:
        if value.startswith("(") and value.endswith(")"):
            has_negative = True
            negative_format = "parentheses"
        elif value.startswith("-"):
            has_negative = True
            negative_format = negative_format or "leading_minus"
        else:
            core = re.sub(r"[,$€£¥\s]", "", value).lstrip("+")
            if core.replace(".", "", 1).isdigit() and core not in {"", "0", "0.0"}:
                has_positive = True

    if not (has_positive or has_negative):
        return {}
    return {
        "has_positive_values": has_positive,
        "has_negative_values": has_negative,
        "negative_format": negative_format,
    }


def uniqueness(values: list[str]) -> dict[str, Any]:
    """Return distinct-count, unique-ratio, and a conservative key hint.

    Parameters
    ----------
    values : list[str]
        Non-blank string values from the profiled sample.

    Returns
    -------
    dict[str, Any]
        Keys ``distinct_sample_count``, ``unique_ratio``, ``possible_key``.
        ``possible_key`` is only a hint (never a declared primary key).
    """
    non_blank = [v.strip() for v in values if v and v.strip()]
    if not non_blank:
        return {"distinct_sample_count": 0, "unique_ratio": 0.0, "possible_key": False}
    distinct = len(set(non_blank))
    ratio = round(distinct / len(non_blank), 4)
    return {
        "distinct_sample_count": distinct,
        "unique_ratio": ratio,
        "possible_key": ratio == 1.0 and len(non_blank) > 1,
    }


def semantic_hint_from_name(normalized_name: str) -> str | None:
    """Return a conservative semantic hint derived from a column name.

    Uses deterministic token matching only — never an LLM, and never invents a
    meaning when the name is unclear.

    Parameters
    ----------
    normalized_name : str
        The normalised snake_case column name.

    Returns
    -------
    str | None
        A semantic hint label, or None when no confident match exists.
    """
    name = normalized_name or ""
    tokens = set(name.split("_"))
    for needles, hint in _SEMANTIC_NAME_HINTS:
        for needle in needles:
            if needle == name or set(needle.split("_")) <= tokens:
                return hint
    return None
