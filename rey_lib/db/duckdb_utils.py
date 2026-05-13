"""
Generic DuckDB connection and query execution layer.

This module owns the database connection, SQL file loading, and raw query
execution. It has no knowledge of any application table, column, or business
rule — those belong in the application layer.

SQL files are loaded from a directory supplied at startup via init_db().
No SQL directory is assumed or hardcoded.

Public API
----------
init_db(db_path, sql_dir)           Set database path and load SQL files.
get_connection()                    Return an open DuckDB connection.
execute(conn, sql_name, params)     Execute a named SQL file.
fetch(conn, sql_name, params)       Execute and return all rows as raw tuples.
load_sql(name)                      Return the SQL string for a named query.
DB_PATH                             Public alias to the current database file path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import duckdb

__all__ = [
    "DB_PATH",
    "init_db",
    "get_connection",
    "execute",
    "fetch",
    "load_sql",
    "bulk_insert",
    "create_staging_table_if_not_exists",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Neutral → DuckDB type map
#
# Callers may pass backend-neutral type names (e.g. 'TEXT', 'NVARCHAR(MAX)').
# Anything not in this map is used as-is.
# ---------------------------------------------------------------------------

_NEUTRAL_TYPE_MAP: dict[str, str] = {
    "TEXT":         "VARCHAR",
    "NVARCHAR":     "VARCHAR",
    "TIMESTAMP":    "TIMESTAMP",
    "INTEGER":      "INTEGER",
    "INT":          "INTEGER",
    "DATE":         "DATE",
}

# Pattern that matches NVARCHAR(n) / VARCHAR(n) with an explicit length.
_NVARCHAR_RE = re.compile(r"^N?VARCHAR\s*\(\s*\d+\s*\)$", re.IGNORECASE)
_NVARCHAR_MAX_RE = re.compile(r"^N?VARCHAR\s*\(\s*MAX\s*\)$", re.IGNORECASE)


def _map_type(sql_type: str) -> str:
    """Return the DuckDB equivalent of a neutral or SQL Server type."""
    upper = sql_type.strip().upper()
    if upper in _NEUTRAL_TYPE_MAP:
        return _NEUTRAL_TYPE_MAP[upper]
    if _NVARCHAR_MAX_RE.match(upper) or _NVARCHAR_RE.match(upper):
        return "VARCHAR"
    return sql_type


# ---------------------------------------------------------------------------
# Module-level state — set once at startup by init_db()
# ---------------------------------------------------------------------------

# Unset until init_db() is called — raises clearly if called too early.
_db_path: Path | None = None
_SQL: dict[str, str]  = {}

# Public alias — readable by application code.
DB_PATH: Path | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(db_path: Path, sql_dir: Path) -> None:
    """
    Set the database file path and load all SQL files from sql_dir.

    Must be called once at application startup before any call to
    get_connection() or execute(). Calling it again replaces the existing
    state — useful in tests.

    Parameters
    ----------
    db_path : Path
        Path to the DuckDB database file. Created on first connection if
        it does not exist. Parent directories are created automatically.
    sql_dir : Path
        Directory containing .sql files. All *.sql files are loaded at
        this point — failure is immediate if the directory is missing.

    Raises
    ------
    FileNotFoundError
        If sql_dir does not exist.
    """
    global _db_path, DB_PATH, _SQL

    if not sql_dir.exists():
        raise FileNotFoundError(
            f"SQL directory not found: {sql_dir}\n"
            f"Expected a directory containing *.sql files."
        )

    _db_path = Path(db_path).expanduser().resolve()
    DB_PATH  = _db_path

    _SQL = {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(sql_dir.glob("*.sql"))
    }

    _logger.debug(
        "DuckDB initialised — db: %s, sql: %s (%d file(s))",
        _db_path, sql_dir, len(_SQL),
    )


def get_connection(db_cfg: Any = None) -> duckdb.DuckDBPyConnection:
    """
    Return an open DuckDB connection.

    ``db_cfg`` is accepted for interface compatibility with other backends
    but ignored — DuckDB uses the path set by ``init_db()``.

    Creates the database file and its parent directories if they do not
    exist. No schema DDL is run — that is the application's responsibility.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Open connection. Caller is responsible for closing it.

    Raises
    ------
    RuntimeError
        If init_db() has not been called yet.
    """
    _require_init()
    _db_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    return duckdb.connect(str(_db_path))


def execute(
    conn: duckdb.DuckDBPyConnection,
    sql_name: str,
    params: Optional[list[Any]] = None,
) -> Any:
    """
    Execute a named SQL query and return the raw DuckDB result.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    sql_name : str
        SQL filename stem without .sql extension (e.g. 'insert_trade').
    params : Optional[list[Any]]
        Positional query parameters.

    Returns
    -------
    Any
        Raw DuckDB result. Call .fetchall(), .fetchone(), or check
        .rowcount depending on the query type.

    Raises
    ------
    KeyError
        If sql_name is not found in the loaded SQL dict.
    """
    return conn.execute(load_sql(sql_name), params or [])


def fetch(
    conn: duckdb.DuckDBPyConnection,
    sql_name: str,
    params: Optional[list[Any]] = None,
) -> list[tuple]:
    """
    Execute a named SQL query and return all rows as raw tuples.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    sql_name : str
        SQL filename stem without .sql extension.
    params : Optional[list[Any]]
        Positional query parameters.

    Returns
    -------
    list[tuple]
        All result rows as tuples. Empty list if no rows matched.
    """
    return conn.execute(load_sql(sql_name), params or []).fetchall()


def fetch_dicts(
    conn: duckdb.DuckDBPyConnection,
    sql_name: str,
    params: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
    """Execute a named SQL query and return all rows as a list of dicts.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    sql_name : str
        SQL filename stem without .sql extension.
    params : Optional[list[Any]]
        Positional query parameters.

    Returns
    -------
    list[dict[str, Any]]
        All result rows as column → value dicts. Empty list if no rows matched.
    """
    result    = conn.execute(load_sql(sql_name), params or [])
    col_names = [d[0] for d in result.description]
    return [dict(zip(col_names, row)) for row in result.fetchall()]


def load_sql(name: str) -> str:
    """
    Return the preloaded SQL string for a named query.

    Parameters
    ----------
    name : str
        SQL filename stem without .sql extension.

    Returns
    -------
    str
        SQL text ready for execution.

    Raises
    ------
    RuntimeError
        If init_db() has not been called yet.
    KeyError
        If no SQL file with that stem was found in the configured sql_dir.
    """
    _require_init()
    if name not in _SQL:
        raise KeyError(
            f"SQL query '{name}' not found. "
            f"Available: {sorted(_SQL.keys())}"
        )
    return _SQL[name]


def bulk_insert(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> int:
    """
    Insert rows into schema.table using executemany.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    schema : str
        Target schema name (e.g. 'main').
    table : str
        Target table name.
    rows : list[dict[str, Any]]
        Row dicts; keys must include every entry of columns.
    columns : list[str]
        Column names defining insert order.

    Returns
    -------
    int
        Number of rows inserted.
    """
    if not rows:
        return 0
    placeholders = ", ".join(["?" ] * len(columns))
    col_list     = ", ".join(f'"{c}"' for c in columns)
    sql          = (
        f'INSERT INTO "{schema}"."{table}" ({col_list}) '
        f'VALUES ({placeholders})'
    )
    values = [tuple(row[c] for c in columns) for row in rows]
    conn.executemany(sql, values)
    return len(rows)


def create_staging_table_if_not_exists(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
    column_defs: list[tuple[str, str]],
) -> bool:
    """
    Create a table in DuckDB if it does not already exist.

    All columns are created nullable. Neutral type names (TEXT, TIMESTAMP,
    etc.) are mapped to DuckDB equivalents; SQL Server types are also
    normalised so mixed callers work without changes.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    schema : str
        Target schema name (e.g. 'main').
    table : str
        Target table name.
    column_defs : list[tuple[str, str]]
        Ordered list of (column_name, sql_type) tuples.

    Returns
    -------
    bool
        True if the table was created on this call; False if it already existed.
    """
    existed = _table_exists(conn, schema, table)
    if existed:
        return False

    col_sql = ",\n    ".join(
        f'"{col}" {_map_type(sql_type)}'
        for col, sql_type in column_defs
    )
    ddl = f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n    {col_sql}\n)'
    conn.execute(ddl)
    _logger.info("Staging table ready: %s.%s", schema, table)
    return True


def _table_exists(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
) -> bool:
    """Return True if schema.table exists in the DuckDB catalog."""
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchall()
    return len(rows) > 0


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _db_path is None:
        raise RuntimeError(
            "duckdb_utils.init_db() must be called before using the database."
        )
