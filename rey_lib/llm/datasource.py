"""
First-class data source abstractions for the analysis framework.

A DataSource knows how to extract raw data from one origin system. It is
contract-agnostic — column filtering, required filters, sampling, and redaction
are all applied later by the preparation pipeline.

Database access goes exclusively through ``DBAdapter`` from
``rey_lib.db.db_adapter``.  No backend-specific imports belong here.

Public API
----------
SourceData
    Result of one extraction: rows (or raw text), metadata, content hash.
DataSource
    Abstract base class.  Subclass to add custom source types.
DBDataSource
    Extracts rows via DBAdapter.fetch_dicts() — backend-agnostic.
CSVDataSource
    Streams rows from a CSV file.
ExcelDataSource
    Reads rows from an Excel worksheet (openpyxl read-only mode).
TextDataSource
    Wraps a plain-text string as a non-tabular source.
"""

from __future__ import annotations

import csv
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SourceData",
    "DataSource",
    "DBDataSource",
    "CSVDataSource",
    "ExcelDataSource",
    "TextDataSource",
]

_DEFAULT_MAX_EXTRACT = 10_000


@dataclass(frozen=True)
class SourceData:
    """Result of one DataSource extraction.

    Tabular sources populate ``rows`` and ``columns``; text sources populate
    ``raw_text`` and leave rows empty.

    Attributes
    ----------
    rows : list[dict[str, Any]]
        Extracted rows as column → value dicts.  Empty for text sources.
    raw_text : str
        Source text for TextDataSource.  Empty for tabular sources.
    columns : list[str]
        Column names present in ``rows``.  Empty for text sources.
    row_count : int
        Number of rows in ``rows`` (0 for text sources).
    source_ref : str
        Human-readable label used in audit records (file name, query hash, …).
    truncated : bool
        True when the source had more rows than ``max_extract`` allowed.
    source_hash : str
        SHA-256 of the serialised extracted content.
    """

    rows:        list[dict[str, Any]]
    raw_text:    str
    columns:     list[str]
    row_count:   int
    source_ref:  str
    truncated:   bool
    source_hash: str


class DataSource(ABC):
    """Abstract base class for all data sources.

    Implementations must be safe to call multiple times — ``extract()`` must
    return the same logical data on each invocation.
    """

    @abstractmethod
    def extract(self, max_extract: int = _DEFAULT_MAX_EXTRACT) -> SourceData:
        """Extract raw data from the source.

        The preparation pipeline applies further limits (contract max_rows,
        sampling).  Set ``max_extract`` high enough that the sampler has
        enough data to work with — typically ``max_rows * 10`` or more.

        Parameters
        ----------
        max_extract : int
            Hard upper bound on rows extracted.  Prevents runaway queries on
            very large sources.

        Returns
        -------
        SourceData
            Extracted rows (or raw text) with metadata and a content hash.
        """


class DBDataSource(DataSource):
    """Extract rows from an already-open database connection via DBAdapter.

    The caller owns the connection lifecycle (open, commit, close).  This
    class only calls ``DBAdapter.fetch_dicts()`` — it never opens or closes
    a connection itself.  This matches the pattern used throughout rey_lib:
    connections are opened once at the application level from YAML config and
    passed into the call chain.

    Parameters
    ----------
    conn : Any
        An already-open backend connection (obtained from the db lib).
    sql_name : str
        Named SQL query (filename stem without .sql extension) loaded via the
        backend's ``init_db`` / ``load_sql`` mechanism.
    params : Optional[list[Any]]
        Positional query parameters forwarded to the backend.
    ref : str
        Human-readable label for audit records (e.g. connection + query name).
    """

    def __init__(
        self,
        conn:     Any,
        sql_name: str,
        params:   Optional[list[Any]] = None,
        ref:      str                 = "",
    ) -> None:
        """Initialise with an open connection and a named query."""
        self._conn     = conn
        self._sql_name = sql_name
        self._params   = params
        self._ref      = ref or f"db:{sql_name}"

    def extract(self, max_extract: int = _DEFAULT_MAX_EXTRACT) -> SourceData:
        """Execute the named query on the open connection and return rows."""
        from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415

        rows: list[dict[str, Any]] = DBAdapter().fetch_dicts(
            self._conn, self._sql_name, self._params
        )

        truncated = len(rows) > max_extract
        rows      = rows[:max_extract]
        col_names = list(rows[0].keys()) if rows else []

        return SourceData(
            rows        = rows,
            raw_text    = "",
            columns     = col_names,
            row_count   = len(rows),
            source_ref  = self._ref,
            truncated   = truncated,
            source_hash = _hash_rows(rows),
        )


class CSVDataSource(DataSource):
    """Stream rows from a CSV file.

    Never loads the full file into memory — reads row-by-row and stops at
    ``max_extract``.

    Parameters
    ----------
    path : Path
        Path to the CSV file.  UTF-8 and UTF-8-BOM encodings are supported.
    """

    def __init__(self, path: Path) -> None:
        """Initialise with the CSV file path."""
        self._path = Path(path)

    def extract(self, max_extract: int = _DEFAULT_MAX_EXTRACT) -> SourceData:
        """Read up to ``max_extract`` rows from the CSV file."""
        rows: list[dict[str, Any]] = []
        truncated = False
        with self._path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if len(rows) >= max_extract:
                    truncated = True
                    break
                rows.append(dict(row))

        col_names = list(rows[0].keys()) if rows else []
        return SourceData(
            rows        = rows,
            raw_text    = "",
            columns     = col_names,
            row_count   = len(rows),
            source_ref  = self._path.name,
            truncated   = truncated,
            source_hash = _hash_rows(rows),
        )


class ExcelDataSource(DataSource):
    """Read rows from an Excel worksheet using openpyxl read-only mode.

    Auto-detects the header row (first row where every non-empty cell is a
    non-empty string).  Blank and metadata rows above the header are skipped.

    Parameters
    ----------
    path : Path
        Path to the ``.xlsx`` file.
    sheet : Optional[str]
        Sheet name.  Defaults to the active (first) sheet.
    """

    def __init__(self, path: Path, sheet: Optional[str] = None) -> None:
        """Initialise with file path and optional sheet name."""
        self._path  = Path(path)
        self._sheet = sheet

    def extract(self, max_extract: int = _DEFAULT_MAX_EXTRACT) -> SourceData:
        """Read up to ``max_extract`` data rows from the worksheet."""
        import openpyxl  # noqa: PLC0415 — optional dependency

        wb = openpyxl.load_workbook(self._path, read_only=True, data_only=True)
        ws = wb[self._sheet] if self._sheet else wb.active

        headers:  Optional[list[str]]  = None
        pre_buf:  list[tuple]          = []
        rows:     list[dict[str, Any]] = []
        truncated = False

        for raw_row in ws.iter_rows(values_only=True):
            if headers is None:
                pre_buf.append(raw_row)
                if raw_row and all(
                    isinstance(c, str) and c.strip()
                    for c in raw_row
                    if c is not None
                ):
                    headers = [str(c).strip() if c is not None else "" for c in raw_row]
            else:
                if any(c is not None for c in raw_row):
                    if len(rows) >= max_extract:
                        truncated = True
                        break
                    rows.append(
                        {headers[j]: (raw_row[j] if j < len(raw_row) else None)
                         for j in range(len(headers))}
                    )

        wb.close()

        # Fallback: treat first non-blank pre-header row as the header.
        if headers is None:
            for i, buf_row in enumerate(pre_buf):
                if any(c is not None for c in buf_row):
                    headers = [str(c).strip() if c is not None else "" for c in buf_row]
                    for data_row in pre_buf[i + 1:]:
                        if any(c is not None for c in data_row):
                            if len(rows) >= max_extract:
                                truncated = True
                                break
                            rows.append(
                                {headers[j]: (data_row[j] if j < len(data_row) else None)
                                 for j in range(len(headers))}
                            )
                    break

        col_names = list(headers) if headers else []
        ref = self._path.name + (f"[{self._sheet}]" if self._sheet else "")
        return SourceData(
            rows        = rows,
            raw_text    = "",
            columns     = col_names,
            row_count   = len(rows),
            source_ref  = ref,
            truncated   = truncated,
            source_hash = _hash_rows(rows),
        )


class TextDataSource(DataSource):
    """Wrap a plain-text string as a non-tabular source.

    Used when input is free-form text (reports, documents, notes).  The
    preparation pipeline skips all tabular stages and passes the text through
    with optional text-level redaction only.

    Parameters
    ----------
    text : str
        The input text.
    ref : str
        Human-readable label shown in audit records.
    """

    def __init__(self, text: str, ref: str = "text") -> None:
        """Initialise with text content and an optional audit label."""
        self._text = text
        self._ref  = ref

    def extract(self, max_extract: int = _DEFAULT_MAX_EXTRACT) -> SourceData:
        """Return the text wrapped in a SourceData with empty rows."""
        content_hash = hashlib.sha256(self._text.encode("utf-8")).hexdigest()
        return SourceData(
            rows        = [],
            raw_text    = self._text,
            columns     = [],
            row_count   = 0,
            source_ref  = self._ref,
            truncated   = False,
            source_hash = content_hash,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _hash_rows(rows: list[dict[str, Any]]) -> str:
    """Return the SHA-256 hex digest of the JSON-serialised row list."""
    content = json.dumps(rows, default=str, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


