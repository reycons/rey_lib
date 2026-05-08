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
call_proc_with_output(conn, proc_name, named_input_params, output_param_specs)
    Execute a stored procedure and capture named output parameter values.
load_sql(name)
    Return the preloaded SQL string for a named query.
create_staging_table_if_not_exists(conn, schema, table, column_defs)
    Create a staging table if it does not already exist.
expand_column_if_truncated(conn, schema, table, exc, rows, column_defs, batch_id)
    Detect truncation error, expand offending column via proc, return True to retry.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import pyodbc

from rey_lib.errors.error_utils import DatabaseError, ConfigError

__all__ = [
    "init_db",
    "get_connection",
    "execute",
    "fetch",
    "bulk_insert",
    "call_proc",
    "call_proc_with_output",
    "load_sql",
    "create_staging_table_if_not_exists",
    "expand_column_if_truncated",
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

# SQL Server error code for string truncation.
_TRUNCATION_ERROR: int = 8152


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
    sql = load_sql(sql_name)
    return _run_cursor(conn, sql, params, error_context=f"Query '{sql_name}'")


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
        return _cursor_to_dicts(cursor)
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

    # Build INSERT statement — all values parameterized, no data interpolation.
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
        _logger.debug("bulk_insert: %d row(s) → %s.%s", row_count, schema, table)
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
        (e.g. 'NaviControl.dbo.pUpd_StagingColumnLength').
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
    p        = params or []
    call_sql = _build_odbc_call(proc_name, len(p))
    return _run_cursor(conn, call_sql, p, error_context=f"Stored procedure '{proc_name}'")


def call_proc_with_output(
    conn: pyodbc.Connection,
    proc_name: str,
    named_input_params: list[tuple[str, Any]],
    output_param_specs: list[tuple[str, str]],
) -> dict[str, Any]:
    """
    Execute a stored procedure and capture named output parameter values.

    Uses a DECLARE / EXEC ... OUTPUT / SELECT pattern to read output
    parameter values back from the procedure. This approach works reliably
    with the SQL Server ODBC driver via pyodbc without requiring bound
    output-parameter types at the Python level.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection.
    proc_name : str
        Fully-qualified procedure name
        (e.g. 'NaviControl.dbo.usp_BatchLog_Begin').
    named_input_params : list[tuple[str, Any]]
        Input parameters as ``(param_name, value)`` pairs, in declaration
        order. Parameter names must match the procedure's ``@`` parameter
        names exactly (without the leading ``@``).
    output_param_specs : list[tuple[str, str]]
        Output parameters as ``(param_name, sql_type)`` pairs.
        ``sql_type`` is any valid SQL Server type literal used in
        ``DECLARE`` — e.g. ``'INT'``, ``'BIGINT'``, ``'NVARCHAR(100)'``.

    Returns
    -------
    dict[str, Any]
        Output parameter values keyed by parameter name.
        Values are as returned by pyodbc from the SELECT.

    Raises
    ------
    DatabaseError
        If procedure execution or result fetch fails.
    """
    # Build: DECLARE @ParamName sql_type = NULL;
    declare_lines = [
        f"DECLARE @{name} {sql_type} = NULL;"
        for name, sql_type in output_param_specs
    ]

    # Build: EXEC proc_name @in1 = ?, ..., @out1 = @out1 OUTPUT, ...
    input_bindings = [f"@{name} = ?" for name, _v in named_input_params]
    output_bindings = [f"@{name} = @{name} OUTPUT" for name, _t in output_param_specs]
    all_bindings = input_bindings + output_bindings
    exec_line = f"EXEC {proc_name} {', '.join(all_bindings)};"

    # Build: SELECT @out1 AS out1, ...
    select_cols = [
        f"@{name} AS {name}" for name, _t in output_param_specs
    ]
    select_line = "SELECT " + ", ".join(select_cols) + ";"

    sql = "\n".join(declare_lines + [exec_line, select_line])
    input_values = [value for _name, value in named_input_params]

    _logger.debug(
        "call_proc_with_output: %s  inputs=%s  output_params=%s",
        proc_name,
        [n for n, _ in named_input_params],
        [n for n, _ in output_param_specs],
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, input_values)
        # Skip any result sets produced by the proc itself before reaching
        # the final SELECT that returns the output parameter values.
        while cursor.description is None:
            if not cursor.nextset():
                _logger.warning(
                    "call_proc_with_output: no result set returned for '%s'",
                    proc_name,
                )
                return {}
        row = cursor.fetchone()
        if row is None:
            return {}
        return {name: row[i] for i, (name, _t) in enumerate(output_param_specs)}
    except pyodbc.Error as exc:
        raise DatabaseError(
            f"Stored procedure '{proc_name}' (with output) failed: {exc}"
        ) from exc
    finally:
        cursor.close()


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


def create_staging_table_if_not_exists(
    conn: pyodbc.Connection,
    schema: str,
    table: str,
    column_defs: list[tuple[str, str]],
) -> bool:
    """
    Create a staging table if it does not already exist in SQL Server.

    Uses OBJECT_ID check — safe to call on every startup. All columns
    are created nullable. Never modifies an existing table — if the
    table exists it is left untouched regardless of column differences.

    Identifiers are validated before interpolation into DDL to prevent
    SQL injection. Only alphanumeric characters, underscores, dots, and
    square brackets are permitted in schema, table, and column names.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open SQL Server connection.
    schema : str
        Target schema name (e.g. 'Staging_SCH').
        May be 'database.schema' for cross-database targets.
    table : str
        Target table name.
    column_defs : list[tuple[str, str]]
        Ordered list of (column_name, sql_type) tuples.
        e.g. [('trade_date', 'DATE'), ('account', 'NVARCHAR(35)')]
        All columns are created as NULL.

    Returns
    -------
    bool
        True if the table was created, False if it already existed.

    Raises
    ------
    DatabaseError
        If an identifier is invalid or the DDL statement fails.
    """
    # Validate all identifiers before interpolation.
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    for col_name, _ in column_defs:
        _validate_identifier(col_name, "column")

    qualified = f"{schema}.{table}"

    # Build column list — all nullable.
    col_sql = ",\n        ".join(
        f"[{col_name}] {sql_type} NULL"
        for col_name, sql_type in column_defs
    )

    ddl = (
        f"IF OBJECT_ID(N'{qualified}', N'U') IS NULL\n"
        f"BEGIN\n"
        f"    CREATE TABLE {qualified} (\n"
        f"        {col_sql}\n"
        f"    )\n"
        f"END"
    )

    cursor = conn.cursor()
    try:
        cursor.execute(ddl)
        conn.commit()

        # Check if the table now exists to determine return value.
        cursor.execute("SELECT OBJECT_ID(?, N'U')", [qualified])
        row    = cursor.fetchone()
        exists = row is not None and row[0] is not None

        _logger.info("Staging table ready: %s", qualified)
        return exists

    except pyodbc.Error as exc:
        conn.rollback()
        raise DatabaseError(
            f"Failed to create staging table '{qualified}': {exc}"
        ) from exc
    finally:
        cursor.close()


def expand_column_if_truncated(
    conn: pyodbc.Connection,
    schema: str,
    table: str,
    exc: Exception,
    rows: list[dict[str, Any]],
    column_defs: list[tuple[str, str]],
    batch_id: Optional[int] = None,
) -> bool:
    """
    Detect a column truncation error, expand offending columns via
    pUpd_StagingColumnLength, and return True so the caller can retry.

    SQL Server raises error 8152 when a value exceeds the column
    definition length. This function scans all columns in the current
    batch, calls the server-side proc for each column whose observed
    max length exceeds its current definition, and commits after each
    expansion. The proc handles type validation and never shrinks columns.

    Returns False when the exception is not a truncation error so the
    caller knows not to retry.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open SQL Server connection.
    schema : str
        Target schema — may be 'database.schema' for cross-db targets.
    table : str
        Target table name.
    exc : Exception
        The exception caught from the failed bulk insert.
    rows : list[dict[str, Any]]
        The rows that failed to insert — scanned for max lengths.
    column_defs : list[tuple[str, str]]
        Current column definitions — iterated for column names.
    batch_id : Optional[int]
        BatchID passed to pUpd_StagingColumnLength for logging via
        pRun_LoggedSQL. Pass None when running outside a batch context.

    Returns
    -------
    bool
        True if at least one column was expanded and the caller should retry.
        False if the error is not a truncation error.

    Raises
    ------
    DatabaseError
        If the proc call fails for any column.
    """
    if not _is_truncation_error(exc, _TRUNCATION_ERROR):
        return False

    _logger.warning(
        "Truncation error on %s.%s — scanning for oversized columns.",
        schema, table,
    )

    expanded_any         = False
    db_name, schema_name = _split_database_schema(schema)

    for col_name, _ in column_defs:
        # Scan all columns — pUpd_StagingColumnLength validates type
        # server-side and exits cleanly for non-varchar columns.
        max_observed = _max_col_length(rows, col_name)
        if max_observed == 0:
            continue

        # Expand to observed max plus 10-character buffer.
        new_len = max_observed + 10

        try:
            cursor = call_proc(
                conn,
                "dbo.pUpd_StagingColumnLength",
                [db_name, schema_name, table, col_name, new_len, batch_id],
            )
            cursor.close()
            conn.commit()
            _logger.info(
                "Expanded column '%s' on %s.%s to NVARCHAR(%d)",
                col_name, schema, table, new_len,
            )
            expanded_any = True
        except DatabaseError as alter_exc:
            conn.rollback()
            raise DatabaseError(
                f"Failed to expand column '{col_name}' on '{schema}.{table}': {alter_exc}"
            ) from alter_exc

    return expanded_any


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _sql_dir is None:
        raise RuntimeError(
            "sqlserver_utils.init_db() must be called before using the database."
        )


def _run_cursor(
    conn: pyodbc.Connection,
    sql: str,
    params: Optional[list[Any]],
    error_context: str,
) -> pyodbc.Cursor:
    """
    Open a cursor, execute SQL with params, and return the open cursor.

    Wraps pyodbc.Error into DatabaseError using error_context as the
    message prefix. Shared by execute() and call_proc() to eliminate
    the duplicated cursor open/execute/error pattern.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open pyodbc connection.
    sql : str
        SQL string to execute — already loaded or built by the caller.
    params : Optional[list[Any]]
        Positional parameters. None is treated as empty list.
    error_context : str
        Human-readable prefix for the DatabaseError message
        (e.g. "Query 'get_users'" or "Stored procedure 'dbo.pIns_Batch'").

    Returns
    -------
    pyodbc.Cursor
        Executed cursor. Caller must close it when done.

    Raises
    ------
    DatabaseError
        If execution fails.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params or [])
        _logger.debug("_run_cursor: %s", error_context)
        return cursor
    except pyodbc.Error as exc:
        cursor.close()
        raise DatabaseError(f"{error_context} failed: {exc}") from exc


def _cursor_to_dicts(cursor: pyodbc.Cursor) -> list[dict[str, Any]]:
    """
    Convert all rows from an open cursor to a list of column-keyed dicts.

    Column names are taken from cursor.description. The cursor is not
    closed by this function — the caller is responsible for closing it.

    Parameters
    ----------
    cursor : pyodbc.Cursor
        An executed cursor with results available.

    Returns
    -------
    list[dict[str, Any]]
        All result rows as dicts. Empty list if no rows matched.

    Raises
    ------
    DatabaseError
        If fetching results fails.
    """
    try:
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except pyodbc.Error as exc:
        raise DatabaseError(f"Failed to fetch cursor results: {exc}") from exc


def _build_odbc_call(proc_name: str, param_count: int) -> str:
    """
    Build an ODBC escape syntax call string for a stored procedure.

    Uses {CALL proc_name (?,?,...)} syntax for maximum driver
    compatibility. Produces {CALL proc_name} with no parens when
    param_count is zero.

    Parameters
    ----------
    proc_name : str
        Fully-qualified procedure name.
    param_count : int
        Number of positional parameters.

    Returns
    -------
    str
        ODBC escape call string ready for cursor.execute().
    """
    if param_count == 0:
        return f"{{CALL {proc_name}}}"
    placeholders = ", ".join("?" * param_count)
    return f"{{CALL {proc_name} ({placeholders})}}"


def _max_col_length(rows: list[dict[str, Any]], col_name: str) -> int:
    """
    Return the maximum string length of a column's values across all rows.

    None values and missing keys are treated as length 0. Used to
    determine the required column width before calling
    pUpd_StagingColumnLength.

    Parameters
    ----------
    rows : list[dict[str, Any]]
        Rows to scan.
    col_name : str
        Column name to inspect.

    Returns
    -------
    int
        Maximum observed length. 0 if all values are blank or missing.
    """
    return max(
        (len(str(row.get(col_name, "") or "")) for row in rows),
        default=0,
    )


def _build_connection_string(db_cfg: Any) -> str:
    """
    Build a pyodbc connection string from a db_cfg Namespace.

    Authentication is driven by the authentication.type key in the
    connection config — never inferred from the absence of a user field.

    Supported authentication types:
        trusted_connection  — Windows Authentication (Trusted_Connection=yes)
        sql_server          — SQL Server Authentication (UID/PWD from .env)

    Parameters
    ----------
    db_cfg : Any
        Single connection Namespace from ctx.db.connections.

    Returns
    -------
    str
        Fully-formed pyodbc connection string.

    Raises
    ------
    ConfigError
        If authentication.type is missing or not a recognised value.
    """
    host      = str(db_cfg.host)
    port      = int(getattr(db_cfg, "port", 1433))
    db        = str(db_cfg.database)
    driver    = str(getattr(db_cfg, "driver", "ODBC Driver 17 for SQL Server"))
    auth      = getattr(db_cfg, "authentication", None)
    auth_type = str(getattr(auth, "type", "")).strip().lower()

    # Include port in server string only when non-default.
    server = f"{host},{port}" if port != 1433 else host

    base = (
        f"Driver={{{driver}}};"
        f"Server={server};"
        f"Database={db};"
    )

    if auth_type == "trusted_connection":
        return base + "Trusted_Connection=yes;"

    if auth_type == "sql_server":
        # Credentials injected from .env via env: block in YAML — never hardcoded.
        user     = str(getattr(db_cfg, "user",     "")).strip()
        password = str(getattr(db_cfg, "password", "")).strip()
        return base + f"UID={user};PWD={password};"

    raise ConfigError(
        f"Connection '{getattr(db_cfg, 'name', '?')}' has missing or unrecognised "
        f"authentication.type '{auth_type}'. "
        f"Must be 'trusted_connection' or 'sql_server'."
    )

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


def _is_truncation_error(exc: Exception, error_code: int) -> bool:
    """
    Return True if exc is a pyodbc error with the given SQL Server error code.

    Checks error args first, falls back to string match for drivers
    that format error codes differently.

    Parameters
    ----------
    exc : Exception
        Exception to inspect.
    error_code : int
        SQL Server error code to match (e.g. 8152 for truncation).

    Returns
    -------
    bool
        True if the error code matches.
    """
    if not isinstance(exc, pyodbc.Error):
        return False
    args = getattr(exc, "args", ())
    if len(args) >= 2:
        try:
            return int(args[1]) == error_code
        except (ValueError, TypeError):
            pass
    return str(error_code) in str(exc)


def _split_database_schema(schema: str) -> tuple[str, str]:
    """
    Split a 'database.schema' string into its two parts.

    Returns ('', schema) when no dot is present — single-part schema
    with no database prefix.

    Parameters
    ----------
    schema : str
        Schema string — either 'schema_name' or 'database.schema_name'.

    Returns
    -------
    tuple[str, str]
        (database_name, schema_name)
    """
    parts = schema.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", schema


def _validate_identifier(name: str, label: str) -> None:
    """
    Validate a SQL identifier before interpolating into DDL.

    Only allows alphanumeric characters, underscores, dots (for
    schema.table references), and square brackets. Raises DatabaseError
    on any character that could be used for SQL injection.

    Parameters
    ----------
    name : str
        Identifier to validate.
    label : str
        Human-readable label used in error messages.

    Raises
    ------
    DatabaseError
        If the identifier contains disallowed characters.
    """
    if not re.fullmatch(r"[\w\.\[\]]+", name):
        raise DatabaseError(
            f"Invalid SQL identifier for {label}: '{name}'. "
            f"Only alphanumeric characters, underscores, dots, "
            f"and square brackets are permitted."
        )