"""
Generic file I/O utilities.

Provides readers and writers for CSV and XLSX formats, output path
construction, and file listing. All functions are format-agnostic and
have no knowledge of any application's data model or transformation logic.

Row filtering is the caller's responsibility — an optional callable can be
passed to get_reader() and is applied per row. This module never imports
application-specific modules.

Public API
----------
input_files(folder, pattern)
    Return sorted list of files matching a glob pattern.
converted_output_path(base_dir, filename_pattern, substitutions)
    Construct an output path by substituting tokens into a filename pattern.
get_reader(infile, file_type, encoding, row_filter)
    Return a row iterator for the given file type.
write_file(outfile, rows, file_type)
    Write rows to a file in the specified format.
move_file(src, dest_dir)
    Move a file to a destination directory, creating dest_dir if needed.    
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Generator, Optional, TextIO

__all__ = [
    "input_files",
    "converted_output_path",
    "get_reader",
    "write_file",
    "scan_column_lengths",
    "move_file",
    "apply_file_movements",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def input_files(folder: Path, pattern: str) -> list[Path]:
    """
    Return a sorted list of files matching a glob pattern in folder.

    Parameters
    ----------
    folder : Path
        Directory to scan.
    pattern : str
        Glob pattern (e.g. '*.csv').

    Returns
    -------
    list[Path]
        Sorted list of matching file paths. Empty list if folder does
        not exist or no files match.
    """
    folder = Path(folder)
    if not folder.exists():
        _logger.debug("input_files: folder does not exist: %s", folder)
        return []
    return sorted(folder.glob(pattern))


def converted_output_path(
    base_dir: Path,
    filename_pattern: str,
    substitutions: dict[str, str],
) -> Path:
    """
    Construct a full output path by substituting tokens into a filename pattern.

    Parameters
    ----------
    base_dir : Path
        Directory where the output file will be written.
    filename_pattern : str
        Filename pattern string with {token} placeholders,
        e.g. '{source}_{name}_{start_date}_{end_date}.csv'.
    substitutions : dict[str, str]
        Token values to substitute into the pattern.

    Returns
    -------
    Path
        Absolute path for the output file.
    """
    filename = filename_pattern.format(**substitutions)
    return Path(base_dir) / filename


def get_reader(
    infile: Path,
    file_type: str = "CSV",
    encoding: str = "utf-8-sig",
    row_filter: Optional[Callable[[dict[str, str]], bool]] = None,
    header_line: Optional[str] = None,
) -> Generator[dict[str, str], None, None]:
    """
    Return a row iterator for the given file based on file_type.

    Rows for which row_filter returns False are excluded. If row_filter
    is None, all rows are yielded.

    Parameters
    ----------
    infile : Path
        Source file to read.
    file_type : str
        File format — 'CSV' or 'XLSX'. Case-insensitive.
    encoding : str
        Character encoding for CSV files. Defaults to 'utf-8-sig' to
        handle BOM-prefixed files from Windows applications.
    row_filter : Optional[Callable[[dict[str, str]], bool]]
        Optional predicate. Called with each raw row dict; rows where
        this returns False are skipped. None means no filtering.
    header_line : Optional[str]
        Exact header line to locate before reading rows. When provided,
        CSV reading begins only after this header is found in the file.

    Yields
    ------
    dict[str, str]
        One raw row dict per data row.

    Raises
    ------
    ValueError
        If file_type is not 'CSV' or 'XLSX'.
    """
    fmt = file_type.upper()
    if fmt == "CSV":
        yield from _csv_reader(
            infile,
            encoding=encoding,
            row_filter=row_filter,
            header_line=header_line,
        )
    elif fmt == "XLSX":
        yield from _xlsx_reader(infile, row_filter=row_filter)
    else:
        raise ValueError(f"Unsupported file_type '{file_type}'. Must be 'CSV' or 'XLSX'.")


def write_file(
    outfile: Path,
    rows: list[dict[str, Any]],
    file_type: str = "CSV",
) -> None:
    """
    Write rows to a file in the specified format.

    Parameters
    ----------
    outfile : Path
        Destination file path. Parent directories are created if needed.
    rows : list[dict[str, Any]]
        Rows to write. Must be non-empty.
    file_type : str
        Output format — 'CSV' or 'XLSX'. Case-insensitive.

    Raises
    ------
    ValueError
        If file_type is not 'CSV' or 'XLSX', or rows is empty.
    """
    if not rows:
        raise ValueError("write_file called with empty rows list.")

    fmt = file_type.upper()
    if fmt == "CSV":
        _csv_writer(outfile, rows)
    elif fmt == "XLSX":
        _xlsx_writer(outfile, rows)
    else:
        raise ValueError(f"Unsupported file_type '{file_type}'. Must be 'CSV' or 'XLSX'.")

def move_file(src: Path, dest_dir: Path, dest_name: Optional[str] = None) -> Path:
    """
    Move a file to a destination directory.

    Creates the destination directory if it does not exist. If a file
    with the same name already exists in dest_dir it is overwritten.

    Parameters
    ----------
    src : Path
        Full path of the file to move.
    dest_dir : Path
        Destination directory. Created if it does not exist.
    dest_name : Optional[str]
        Destination filename. If None, keeps src.name.

    Returns
    -------
    Path
        Full path of the file in its new location.

    Raises
    ------
    FileNotFoundError
        If src does not exist.
    OSError
        If the move fails for any reason.
    """
    src      = Path(src)
    dest_dir = Path(dest_dir)

    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (dest_name if dest_name else src.name)
    src.replace(dest)
    _logger.debug("Moved: %s → %s", src, dest)
    return dest


def apply_file_movements(paths: Any, file_movements: Any) -> int:
    """Apply file movement rules for one data source.

    This pipeline is independent from transform/load and is intended for
    pre-transform intake (e.g. downloads folder -> inbox folder).

    Supported name transforms:
        - date_range_from_column

    Parameters
    ----------
    paths : Any
        Data-source paths namespace.
    file_movements : Any
        file_movements namespace containing filename_pattern, name_transforms,
        and success movement instructions.

    Returns
    -------
    int
        Number of files moved.

    Raises
    ------
    ValueError
        If required movement config is missing or invalid.
    """
    if file_movements is None:
        return 0

    pattern: str = getattr(file_movements, "filename_pattern", "*")
    success: Any = getattr(file_movements, "success", None)
    if not success:
        return 0

    first_move = getattr(success[0], "move", None)
    if first_move is None:
        raise ValueError("file_movements.success[0].move is required.")

    source_key = getattr(first_move, "from", None)
    dest_key = getattr(first_move, "to", None)
    if source_key is None or dest_key is None:
        raise ValueError("file_movements.success[0].move requires 'from' and 'to'.")

    source_dir = _resolve_path_key(paths, source_key)
    dest_dir = _resolve_path_key(paths, dest_key)
    files = input_files(source_dir, pattern)

    if not files:
        return 0

    name_transforms: list[Any] = list(getattr(file_movements, "name_transforms", []) or [])

    moved = 0
    for file_path in files:
        dest_name = _resolve_dest_name(file_path, name_transforms)
        move_file(file_path, dest_dir, dest_name=dest_name)
        moved += 1

    return moved


def scan_column_lengths(
    files: list[Path],
    file_type: str = "CSV",
    encoding: str = "utf-8-sig",
) -> dict[str, int]:
    """
    Scan a list of files and return the maximum observed length per column.

    Reads every row in every file and tracks the longest value seen for
    each column name. Used to size VARCHAR columns when auto-creating a
    staging table — caller adds a buffer before passing to DDL.

    Returns an empty dict if files is empty or no rows are found.

    Parameters
    ----------
    files : list[Path]
        Files to scan. All must share the same column structure.
    file_type : str
        File format — 'CSV' or 'XLSX'. Case-insensitive.
    encoding : str
        Character encoding for CSV files.

    Returns
    -------
    dict[str, int]
        Mapping of column name → maximum observed value length in characters.
        Columns with all-blank values have length 0.
    """
    max_lengths: dict[str, int] = {}

    for file_path in files:
        for row in get_reader(file_path, file_type=file_type, encoding=encoding):
            for col, val in row.items():
                length = len(val) if val else 0
                if col not in max_lengths or length > max_lengths[col]:
                    max_lengths[col] = length

    return max_lengths

# ---------------------------------------------------------------------------
# Private — CSV reader / writer
# ---------------------------------------------------------------------------

def _csv_reader(
    infile: Path,
    encoding: str,
    row_filter: Optional[Callable[[dict[str, str]], bool]],
    header_line: Optional[str],
) -> Generator[dict[str, str], None, None]:
    """
    Yield data rows from a CSV file.

    Skips blank lines and mid-file header-repeat rows. Applies row_filter
    when provided. The file handle is managed via a context manager.
    """
    with infile.open(newline="", encoding=encoding, errors="replace") as fh:
        header = ""
        if header_line is None:
            # Skip blank lines to find the first non-empty header line.
            for line in fh:
                stripped = line.strip()
                if stripped:
                    header = stripped
                    break
        else:
            # Scan until the matched header line is found.
            for line in fh:
                stripped = line.strip()
                if stripped == header_line:
                    header = stripped
                    break

        if not header:
            return

        fieldnames = [c.strip() for c in header.split(",")]
        reader     = csv.DictReader(fh, fieldnames=fieldnames)

        for row in reader:
            # Skip blank rows, including rows that contain only whitespace.
            if not any((v or "").strip() for v in row.values()):
                continue

            # Skip repeated header rows embedded mid-file.
            first_col = fieldnames[0] if fieldnames else ""
            if row.get(first_col, "").strip() == first_col:
                continue

            if row_filter is not None and not row_filter(row):
                continue

            yield row


def _resolve_path_key(paths: Any, key: str) -> Path:
    """Resolve a named path key from a data-source paths namespace."""
    value = getattr(paths, key, None)
    if value is None:
        raise ValueError(f"Path key '{key}' not found in data source paths.")
    return Path(value)


def _resolve_dest_name(file_path: Path, name_transforms: list[Any]) -> str:
    """Resolve final destination filename after applying name transforms."""
    if not name_transforms:
        return file_path.name

    transform = name_transforms[0]
    transform_type: str = getattr(transform, "type", "")

    if transform_type == "date_range_from_column":
        return _apply_date_range_from_column(file_path, transform)

    raise ValueError(f"Unsupported name_transform type: '{transform_type}'.")


def _apply_date_range_from_column(file_path: Path, transform: Any) -> str:
    """Create destination filename using min/max date values from one CSV column."""
    source_column: str = getattr(transform, "source_column")
    date_format: str = getattr(transform, "date_format")
    output_template: str = getattr(transform, "output_template")
    output_format: str = getattr(transform, "output_format")

    start_date, end_date = _read_date_range_from_column(
        file_path=file_path,
        source_column=source_column,
        date_format=date_format,
    )
    return output_template.format(
        start_date=start_date.strftime(output_format),
        end_date=end_date.strftime(output_format),
    )


def _read_date_range_from_column(
    file_path: Path,
    source_column: str,
    date_format: str,
) -> tuple[datetime, datetime]:
    """Read CSV rows and return (min_date, max_date) from source_column."""
    dates: list[datetime] = []

    with file_path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if source_column not in (reader.fieldnames or []):
            fh.seek(0)
            reader = _find_header_reader(fh, source_column)

        for row in reader:
            raw = (row.get(source_column, "") or "").strip()
            if not raw:
                continue
            try:
                dates.append(datetime.strptime(raw, date_format))
            except ValueError:
                _logger.debug(
                    "Skipping unparseable %s value '%s' in %s",
                    source_column,
                    raw,
                    file_path.name,
                )

    if not dates:
        raise ValueError(
            f"No parseable values found for column '{source_column}' in '{file_path.name}'."
        )

    return min(dates), max(dates)


def _find_header_reader(fh: TextIO, source_column: str) -> csv.DictReader:
    """Return DictReader positioned at the header line that contains source_column."""
    for line in fh:
        if source_column in line:
            remaining = line + fh.read()
            return csv.DictReader(StringIO(remaining))
    raise ValueError(f"Column '{source_column}' not found in file.")


def _csv_writer(outfile: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to a CSV file using the key order of the first row.

    Uses QUOTE_NONE so values are written exactly as-is — no extra quoting
    or escaping is applied by the CSV writer. Values that already contain
    quote characters (e.g. constants configured with quote: '"') are written
    verbatim.
    """
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    with outfile.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=list(rows[0].keys()),
            quoting=csv.QUOTE_NONE,
            quotechar="\x00",
            escapechar="\\",
        )
        writer.writeheader()
        writer.writerows(rows)

    _logger.debug("Wrote %d row(s) to %s", len(rows), outfile.name)


# ---------------------------------------------------------------------------
# Private — XLSX reader / writer
# ---------------------------------------------------------------------------




def _xlsx_reader(
    infile: Path,
    row_filter: Optional[Callable[[dict[str, str]], bool]],
) -> Generator[dict[str, str], None, None]:
    """
    Yield rows from an XLSX file as string dicts.

    pandas is imported on demand — it is a heavy optional dependency.
    """
    import pandas as pd  # noqa: PLC0415

    df = pd.read_excel(infile)
    for record in df.to_dict(orient="records"):
        row = {str(k): str(v) if v is not None else "" for k, v in record.items()}
        if row_filter is not None and not row_filter(row):
            continue
        yield row


def _xlsx_writer(outfile: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to an XLSX file. pandas imported on demand."""
    import pandas as pd  # noqa: PLC0415

    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(rows).to_excel(outfile, index=False)
    _logger.debug("Wrote %d row(s) to %s", len(rows), outfile.name)
