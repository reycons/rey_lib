"""Reading a FILE's fields, and nothing about what is done with them.

What is left here after column transformation moved to the object that owns
it (``rey_lib.data.column_transform``). The division is the one the
signatures already drew: a header LINE, a FILE NAME, a format token and
resolving a field by POSITION or OFFSET are file parsing; applying a rule to a
value is not, and never was a fact about files.

    match_header             does this file's header match the declaration
    parse_date_from_filename the date a file's NAME carries
    keyed_record             a raw row of any format, as a keyed record

``keyed_record`` is the boundary itself. Positional and fixed-width formats
name a field by index or by offsets, and a transform must not have to know
that -- so the raw row is keyed HERE, by whatever the declaration calls each
source, and what leaves is the same keyed shape every ``DataFile`` already
produces. A delimited file with a header is already that, so keying it is the
identity.

Nothing here applies a transformation, and nothing here is a transformation
type.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from rey_lib.data.date_formats import to_strptime_format
from rey_lib.logs import get_logger

__all__ = [
    "match_header",
    "parse_date_from_filename",
    "keyed_record",
]

_logger = get_logger(__name__)


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

    fmt = to_strptime_format(fmt_raw)
    try:
        return datetime.strptime(m.group("date"), fmt).date()
    except ValueError:
        _logger.debug(
            "Could not parse date '%s' from filename '%s' using format '%s'",
            m.group("date"), filename, fmt,
        )
        return None


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


def keyed_record(raw_row: Any, file_type_cfg: dict) -> dict[str, Any]:
    """Return one raw row as a record keyed by what the declaration calls it.

    **THE FILE BOUNDARY, as one function.** A delimited row with a header
    arrives keyed already and is returned as it is. A positional row arrives
    as a list and a fixed-width row as a line, and neither can be looked up by
    name -- so each declared column's source is resolved here, by index or by
    offsets, and the value is stored under that source.

    What leaves is what every ``DataFile`` produces for every format, which is
    what lets the transform object name a source by key and take no positional
    branch at all.

    Args:
        raw_row: The row as its reader produced it -- a mapping, a sequence,
            or a line.
        file_type_cfg: The declaration, for its ``file_type`` and the sources
            its columns name.

    Returns:
        The record, keyed by source.
    """
    file_type = _normalize_file_type(file_type_cfg.get("file_type", "delimited_header"))
    if file_type in ("delimited_header", "CSV") and isinstance(raw_row, dict):
        # Already keyed by its own header, and returned WHOLE. Rebuilding it
        # from the declared sources would drop any field the declaration does
        # not name -- and a rule may still read one, because regex_extract,
        # prefix_map and regex_date are all given the record rather than a
        # single value.
        return dict(raw_row)

    keyed: dict[str, Any] = {}
    for col_cfg in file_type_cfg.get("columns", []) or []:
        source = col_cfg.get("source")
        if source is None:
            continue
        keyed[str(source)] = _resolve_source_value(raw_row, col_cfg, file_type)
    return keyed
