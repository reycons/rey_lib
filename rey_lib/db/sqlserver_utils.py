"""
Generic SQL Server connection and query execution layer.

Owns all SQL Server connections, query execution, transaction handling,
and bulk loading. No raw pyodbc calls are permitted outside this module.

Connection details are passed as a Namespace object resolved from ctx at
call time — this module has no knowledge of ctx structure or application
config layout. The caller is responsible for resolving the correct
connection from ctx and passing it here.

Windows Authentication is used when db_cfg.user is absent or empty.
SQL Server Authentication is used otherwise. Passwords must be injected
from .env via inject_secrets() before get_connection() is called —
they are never read from YAML.

Bulk inserts use fast_executemany=True on the pyodbc cursor, which
batches all rows into a single server round-trip. This provides
significantly better throughput than row-by-row execution without
requiring bcp.exe or filesystem access from SQL Server.

All named queries must be defined in .sql files loaded via init_db().
String-formatted SQL is forbidden — parameterized execution only.

Public API
----------
init_db(sql_dir)
    Preload all .sql files from sql_dir. Call once at startup.
get_connection(db_cfg)
    Return an open pyodbc connection with retry and explicit timeout.
execute(conn, sql_name, params)
    Execute a named SQL file. Return the open cursor.
fetch(conn, sql_name, params)
    Execute and return all rows as list[dict[str, Any]].
bulk_insert(conn, schema, table, rows, columns)
    Insert a list of row dicts into any table using fast_executemany.
call_proc(conn, proc_name, params)
    Execute a stored procedure by name.
load_sql(name)
    Return the preloaded SQL string for a named query.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import pyodbc

from rey_lib.errors.error_utils import DatabaseError

__all__ = [
    "init_db",
    "get_connection",
    "execute",
    "fetch",
    "bulk_insert",
    "call_proc",
    "load_sql",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — set once at startup by init_db()
# ---------------------------------------------------------------------------

# Unset until init_db() is called — raises clearly if called too early.
_sql_dir: Path | None = None
_SQL: dict[str, str]  = {}

# Retry settings for connection attempts.
_MAX_CONNECT_ATTEMPTS: int   = 3
_CONNECT_BACKOFF_BASE: float = 1.0   # seconds; doubles on each retry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(sql_dir: Path) -> None:
    """
    Set the SQL directory and preload all .sql files.

    Must be called once at application startup before any call to
    get_connection() or execute(). Calling again replaces existing
    state — safe to use in tests.

    Parameters
    ----------
    sql_dir : Path
        Directory containing .sql files. All *.sql files are loaded
        immediately. Fails fast if the directory does not exist.

    Raises
    ------
    FileNotFoundError
        If sql_dir does not exist on disk.
    """
    global _sql_dir, _SQL

    sql_dir = Path(sql_dir).resolve()
    if not sql_dir.exists():
        raise FileNotFoundError(
            f"SQL directory not found: {sql_dir}\n"
            f"Expected a directory containing *.sql files."
        )

    _sql_dir = sql_dir
    _SQL     = {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(sql_dir.glob("*.sql"))
    }

    _logger.debug(
        "sqlserver_utils initialised — sql_dir: %s (%d file(s) loaded)",
        sql_dir,
        len(_SQL),
    )


def get_connection(db_cfg: Any) -> pyodbc.Connection:
    """
    Return an open pyodbc connection built from a db_cfg Namespace.

    Retries up to _MAX_CONNECT_ATTEMPTS times with exponential backoff.
    Uses Windows Authentication when db_cfg.user is absent or empty;
    SQL Server Authentication otherwise.

    The connection is returned with autocommit=False. The caller is
    responsible for commit() and rollback(). Always close the connection
    explicitly or use it as a context manager.

    Parameters
    ----------
    db_cfg : Any
        Namespace for a single database connection. Expected attributes:
            host      str   — server hostname or (local)
            database  str   — target database name
            driver    str   — ODBC driver name
            port      int   — SQL Server port (default 1433)
            timeout   int   — login timeout in seconds (default 30)
            user      str   — optional; omit for Windows Authentication
            password  str   — optional; injected from .env at startup

    Returns
    -------
    pyodbc.Connection
        Open connection with autocommit disabled.

    Raises
    ------
    DatabaseError
        If all connection attempts are exhausted.
    """
    _require_init()
    conn_str = _build_connection_string(db_cfg)
    timeout  = int(getattr(db_cfg, "timeout", 30))
    return _connect_with_retry(conn_str, timeout)


def execute(
    conn: pyodbc.Connection,
    sql_name: str,
    params: Optional[list[Any]] = None,
) -> pyodbc.Cursor:
    """
    Execute a named SQL query and return the open cursor.

    The caller is responsible for reading results, closing the cursor,
    and managing the transaction. The cursor is not auto-closed so that
    the caller can inspect rowcount or iterate results.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection.
    sql_name : str
        SQL filename stem without .sql extension
        (e.g. 'insert_file_log_staging').
    params : Optional[list[Any]]
        Positional parameters bound via parameterized execution.
        Never use string formatting to inject values.

    Returns
    -------
    pyodbc.Cursor
        Executed cursor. Caller must close it when done.

    Raises
    ------
    KeyError
        If sql_name is not found in the loaded SQL dict.
    DatabaseError
        If execution fails.
    """
    sql    = load_sql(sql_name)
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params or [])
        return cursor
    except pyodbc.Error as exc:
        cursor.close()
        raise DatabaseError(
            f"Query '{sql_name}' failed: {exc}"
        ) from exc


def fetch(
    conn: pyodbc.Connection,
    sql_name: str,
    params: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
    """
    Execute a named SQL query and return all rows as a list of dicts.

    Column names are taken from cursor.description. Each row is returned
    as {column_name: value}. The cursor is closed before returning.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection.
    sql_name : str
        SQL filename stem without .sql extension.
    params : Optional[list[Any]]
        Positional parameters.

    Returns
    -------
    list[dict[str, Any]]
        All result rows as dicts. Empty list if no rows matched.

    Raises
    ------
    KeyError
        If sql_name is not found in the loaded SQL dict.
    DatabaseError
        If execution or result fetching fails.
    """
    cursor = execute(conn, sql_name, params)
    try:
        columns = [col[0] for col in cursor.description]
        return [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]
    except pyodbc.Error as exc:
        raise DatabaseError(
            f"Failed to fetch results for '{sql_name}': {exc}"
        ) from exc
    finally:
        cursor.close()


def bulk_insert(
    conn: pyodbc.Connection,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> int:
    """
    Insert a list of row dicts into any table using fast_executemany.

    All rows are inserted in a single server round-trip. The caller is
    responsible for commit() and rollback() — this function does not
    commit. On failure the exception is raised and the caller must
    rollback to leave the table in a consistent state.

    Column order is determined by the columns parameter — values are
    extracted from each row dict in that order. This decouples the
    dict key order from the INSERT column list.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection with autocommit disabled.
    schema : str
        Target schema name (e.g. 'Staging_SCH').
    table : str
        Target table name (e.g. 'FileLanding').
    rows : list[dict[str, Any]]
        Rows to insert. Each dict must contain all keys listed in columns.
    columns : list[str]
        Ordered list of column names to insert. Determines both the
        INSERT column list and the value extraction order from each row.

    Returns
    -------
    int
        Number of rows inserted.

    Raises
    ------
    DatabaseError
        If the bulk insert fails for any reason.
    """
    if not rows:
        _logger.debug("bulk_insert: no rows to insert into %s.%s", schema, table)
        return 0

    # Build INSERT statement from schema, table, and column list.
    # All values are parameterized — no string formatting of data.
    col_list     = ", ".join(columns)
    placeholders = ", ".join("?" * len(columns))
    sql          = (
        f"INSERT INTO {schema}.{table} ({col_list}) "
        f"VALUES ({placeholders})"
    )

    # Extract values from each row in column order.
    value_rows = [
        [row.get(col) for col in columns]
        for row in rows
    ]

    cursor = conn.cursor()
    try:
        # fast_executemany batches all rows into a single server round-trip.
        cursor.fast_executemany = True
        cursor.executemany(sql, value_rows)
        row_count = len(rows)
        _logger.debug(
            "bulk_insert: %d row(s) → %s.%s",
            row_count, schema, table,
        )
        return row_count
    except pyodbc.Error as exc:
        raise DatabaseError(
            f"bulk_insert failed for {schema}.{table}: {exc}"
        ) from exc
    finally:
        cursor.close()


def call_proc(
    conn: pyodbc.Connection,
    proc_name: str,
    params: Optional[list[Any]] = None,
) -> pyodbc.Cursor:
    """
    Execute a stored procedure by name with positional parameters.

    Uses ODBC escape syntax {CALL proc_name (?, ...)} for maximum
    driver compatibility. The returned cursor is not closed so the
    caller can inspect output parameters or result sets if needed.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection.
    proc_name : str
        Fully-qualified procedure name
        (e.g. 'NaviControl.dbo.pBCP_NewFiles').
    params : Optional[list[Any]]
        Positional input parameters. Pass None for zero-parameter procs.

    Returns
    -------
    pyodbc.Cursor
        Executed cursor. Caller must close it when done.

    Raises
    ------
    DatabaseError
        If procedure execution fails.
    """
    p            = params or []
    placeholders = ", ".join("?" * len(p))
    # ODBC escape syntax differs for zero vs. N parameters.
    call_sql = (
        f"{{CALL {proc_name} ({placeholders})}}"
        if p
        else f"{{CALL {proc_name}}}"
    )
    cursor = conn.cursor()
    try:
        cursor.execute(call_sql, p)
        _logger.debug(
            "call_proc: %s (%d param(s))", proc_name, len(p)
        )
        return cursor
    except pyodbc.Error as exc:
        cursor.close()
        raise DatabaseError(
            f"Stored procedure '{proc_name}' failed: {exc}"
        ) from exc


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
        SQL text ready for parameterized execution.

    Raises
    ------
    RuntimeError
        If init_db() has not been called yet.
    KeyError
        If no SQL file with that stem was found in sql_dir.
    """
    _require_init()
    if name not in _SQL:
        raise KeyError(
            f"SQL query '{name}' not found. "
            f"Available: {sorted(_SQL.keys())}"
        )
    return _SQL[name]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _sql_dir is None:
        raise RuntimeError(
            "sqlserver_utils.init_db() must be called before using the database."
        )


def _build_connection_string(db_cfg: Any) -> str:
    """
    Build a pyodbc connection string from a db_cfg Namespace.

    Uses Windows Authentication (Trusted_Connection=yes) when user is
    absent or empty. Uses SQL Server Authentication otherwise.

    Parameters
    ----------
    db_cfg : Any
        Single connection Namespace from ctx.db.connections.

    Returns
    -------
    str
        Fully-formed pyodbc connection string.
    """
    host   = str(db_cfg.host)
    port   = int(getattr(db_cfg, "port", 1433))
    db     = str(db_cfg.database)
    driver = str(getattr(db_cfg, "driver", "ODBC Driver 17 for SQL Server"))
    user   = str(getattr(db_cfg, "user", "")).strip()

    # Include port in server string only when non-default.
    server = f"{host},{port}" if port != 1433 else host

    base = (
        f"Driver={{{driver}}};"
        f"Server={server};"
        f"Database={db};"
    )

    if user:
        # SQL Server Authentication — password injected from .env.
        password = str(getattr(db_cfg, "password", ""))
        return base + f"UID={user};PWD={password};"

    # Default for internal apps — Windows Authentication.
    return base + "Trusted_Connection=yes;"


def _connect_with_retry(conn_str: str, timeout: int) -> pyodbc.Connection:
    """
    Open a pyodbc connection with exponential backoff on failure.

    Parameters
    ----------
    conn_str : str
        Fully-formed pyodbc connection string.
    timeout : int
        Login timeout in seconds passed to pyodbc.connect().

    Returns
    -------
    pyodbc.Connection
        Open connection with autocommit=False.

    Raises
    ------
    DatabaseError
        If all _MAX_CONNECT_ATTEMPTS attempts fail.
    """
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
        try:
            conn = pyodbc.connect(conn_str, timeout=timeout, autocommit=False)
            _logger.debug(
                "SQL Server connected (attempt %d of %d).",
                attempt,
                _MAX_CONNECT_ATTEMPTS,
            )
            return conn
        except pyodbc.Error as exc:
            last_exc = exc
            if attempt < _MAX_CONNECT_ATTEMPTS:
                delay = _CONNECT_BACKOFF_BASE * (2 ** (attempt - 1))
                _logger.warning(
                    "Connection attempt %d/%d failed — retrying in %.1fs: %s",
                    attempt, _MAX_CONNECT_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)

    raise DatabaseError(
        f"SQL Server connection failed after {_MAX_CONNECT_ATTEMPTS} attempts."
    ) from last_exc