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
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Callable, Generator, Optional

__all__ = [
    "input_files",
    "converted_output_path",
    "get_reader",
    "write_file",
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
        yield from _csv_reader(infile, encoding=encoding, row_filter=row_filter)
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


# ---------------------------------------------------------------------------
# Private — CSV reader / writer
# ---------------------------------------------------------------------------

def _csv_reader(
    infile: Path,
    encoding: str,
    row_filter: Optional[Callable[[dict[str, str]], bool]],
) -> Generator[dict[str, str], None, None]:
    """
    Yield data rows from a CSV file.

    Skips blank lines and mid-file header-repeat rows. Applies row_filter
    when provided. The file handle is managed via a context manager.
    """
    with infile.open(newline="", encoding=encoding, errors="replace") as fh:
        # Skip blank lines to find the first non-empty header line.
        header = ""
        for line in fh:
            stripped = line.strip()
            if stripped:
                header = stripped
                break

        if not header:
            return

        fieldnames = [c.strip() for c in header.split(",")]
        reader     = csv.DictReader(fh, fieldnames=fieldnames)

        for row in reader:
            # Skip blank rows.
            if not any(v for v in row.values() if v):
                continue

            # Skip repeated header rows embedded mid-file.
            first_col = fieldnames[0] if fieldnames else ""
            if row.get(first_col, "").strip() == first_col:
                continue

            if row_filter is not None and not row_filter(row):
                continue

            yield row


def _csv_writer(outfile: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to a CSV file using the key order of the first row."""
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    with outfile.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
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
