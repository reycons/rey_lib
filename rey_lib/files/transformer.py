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
import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from rey_lib.errors.error_utils import AppError

__all__ = [
    "transform_row",
    "match_header",
    "parse_date_from_filename",
    "TransformError",
]

_logger = logging.getLogger(__name__)


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

    The pattern uses {date} as a placeholder for the date string portion.
    Example: "tran_{date}.csv" with date_format "%Y%m%d" matches
    "tran_20261231.csv" and returns date(2026, 12, 31).

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
    # Support both key names used across projects.
    pattern = file_type_cfg.get("file_pattern") or file_type_cfg.get("filename_pattern", "")
    fmt     = file_type_cfg.get("date_format", "%Y%m%d")

    if not pattern:
        return None

    # Convert the glob-style pattern to a regex by escaping everything
    # except the {date} placeholder, which becomes a named capture group.
    regex = re.escape(pattern).replace(r"\{date\}", r"(?P<date>.+)")
    m     = re.fullmatch(regex, filename)
    if not m:
        return None

    try:
        return datetime.strptime(m.group("date"), fmt).date()
    except ValueError:
        _logger.debug(
            "Could not parse date '%s' from filename '%s'",
            m.group("date"), filename,
        )
        return None


def transform_row(
    raw_row: dict[str, str],
    file_type_cfg: dict,
    file_date: Optional[date] = None,
    row_num: int = 0,
) -> Optional[dict[str, Any]]:
    """
    Apply column mapping, constants, transforms, and file_date injection
    to one raw CSV row.

    Returns a new dict whose keys are database column names, or None if
    the row fails the row_filter check and should be discarded.

    Application-specific values such as source_file or batch_id must be
    provided via the 'constants' section of file_type_cfg — they are
    never injected by this function directly.

    Constants whose YAML value is the sentinel ``ctx.row_num`` are resolved
    to the current 1-based row number supplied via the ``row_num`` parameter.

    Parameters
    ----------
    raw_row : dict[str, str]
        Raw row from the CSV reader — keys are source file column names.
    file_type_cfg : dict
        A single file_types entry from the data source config. Expected
        keys: columns, constants, field_transforms, row_filter,
        file_date_column (optional).
    file_date : Optional[date]
        Date parsed from the filename by parse_date_from_filename().
        Injected into the column named by file_type_cfg['file_date_column']
        when that key is present and non-empty.
    row_num : int
        1-based row counter for the current file.  Injected into any
        constant whose configured value is the sentinel ``ctx.row_num``.

    Returns
    -------
    Optional[dict[str, Any]]
        Transformed row dict, or None if the row should be discarded.

    Raises
    ------
    TransformError
        If a required field fails to transform.
    """
    # Apply row filter first — discard non-data rows silently.
    if not _passes_row_filter(raw_row, file_type_cfg):
        return None

    columns    = file_type_cfg.get("columns", {})
    constants  = file_type_cfg.get("constants", {})
    transforms = file_type_cfg.get("field_transforms", {})

    out: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1 — column mapping
    # ------------------------------------------------------------------
    for db_col, src_col in columns.items():
        raw_val     = raw_row.get(src_col)
        out[db_col] = raw_val.strip() if raw_val is not None else ""

    # ------------------------------------------------------------------
    # Step 2 — inject constants
    # ------------------------------------------------------------------
    for db_col, value in constants.items():
        out[db_col] = row_num if value == "ctx.row_num" else value

    # ------------------------------------------------------------------
    # Step 3 — inject file_date
    # ------------------------------------------------------------------
    if file_date is not None:
        date_col = file_type_cfg.get("file_date_column", "")
        if date_col:
            out[date_col] = file_date

    # ------------------------------------------------------------------
    # Step 4 — apply transforms
    # ------------------------------------------------------------------
    secrets = file_type_cfg.get("secrets", {})

    for db_col, transform_cfg in transforms.items():
        transform_type = transform_cfg.get("type", "")

        out[db_col] = _apply_transform(
            db_col,
            out,
            raw_row,
            transform_cfg,
            transform_type,
            secrets,
        )

    # ------------------------------------------------------------------
    # Step 5 — inject hash column
    # ------------------------------------------------------------------
    hash_cfg = file_type_cfg.get("hash")
    if hash_cfg:
        out[hash_cfg["name"]] = _compute_hash(out, hash_cfg)

    return out


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
    value       = raw_row.get(column, "").strip()

    if filter_type == "date":
        fmt = row_filter.get("format", "%m/%d/%Y")
        return _try_parse_date(value, fmt) is not None

    if filter_type == "not_blank":
        return bool(value)

    # Unknown filter type — pass through rather than silently drop rows.
    _logger.debug("Unknown row_filter type '%s' — passing row through.", filter_type)
    return True


# ---------------------------------------------------------------------------
# Private — transform dispatcher
# ---------------------------------------------------------------------------

def _apply_transform(
	db_col: str,
	out: dict[str, Any],
	raw_row: dict[str, str],
	cfg: dict,
	transform_type: str,
	secrets: dict[str, str],
) -> Any:
	try:
		if transform_type == "date":
			return _transform_date(out.get(db_col, ""), cfg)

		if transform_type == "numeric":
			return _transform_numeric(out.get(db_col, ""), cfg)

		if transform_type == "regex_extract":
			return _transform_regex_extract(raw_row, cfg)

		if transform_type == "prefix_map":
			return _transform_prefix_map(db_col, out, raw_row, cfg)

		if transform_type == "strip_parens_suffix":
			return _transform_strip_parens(out.get(db_col, ""))

		if transform_type == "regex_date":
			return _transform_regex_date(raw_row, cfg)

		if transform_type == "encrypt":
			return _transform_encrypt(out.get(db_col, ""), raw_row, cfg, secrets)

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
# Private — individual transform implementations
# ---------------------------------------------------------------------------
def _transform_date(value: str, cfg: dict) -> Optional[date]:
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None

		raise TransformError("Empty date value and allow_blank is False.")

	fmt    = cfg.get("format", "%m/%d/%Y")
	result = _try_parse_date(value, fmt)

	if result is None:
		for fallback in (
			 "%m/%d/%y"
			,"%-m/%-d/%y"
			,"%-m/%-d/%Y"
		):
			result = _try_parse_date(value, fallback)

			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as date with format '{fmt}'."
		)

	return result

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


def _transform_regex_date(raw_row: dict[str, str], cfg: dict) -> Optional[date]:
    """
    Extract a date string from a source field via regex then parse it.

    Combines regex extraction and date parsing in a single transform —
    useful when a date is embedded within a larger string such as an
    option symbol (e.g. AAPL240119C00150000 → 2024-01-19).

    Parameters
    ----------
    raw_row : dict[str, str]
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source      — source column name
            pattern     — regex with optional capture group(s)
            format      — strptime format for the extracted date string
            group       — capture group index (default 1); use 0 for full match
            allow_blank — if True, return None on no-match instead of raising

    Returns
    -------
    Optional[date]
        Parsed date, or None if no match or allow_blank is True.

    Raises
    ------
    TransformError
        If the pattern matches but the date cannot be parsed and
        allow_blank is False.
    """
    source      = cfg.get("source", "")
    pattern     = cfg.get("pattern", "")
    fmt         = cfg.get("format", "%y%m%d")
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
    return result


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
    """
    Attempt to parse a date string, returning None on failure.

    Parameters
    ----------
    value : str
        Date string to parse.
    fmt : str
        strptime format string.

    Returns
    -------
    Optional[date]
        Parsed date or None.
    """
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None