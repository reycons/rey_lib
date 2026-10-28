"""
Input resolution helpers for file_operator.

Handles layout detection, input file enumeration, and CLI argument parsing
for column names and fixed-width ranges.  All functions are pure or read-only
with respect to the filesystem.

Public API
----------
resolve_layout          Determine effective layout for a file.
resolve_input_files     Enumerate files from --input or --inbox args.
parse_column_list       Parse a comma-separated column name string.
parse_ranges            Parse a '2-13,42-53' range string into tuples.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.files.file_utils import matched_tree_files
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import AppLogger

from rey_lib.files.error_utils import InputError, LayoutError

__all__: list[str] = [
    "resolve_layout",
    "resolve_input_files",
    "parse_column_list",
    "parse_ranges",
]

_log = AppLogger(__name__)

# Supported layout identifiers.
LAYOUTS: frozenset[str] = frozenset({"delimited", "fixed_width", "excel", "auto"})

_DELIMITED_EXTS:   frozenset[str] = frozenset({".csv", ".tsv", ".txt", ".dat", ".pipe"})
_FIXED_WIDTH_EXTS: frozenset[str] = frozenset({".txt", ".dat", ".fix", ".fwf"})
_EXCEL_EXTS:       frozenset[str] = frozenset({".xlsx", ".xlsm", ".xls"})


def resolve_layout(file_path: Path, requested: str) -> str:
    """Return the effective layout, auto-detecting from extension when needed.

    When ``requested`` is ``'auto'``, the extension is used to detect the
    layout.  ``.txt`` and ``.dat`` are ambiguous (both delimited and
    fixed-width use them); they default to ``'delimited'``.

    Parameters
    ----------
    file_path : Path
        Source file — extension used for auto-detection.
    requested : str
        Layout requested by the caller, or ``'auto'``.

    Returns
    -------
    str
        One of ``'delimited'``, ``'fixed_width'``, ``'excel'``.

    Raises
    ------
    LayoutError
        When ``requested`` is not ``'auto'`` and is not a known layout,
        or when auto-detection finds an unrecognised extension.
    """
    if requested != "auto":
        if requested not in LAYOUTS:
            raise LayoutError(f"Unknown layout '{requested}'.")
        return requested

    ext = file_path.suffix.lower()

    if ext in _EXCEL_EXTS:
        return "excel"

    # .txt and .dat overlap — default to delimited; caller may force fixed_width.
    if ext in _DELIMITED_EXTS:
        return "delimited"

    raise LayoutError(
        f"Cannot auto-detect layout for extension '{ext}' on '{file_path.name}'. "
        "Pass --layout explicitly."
    )


def resolve_input_files(args: object, log: AppLogger) -> list[Path]:
    """Return the list of files to process from --input or --inbox args.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Must expose ``input``, ``inbox``, and
        ``file_pattern`` attributes.
    log : AppLogger
        Logger for diagnostic messages.

    Returns
    -------
    list[Path]
        Sorted list of existing file paths to process.  Empty when no files
        are found.

    Raises
    ------
    InputError
        When the specified input file or inbox directory does not exist.
    """
    # Single-file mode.
    if getattr(args, "input", None):
        p = Path(args.input)
        if not p.exists():
            raise InputError(f"Input file not found: {p}")
        return [p]

    # Inbox mode.
    inbox   = Path(args.inbox)
    pattern = getattr(args, "file_pattern", None) or "*"

    if not inbox.is_dir():
        raise InputError(f"Inbox directory not found: {inbox}")

    files = matched_tree_files(inbox, pattern)

    if not files:
        log.warning("No files matched pattern '%s' in '%s'.", pattern, inbox)

    return files


def parse_column_list(raw: str | None) -> list[str]:
    """Parse a comma-separated column name string into a list of stripped names.

    Parameters
    ----------
    raw : str | None
        Raw CLI value, e.g. ``'ACCOUNT NUMBER,MASTER ACCOUNT'``.

    Returns
    -------
    list[str]
        Stripped, non-empty column names.  Empty list when ``raw`` is
        ``None`` or blank.
    """
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def parse_ranges(raw: str | None) -> tuple[list[tuple[int, int]], list[str]]:
    """Parse a '2-13,42-53' range string into column ranges and auto-names.

    Ranges are 1-based and inclusive on both ends, following the fixed-width
    convention used in the redaction library.

    Parameters
    ----------
    raw : str | None
        Raw CLI value, e.g. ``'2-13,42-53'``.

    Returns
    -------
    tuple[list[tuple[int, int]], list[str]]
        ``(ranges, names)`` where ``ranges`` is a list of ``(start, end)``
        tuples and ``names`` is a corresponding list of auto-generated column
        names like ``'RANGE_2_13'``.

    Raises
    ------
    ValueError
        When a range token cannot be parsed as ``start-end`` integers.
    """
    if not raw:
        return [], []

    ranges: list[tuple[int, int]] = []
    names:  list[str]             = []

    for part in raw.split(","):
        part = part.strip()
        try:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
        except ValueError as exc:
            raise ConfigError(f"Invalid range token '{part}': expected 'start-end'.") from exc

        ranges.append((start, end))
        names.append(f"RANGE_{start}_{end}")

    return ranges, names
