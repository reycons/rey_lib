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
]

_logger = logging.getLogger(__name__)

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


def get_connection() -> duckdb.DuckDBPyConnection:
    """
    Return an open DuckDB connection.

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


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _db_path is None:
        raise RuntimeError(
            "duckdb_utils.init_db() must be called before using the database."
        )
