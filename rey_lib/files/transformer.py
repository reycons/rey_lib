"""
Generic field-level transformer for delimited file conversion.

Applies a sequence of typed transformations to a raw row dict, producing
a new dict whose keys match the configured output column names. All
transformers are pure functions — they never mutate the input row.

Designed to be driven entirely by YAML config — no application-specific
knowledge lives here. Column mapping, constants, and transform rules all
come from the caller via file_type_cfg.

Transformation types
--------------------
date                Parse a string into a date using a specified format.
                    Supports fallback formats for common broker quirks.
datetime            Parse a string into a datetime (date + time).
                    Falls back to common datetime formats automatically.
time                Parse a string into a time-of-day value.
numeric             Strip unwanted characters and cast to float.
                    Handles parenthesised negatives: (1234.56) → -1234.56.
regex_extract       Extract a capture group from a source field using a regex.
                    Supports optional cast to float or int after extraction.
regex_date          Extract a date string from a source field via regex,
                    then parse it — useful for dates embedded in symbols.
prefix_map          Map a field value to a normalised value by matching
                    prefixes longest-first. Optionally strips the matched
                    prefix from another output field.
strip_parens_suffix Remove trailing parenthesised tokens from a string
                    e.g. "(Cash)", "(Margin)", "(TICKER)" → clean description.
not_blank           Pass-through — used by row_filter to discard blank values.
encrypt             Symmetrically encrypt a field value using Fernet (AES-128-CBC
                    with HMAC). The key is named by 'key_env' in the transform
                    config and resolved from the environment. Output is a
                    URL-safe base64 token string. Requires the 'cryptography'
                    package: pip install cryptography.

                    YAML example::

                        field_transforms:
                          account_number:
                            type: encrypt
                            key_env: TRADE_ENCRYPTION_KEY

                    Generate a key once and store in .env::

                        python -c "from cryptography.fernet import Fernet; \\
                                   print(Fernet.generate_key().decode())"

Hash columns
------------
hash                Compute a deterministic hash across a fixed set of output
                    columns and inject the result as a new column. Useful as
                    a surrogate link key in the database.

                    YAML example::

                        hash:
                          name:      file_key
                          hash_type: sha256   # sha256 | sha1 | md5 (default sha256)
                          columns:
                            - trade_date
                            - account
                            - card_order_no

                    Values are stringified and joined with '|' before hashing.
                    None / blank values are treated as empty strings so the hash
                    is always deterministic and never raises.

Constants and file_date injection
----------------------------------
Application-specific values (e.g. source_file, batch_id) must be injected
by the caller via the 'constants' section of the file_type_cfg — never
hardcoded in this module. The file_date parameter provides a date value
parsed from the filename; the target column is specified via
'file_date_column' in file_type_cfg, keeping the injection config-driven.

Public API
----------
transform_row(raw_row, file_type_cfg, file_date)
    Apply all column mappings, constants, transforms, and file_date
    injection to one raw row dict. Returns None if the row fails the
    row_filter check and should be discarded.
match_header(file_header, file_type_cfg)
    Return True if file_header matches the expected header for this
    file type definition.
parse_date_from_filename(filename, file_type_cfg)
    Extract and parse the date embedded in a filename using the
    configured pattern and date_format.
TransformError
    Raised when a field transformation fails and cannot be recovered.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from typing import Any, Optional

from rey_lib.errors.error_utils import AppError
from rey_lib.logs import get_logger

__all__ = [
    "transform_row",
    "match_header",
    "parse_date_from_filename",
    "TransformError",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class TransformError(AppError):
	def __init__(self, message: str, column: str = ""):
		super().__init__(message)
		self.column = column

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_header(file_header: str, file_type_cfg: dict) -> bool:
    """
    Return True if file_header exactly matches the expected header for
    this file type definition.

    Comparison is made after normalising whitespace on both sides —
    leading, trailing, and repeated internal spaces are collapsed.

    Parameters
    ----------
    file_header : str
        The raw header line read from the file (first non-blank line).
    file_type_cfg : dict
        A single file_types entry from the data source config.

    Returns
    -------
    bool
        True if the headers match, False otherwise.
    """
    expected = _normalise_header(file_type_cfg.get("header", ""))
    observed = _normalise_header(file_header)
    return expected == observed


def parse_date_from_filename(filename: str, file_type_cfg: dict) -> Optional[date]:
    """
    Extract the date embedded in a filename using the configured pattern.

    The pattern uses any single ``{token}`` placeholder for the date portion —
    e.g. ``bal_{yyyymmdd}.csv``, ``tran_{date}.csv``. The ``date_format`` field
    accepts either Excel/Java-style tokens (``yyyymmdd``, ``yyyymm``) or Python
    strptime strings (``%Y%m%d``). Whichever style is used, the same function
    converts it to strptime before parsing.

    Parameters
    ----------
    filename : str
        The bare filename (not the full path).
    file_type_cfg : dict
        A single file_types entry from the data source config.
        Expected keys: file_pattern (or filename_pattern), date_format.

    Returns
    -------
    Optional[date]
        Parsed date, or None if the pattern does not match or is absent.
    """
    pattern = file_type_cfg.get("file_pattern") or file_type_cfg.get("filename_pattern", "")
    fmt_raw = file_type_cfg.get("date_format", "yyyymmdd")

    if not pattern:
        return None

    # Split on the first {token} placeholder, escape each literal part,
    # then reassemble with a named capture group — avoids double-escaping.
    parts = re.split(r"\{[^}]+\}", pattern, maxsplit=1)
    if len(parts) == 2:
        regex = re.escape(parts[0]) + r"(?P<date>[^./\\]+)" + re.escape(parts[1])
    else:
        return None
    m     = re.fullmatch(regex, filename)
    if not m:
        return None

    fmt = _to_strptime_format(fmt_raw)
    try:
        return datetime.strptime(m.group("date"), fmt).date()
    except ValueError:
        _logger.debug(
            "Could not parse date '%s' from filename '%s' using format '%s'",
            m.group("date"), filename, fmt,
        )
        return None


# Regex alternation — longest token first to prevent partial matches.
# Follows Java/Excel standard convention:
#   MM  → months   (uppercase)
#   mm  → minutes  (lowercase)
#   dd  → days     HH → 24-hour   hh → 12-hour   ss → seconds
_DATE_TOKEN_RE: re.Pattern[str] = re.compile(
    r"yyyy|yy|MMMM|MMM|MM|M|dd|d|HH|hh|H|h|mm|ss|SSS|SS|S|a|Z"
)

_DATE_TOKEN_MAP: dict[str, str] = {
    # Year
    "yyyy": "%Y",   # 2026
    "yy":   "%y",   # 26
    # Month — uppercase
    "MMMM": "%B",   # January
    "MMM":  "%b",   # Jan
    "MM":   "%m",   # 05
    "M":    "%m",   # 5
    # Day
    "dd":   "%d",   # 14
    "d":    "%d",   # 4
    # Hour
    "HH":   "%H",   # 19  (24-hour)
    "hh":   "%I",   # 07  (12-hour)
    "H":    "%H",
    "h":    "%I",
    # Minute — lowercase
    "mm":   "%M",   # 20
    # Second
    "ss":   "%S",   # 52
    # Fractional seconds
    "SSS":  "%f",   # microseconds
    "SS":   "%f",
    "S":    "%f",
    # AM/PM
    "a":    "%p",   # AM / PM
    # Timezone offset
    "Z":    "%z",   # +0000 / -0500
}


def _to_strptime_format(fmt: str) -> str:
    """Convert an Excel/Java-style date format string to a Python strptime format.

    Accepts both double-token (``MM``, ``dd``) and single-token (``M``, ``d``)
    variants. If ``fmt`` already contains ``%`` it is returned unchanged so
    existing strptime strings in config continue to work.

    Follows Java/Excel standard: ``MM`` = months (uppercase), ``mm`` = minutes
    (lowercase). Existing strptime strings (containing ``%``) pass through unchanged.

    Examples
    --------
    ``MM/dd/yyyy``           → ``%m/%d/%Y``
    ``M/d/yy``               → ``%m/%d/%y``
    ``yyyyMMdd``             → ``%Y%m%d``
    ``yyyy-MM-dd``           → ``%Y-%m-%d``
    ``dd-MMM-yyyy``          → ``%d-%b-%Y``
    ``yyyy-MM-ddTHH:mm:ss``  → ``%Y-%m-%dT%H:%M:%S``
    """
    if "%" in fmt:
        return fmt
    return _DATE_TOKEN_RE.sub(lambda tok: _DATE_TOKEN_MAP[tok.group()], fmt)


def transform_row(
    raw_row: dict[str, str],
    file_type_cfg: dict,
    row_num: int = 0,
    ctx: Any = None,
) -> Optional[dict[str, Any]]:
    """
    Apply column mapping and transforms to one raw CSV row.

    Parameters
    ----------
    raw_row : dict[str, str]
        Raw row from the CSV reader.
    file_type_cfg : dict
        A single file type config entry. ``columns`` must be a list.
    row_num : int
        1-based row counter for the current file.
    ctx : Any
        Application context — required for ``context`` transform type.

    Returns
    -------
    Optional[dict[str, Any]]
        Transformed row dict, or None if the row should be discarded.

    Raises
    ------
    TransformError
        If a required field fails to transform.
    """
    return _transform_row_v2(raw_row, file_type_cfg, row_num=row_num, ctx=ctx)


def _transform_row_v2(
    raw_row: dict[str, str],
    file_type_cfg: dict,
    row_num: int = 0,
    ctx: Any = None,
) -> Optional[dict[str, Any]]:
    """New list-based column shape — each output field defined in one place."""
    if not _passes_row_filter(raw_row, file_type_cfg):
        return None

    file_type = _normalize_file_type(file_type_cfg.get("file_type", "delimited_header"))
    secrets   = file_type_cfg.get("secrets", {})
    out:      dict[str, Any] = {}
    deferred: list[dict]     = []

    for col_cfg in file_type_cfg.get("columns", []):
        name          = col_cfg["name"]
        transform_cfg = col_cfg.get("transform") or {}

        if transform_cfg.get("type") == "hash":
            deferred.append(col_cfg)
            continue

        raw_value  = _resolve_source_value(raw_row, col_cfg, file_type)
        out[name]  = _apply_transform_v2(
            name, raw_value, transform_cfg, out, raw_row, secrets,
            row_num=row_num, ctx=ctx,
        )

    for col_cfg in deferred:
        out[col_cfg["name"]] = _compute_hash(out, col_cfg["transform"])

    return out


# ---------------------------------------------------------------------------
# Private — new-shape helpers
# ---------------------------------------------------------------------------

_FILE_TYPE_ALIASES: dict[str, str] = {
    "CSV":              "delimited_header",
    "CSVPositional":    "delimited_no_header",
    "FixedWidth":       "fixed_width",
    "FixedWidthHeader": "fixed_width_header",
    "MultiFormat":      "multi_format",
}


def _normalize_file_type(file_type: str) -> str:
    """Resolve legacy file_type names to canonical values."""
    return _FILE_TYPE_ALIASES.get(file_type, file_type)


def _resolve_context_value(value_str: str, ctx: Any = None, row_num: int = 0) -> Any:
    """Resolve a ``ctx.*`` reference string to its runtime value."""
    if value_str == "ctx.row_num":
        return row_num
    if value_str.startswith("ctx.") and ctx is not None:
        current = ctx
        for part in value_str[4:].split("."):
            current = getattr(current, part, None)
            if current is None:
                return ""
        return current if current is not None else ""
    return value_str


def _resolve_source_value(
    raw_row: Any,
    col_cfg: dict,
    file_type: str,
) -> Any:
    """Extract the raw source value for one column from the raw row."""
    source = col_cfg.get("source")
    if source is None:
        return None

    if file_type in ("delimited_header", "CSV"):
        return raw_row.get(str(source))

    if file_type == "delimited_no_header":
        idx = int(source) - 1
        if isinstance(raw_row, (list, tuple)):
            return raw_row[idx] if idx < len(raw_row) else None
        return raw_row.get(idx)

    if file_type in ("fixed_width", "fixed_width_header"):
        if isinstance(source, dict):
            start = int(source.get("start", 1)) - 1
            end   = int(source.get("end", start + 1))
            line  = raw_row if isinstance(raw_row, str) else ""
            return line[start:end]
        return None

    return raw_row.get(str(source))


def _apply_transform_v2(
    db_col: str,
    value: Any,
    transform_cfg: dict,
    out: dict[str, Any],
    raw_row: Any,
    secrets: dict[str, str],
    row_num: int = 0,
    ctx: Any = None,
) -> Any:
    """Transform dispatcher for the new list-based column shape."""
    if not transform_cfg:
        return value.strip() if isinstance(value, str) else value

    transform_type = transform_cfg.get("type", "")

    try:
        if transform_type == "constant":
            return transform_cfg.get("value")

        if transform_type == "context":
            return _resolve_context_value(
                transform_cfg.get("value", ""), ctx=ctx, row_num=row_num,
            )

        if transform_type == "date":
            return _transform_date(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "datetime":
            return _transform_datetime(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "time":
            return _transform_time(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "numeric":
            return _transform_numeric(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "regex_extract":
            return _transform_regex_extract(raw_row, transform_cfg)

        if transform_type == "prefix_map":
            return _transform_prefix_map(db_col, out, raw_row, transform_cfg)

        if transform_type == "strip_parens_suffix":
            return _transform_strip_parens((value or "") if isinstance(value, str) else "")

        if transform_type == "regex_date":
            return _transform_regex_date(raw_row, transform_cfg)

        if transform_type == "encrypt":
            return _transform_encrypt(value, raw_row, transform_cfg, secrets)

        if transform_type == "file_hash":
            return _resolve_context_value("ctx.file_checksum", ctx=ctx)

        if transform_type == "not_blank":
            return out.get(db_col, "")

        raise TransformError(
            f"Unknown transform type '{transform_type}' for column '{db_col}'",
            column=db_col,
        )

    except TransformError as exc:
        if not getattr(exc, "column", ""):
            exc.column = db_col
        raise


# ---------------------------------------------------------------------------
# Private — row filter
# ---------------------------------------------------------------------------

def _passes_row_filter(raw_row: dict[str, str], file_type_cfg: dict) -> bool:
    """
    Return True if the row passes the configured row filter.

    A row that fails the filter is silently discarded — this handles blank
    lines, header repetitions, footers, and summary rows.

    Parameters
    ----------
    raw_row : dict[str, str]
        Raw CSV row.
    file_type_cfg : dict
        File type config containing an optional row_filter section.

    Returns
    -------
    bool
        True if the row should be processed, False if it should be discarded.
    """
    row_filter = file_type_cfg.get("row_filter")
    if not row_filter:
        return True

    column      = row_filter.get("column", "")
    filter_type = row_filter.get("type", "")
    value       = (raw_row.get(column) or "").strip()

    if filter_type == "date":
        fmt = _to_strptime_format(row_filter.get("format", "%m/%d/%Y"))
        return _try_parse_date(value, fmt) is not None

    if filter_type == "not_blank":
        return bool(value)

    # Unknown filter type — pass through rather than silently drop rows.
    _logger.debug("Unknown row_filter type '%s' — passing row through.", filter_type)
    return True


# ANSI standard output formats — used when output_format is not configured.
_ANSI_DATE_FMT:     str = "%Y-%m-%d"
_ANSI_DATETIME_FMT: str = "%Y-%m-%d %H:%M:%S"
_ANSI_TIME_FMT:     str = "%H:%M:%S"


def _apply_output_format(
    parsed: date | datetime | time | None,
    cfg:    dict,
    ansi_fmt: str,
) -> Optional[str]:
    """Format a parsed date/datetime/time to a string.

    Uses ``output_format`` from config when present (token syntax supported),
    otherwise falls back to the ANSI standard format for the type.
    Returns None when ``parsed`` is None.
    """
    if parsed is None:
        return None
    raw_out = cfg.get("output_format")
    fmt     = _to_strptime_format(raw_out) if raw_out else ansi_fmt
    return parsed.strftime(fmt)


# ---------------------------------------------------------------------------
# Private — individual transform implementations
# ---------------------------------------------------------------------------
def _transform_date(value: str, cfg: dict) -> Optional[str]:
	"""Parse a date string using the configured format with fallback support.

	Tries ``format`` first. If that fails, tries ``fallback_formats`` from the
	field config (for uncommon or feed-specific formats), then the built-in
	unambiguous fallbacks. Ambiguous formats (e.g. day/month vs month/day)
	must be declared explicitly via ``fallback_formats`` — they are never
	auto-detected.

	Output defaults to ANSI standard ``yyyy-MM-dd``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``MM/dd/yyyy``)
	output_format    : str  — output string format (default ANSI ``yyyy-MM-dd``)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty date value and allow_blank is False.")

	fmt    = _to_strptime_format(cfg.get("format", "%m/%d/%Y"))
	result = _try_parse_date(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_date(value, _to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%m/%d/%y",              # US short year
			"%m/%d/%Y",              # US long year
			"%Y-%m-%d",              # ISO 8601 date
			"%Y%m%d",                # compact ISO
			"%Y/%m/%d",              # ISO slash
			"%Y-%m-%dT%H:%M:%S",    # ISO 8601 datetime
			"%Y-%m-%d %H:%M:%S",    # ISO datetime space
			"%m/%d/%Y %H:%M:%S",    # US datetime
			"%d-%b-%Y",              # 14-May-2026
			"%d-%b-%y",              # 14-May-26
			"%d %b %Y",              # 14 May 2026
			"%b %d, %Y",             # May 14, 2026
		):
			result = _try_parse_date(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as date with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_DATE_FMT)


def _transform_datetime(value: str, cfg: dict) -> Optional[str]:
	"""Parse a datetime string — preserves both date and time components.

	Output defaults to ANSI standard ``yyyy-MM-dd HH:mm:ss``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``yyyy-MM-dd HH:mm:ss``)
	output_format    : str  — output string format (default ANSI)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty datetime value and allow_blank is False.")

	fmt    = _to_strptime_format(cfg.get("format", "yyyy-MM-dd HH:mm:ss"))
	result = _try_parse_datetime(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_datetime(value, _to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%Y-%m-%dT%H:%M:%S",    # ISO 8601
			"%Y-%m-%d %H:%M:%S",    # ISO space
			"%Y-%m-%dT%H:%M:%S.%f", # ISO with microseconds
			"%Y-%m-%d %H:%M:%S.%f", # ISO space with microseconds
			"%m/%d/%Y %H:%M:%S",    # US datetime
			"%m/%d/%y %H:%M:%S",    # US short year
			"%Y%m%d%H%M%S",         # compact
		):
			result = _try_parse_datetime(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as datetime with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_DATETIME_FMT)


def _transform_time(value: str, cfg: dict) -> Optional[str]:
	"""Parse a time-of-day string — discards any date component.

	Output defaults to ANSI standard ``HH:mm:ss``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``HH:mm:ss``)
	output_format    : str  — output string format (default ANSI)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty time value and allow_blank is False.")

	fmt    = _to_strptime_format(cfg.get("format", "HH:mm:ss"))
	result = _try_parse_time(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_time(value, _to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%H:%M:%S",      # 19:20:52
			"%H:%M",         # 19:20
			"%I:%M:%S %p",   # 07:20:52 AM
			"%I:%M %p",      # 07:20 AM
			"%H%M%S",        # 192052 compact
		):
			result = _try_parse_time(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as time with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_TIME_FMT)


def _transform_numeric(value: str, cfg: dict) -> Optional[float]:
    """
    Strip unwanted characters from a string and cast to float.

    Handles parenthesised negatives: (1234.56) → -1234.56.

    Parameters
    ----------
    value : str
        Raw string value.
    cfg : dict
        Transform config. Keys: strip_chars (optional — chars to remove).

    Returns
    -------
    Optional[float]
        Parsed float, or None if the value is blank or unparseable.
    """
    value = value.strip()
    if not value:
        return None

    strip_chars = cfg.get("strip_chars", "")
    if strip_chars:
        for ch in strip_chars:
            value = value.replace(ch, "")

    value = value.strip()

    # Parenthesised negatives are common in broker exports.
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    try:
        return float(value)
    except ValueError:
        _logger.debug("Cannot parse '%s' as numeric — returning None.", value)
        return None


def _transform_regex_extract(raw_row: dict[str, str], cfg: dict) -> Any:
    """
    Extract a capture group from a source field using a regex pattern.

    Parameters
    ----------
    raw_row : dict
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source   — source column name in the raw row
            pattern  — regex pattern with capture group(s)
            group    — capture group index (default 1)
            strip    — strip whitespace from result (default True)
            cast_to  — optional: 'float'/'double' or 'int'/'integer'

    Returns
    -------
    Any
        Captured group value, cast to the configured type if specified.
        Returns the original value if no match; returns None on cast failure.
    """
    source      = cfg.get("source", "")
    pattern     = cfg.get("pattern", "")
    group       = cfg.get("group", 1)
    do_strip    = cfg.get("strip", True)
    allow_blank = cfg.get("allow_blank", False)

    value = raw_row.get(source, "").strip()
    if not value or not pattern:
        return None if allow_blank else ""

    m = re.search(pattern, value)
    if not m:
        if not allow_blank:
            _logger.debug("Pattern '%s' did not match '%s'.", pattern, value)
        return None if allow_blank else value

    result = m.group(group)
    result = result.strip() if do_strip else result

    cast_to = cfg.get("cast_to", "")
    if cast_to in ("float", "double"):
        try:
            return float(result) if result else None
        except ValueError:
            _logger.debug("Cannot cast '%s' to float — returning None.", result)
            return None
    if cast_to in ("int", "integer"):
        try:
            return int(result) if result else None
        except ValueError:
            _logger.debug("Cannot cast '%s' to int — returning None.", result)
            return None

    return result


def _transform_prefix_map(
    db_col: str,
    out: dict[str, Any],
    raw_row: dict[str, str],
    cfg: dict,
) -> str:
    """
    Map a field value to a normalised value by matching prefixes longest-first.

    Prefixes are tested longest-first so more specific prefixes take
    precedence over shorter ones. Matching is case-insensitive when
    case_insensitive is True in config.

    If strip_from is set, the matched prefix is removed from the named
    output column (typically 'description') as a side effect.

    Parameters
    ----------
    db_col : str
        The output column being mapped (typically 'action').
    out : dict
        Current output row — modified in-place if strip_from is set.
    raw_row : dict
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source           — source column name
            prefixes         — dict of prefix → normalised value
            strip_from       — output column to strip the prefix from
            case_insensitive — bool (default True)
            default          — value when no prefix matches

    Returns
    -------
    str
        Normalised value, or default if no prefix matches.
    """
    source           = cfg.get("source", db_col)
    prefixes         = cfg.get("prefixes", {})
    strip_from       = cfg.get("strip_from", "")
    case_insensitive = cfg.get("case_insensitive", True)
    default          = cfg.get("default", "Other")

    raw_value = raw_row.get(source, out.get(db_col, "")).strip()
    compare   = raw_value.upper() if case_insensitive else raw_value

    # Sort prefixes longest-first so more specific entries win.
    for prefix in sorted(prefixes.keys(), key=len, reverse=True):
        test = prefix.upper() if case_insensitive else prefix
        if compare.startswith(test):
            if strip_from and strip_from in out:
                suffix          = raw_value[len(prefix):].strip()
                out[strip_from] = _transform_strip_parens(suffix)
            return prefixes[prefix]

    _logger.debug(
        "No prefix matched for value '%s' — using default '%s'.",
        raw_value, default,
    )
    return default


def _transform_regex_date(raw_row: dict[str, str], cfg: dict) -> Optional[str]:
    """
    Extract a date string from a source field via regex then parse it.

    Combines regex extraction and date parsing in a single transform —
    useful when a date is embedded within a larger string such as an
    option symbol (e.g. AAPL240119C00150000 → 2024-01-19).

    Output defaults to ANSI standard ``yyyy-MM-dd``. Override with
    ``output_format`` using the same token syntax as ``format``.

    Parameters
    ----------
    raw_row : dict[str, str]
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source        — source column name
            pattern       — regex with optional capture group(s)
            format        — input format for the extracted date string
            output_format — output string format (default ANSI ``yyyy-MM-dd``)
            group         — capture group index (default 1); use 0 for full match
            allow_blank   — if True, return None on no-match instead of raising

    Returns
    -------
    Optional[str]
        Formatted date string, or None if no match or allow_blank is True.

    Raises
    ------
    TransformError
        If the pattern matches but the date cannot be parsed and
        allow_blank is False.
    """
    source      = cfg.get("source", "")
    pattern     = cfg.get("pattern", "")
    fmt         = _to_strptime_format(cfg.get("format", "%y%m%d"))
    group       = cfg.get("group", 1)
    allow_blank = cfg.get("allow_blank", False)

    value = raw_row.get(source, "").strip()
    if not value or not pattern:
        return None

    m = re.search(pattern, value)
    if not m:
        return None

    try:
        date_str = m.group(group) if group else m.group(0)
    except IndexError:
        if allow_blank:
            return None
        raise TransformError(
            f"Pattern '{pattern}' has no group {group} in value '{value}'."
        )

    result = _try_parse_date(date_str.strip(), fmt)
    if result is None:
        if allow_blank:
            return None
        raise TransformError(
            f"Cannot parse '{date_str}' as date with format '{fmt}'."
        )
    return _apply_output_format(result, cfg, _ANSI_DATE_FMT)


def _transform_encrypt(
    value: Any,
    raw_row: dict[str, str],
    cfg: dict,
    secrets: dict[str, str],
) -> Optional[str]:
    """
    Encrypt a string value using Fernet symmetric encryption.

    Blank values are returned unchanged. The encryption key is resolved
    from the secrets dict using the key_env name specified in config.
    Requires the 'cryptography' package.

    Parameters
    ----------
    value : Any
        Plaintext value to encrypt (already mapped output value).
    raw_row : dict[str, str]
        Raw source row. Used when encrypt config also includes regex
        extraction keys (source/pattern/group/strip).
    cfg : dict
        Transform config. Keys:
            key_env — name of the environment variable holding the Fernet key.
            include_key_env — when True, output as '<key_env>:<token>'.
    secrets : dict[str, str]
        Resolved env-var values keyed by variable name.

    Returns
    -------
    Optional[str]
        Fernet token string, or '<key_env>:<token>' when include_key_env is
        True. Returns the original value if blank.

    Raises
    ------
    TransformError
        If key_env is missing, the key cannot be found, or encryption fails.
    """
    # Optional pre-extraction: when source/pattern are present, derive the
    # plaintext from raw_row first, then encrypt the extracted value.
    plaintext: Any = value
    if cfg.get("source") and cfg.get("pattern"):
        plaintext = _transform_regex_extract(raw_row, cfg)

    if plaintext is None:
        return None

    plaintext_str = str(plaintext).strip()

    # Return blank values unchanged — nothing to encrypt.
    if not plaintext_str:
        return plaintext_str

    key_env = cfg.get("key_env", "")
    if not key_env:
        raise TransformError(
            "encrypt transform requires 'key_env' to be set in the transform config."
        )

    raw_key = secrets.get(key_env)
    if not raw_key:
        raise TransformError(
            f"Encryption key '{key_env}' not found. "
            f"Ensure {key_env} is set in the environment or .env file."
        )

    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415  lazy import
    except ImportError as exc:
        raise TransformError(
            "The 'cryptography' package is required for encrypt transforms. "
            "Install it: pip install cryptography"
        ) from exc

    try:
        # Key must be bytes; encode if the env var value is a plain string.
        key    = raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key
        fernet = Fernet(key)
        token  = fernet.encrypt(plaintext_str.encode("utf-8"))
        token_text = token.decode("utf-8")

        if cfg.get("include_key_env", False):
            return f"{key_env}:{token_text}"

        return token_text
    except (TypeError, ValueError) as exc:
        raise TransformError(
            f"Fernet encryption failed: {exc}"
        ) from exc


def _transform_strip_parens(value: str) -> str:
    """
    Remove trailing parenthesised tokens from a description string.

    Applied repeatedly until no trailing parens tokens remain.
    Examples: "Buy (Cash)" → "Buy", "Foo (Bar) (Baz)" → "Foo".

    Parameters
    ----------
    value : str
        Raw description string.

    Returns
    -------
    str
        Cleaned string with trailing parenthesised tokens removed.
    """
    pattern = re.compile(r"\s*\([^)]+\)\s*$")
    result  = value.strip()
    while True:
        cleaned = pattern.sub("", result).strip()
        if cleaned == result:
            break
        result = cleaned
    return result


# ---------------------------------------------------------------------------
# Private — helpers
# ---------------------------------------------------------------------------

def _compute_hash(out: dict[str, Any], hash_cfg: dict) -> str:
    """Compute a deterministic hash over the specified output columns.

    Values are stringified and joined with '|' before hashing.  None values
    and missing keys become empty strings so the result is always stable.

    Parameters
    ----------
    out : dict[str, Any]
        The fully-transformed output row (all earlier steps already applied).
    hash_cfg : dict
        The 'hash' section from the file-type config.  Expected keys:
            name      — output column name (used by the caller, not here)
            hash_type — algorithm name accepted by hashlib (default 'sha256')
            columns   — list of output column names to include in the hash

    Returns
    -------
    str
        Hex-digest string.

    Raises
    ------
    TransformError
        If the requested hash_type is not available in hashlib.
    """
    algorithm = hash_cfg.get("hash_type", "sha256")
    columns   = hash_cfg.get("columns", [])

    parts = (str(out.get(col, "") or "") for col in columns)
    payload = "|".join(parts).encode("utf-8")

    try:
        digest = hashlib.new(algorithm, payload)
    except ValueError as exc:
        raise TransformError(
            f"Unsupported hash_type '{algorithm}': {exc}"
        ) from exc

    return digest.hexdigest()


def _normalise_header(header: str) -> str:
    """
    Normalise a header string for comparison by collapsing whitespace.

    Strips each field and rejoins with commas so that leading/trailing
    spaces around column names do not cause false mismatches.

    Parameters
    ----------
    header : str
        Raw header string.

    Returns
    -------
    str
        Normalised header string.
    """
    return ",".join(col.strip() for col in header.split(","))


def _try_parse_date(value: str, fmt: str) -> Optional[date]:
    """Attempt to parse a date string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _try_parse_datetime(value: str, fmt: str) -> Optional[datetime]:
    """Attempt to parse a datetime string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt)
    except ValueError:
        return None


def _try_parse_time(value: str, fmt: str) -> Optional[time]:
    """Attempt to parse a time string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt).time()
    except ValueError:
        return None