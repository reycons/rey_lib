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

Governed SQL-file transformation
--------------------------------
open_memory_connection()            Caller-governed in-memory connection.
register_csv_source(conn, path)     Register a normalized CSV as relation source.
register_text_line_source(...)      Register a fixed-width file as source_lines.
load_sql_file(sql_path)             Return a governed .sql file's text unchanged.
fetch_sql_rows(conn, sql_text)      Execute SQL and return its columns and rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, Optional

import duckdb

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.files.file_utils import read_text_file
from rey_lib.logs import get_logger

__all__ = [
    "DB_PATH",
    "init_db",
    "get_connection",
    "get_current_database",
    "list_database_objects",
    "get_object_ddl",
    "execute",
    "fetch",
    "run_sql",
    "load_sql",
    "bulk_insert",
    "create_staging_table_if_not_exists",
    "MEMORY_DATABASE",
    "CSV_SOURCE_RELATION",
    "TEXT_LINE_SOURCE_RELATION",
    "TEXT_LINE_COLUMN",
    "SqlResult",
    "open_memory_connection",
    "register_csv_source",
    "register_text_line_source",
    "load_sql_file",
    "fetch_sql_rows",
    "default_file_select_sql",
    "file_source_expression",
]

_logger = get_logger(__name__)

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


def _config_value(db_cfg: Any, name: str) -> Any:
    """Read one field from a connection config, mapping or namespace alike."""
    if db_cfg is None:
        return None
    if isinstance(db_cfg, dict):
        return db_cfg.get(name)
    return getattr(db_cfg, name, None)


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


def get_connection(db_cfg: Any = None, *, ctx: Any = None) -> duckdb.DuckDBPyConnection:
    """
    Return an open DuckDB connection for ``db_cfg``.

    This is the connection the shared adapter dispatches to, so a caller that
    selects a DuckDB connection by name gets one the same way it would get any
    other backend's — there is no separate path for DuckDB.

    The configuration decides what is opened:

    * ``database: ':memory:'`` opens a private in-memory database, which is
      what a connection that queries files rather than storing them declares.
    * ``path`` opens that file, creating it and its parent on first use.

    With no configuration the module state set by :func:`init_db` is used, so
    applications that initialise DuckDB once at startup are unaffected.

    Parameters
    ----------
    db_cfg : Any
        A connection config exposing ``database`` and/or ``path``. Optional.
    ctx : Any
        Accepted so every backend answers the adapter's call the same way.
        DuckDB opens a file and takes no credential, so there is nothing here
        to read from the environment.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Open connection. Caller is responsible for closing it.

    Raises
    ------
    RuntimeError
        If no configuration is supplied and init_db() has not been called.
    """
    database = str(_config_value(db_cfg, "database") or "")
    if database == MEMORY_DATABASE:
        return duckdb.connect(MEMORY_DATABASE, config=dict(_OFFLINE_CONNECTION_CONFIG))

    configured = _config_value(db_cfg, "path") or (database or None)
    if configured:
        path = Path(str(configured)).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(path))

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


def run_sql(
    conn: duckdb.DuckDBPyConnection,
    sql_text: str,
    params: Optional[list[Any]] = None,
) -> Any:
    """
    Execute an ad hoc SQL statement and return the raw DuckDB result.

    Use for diagnostic, introspection, or user-supplied SQL where a named
    query file is not appropriate. Execution still goes through the managed
    connection obtained from get_connection().

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open DuckDB connection.
    sql_text : str
        SQL statement to execute.
    params : Optional[list[Any]]
        Positional query parameters.

    Returns
    -------
    Any
        Raw DuckDB result.
    """
    return conn.execute(sql_text, params or [])


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
# Governed SQL-file transformation
#
# A caller-owned connection queries one registered source file. Nothing here
# is module state: the connection lives for the length of one transformation
# and closes with it, so concurrent transformations cannot see each other.
# ---------------------------------------------------------------------------

# The logical relation names governed SQL is written against. Transformation
# SQL never names a physical path — it selects from these.
CSV_SOURCE_RELATION       = "source"
TEXT_LINE_SOURCE_RELATION = "source_lines"

# Fixed-width input is one opaque line per row. The SQL file defines columns
# from it with substr, trim, and try_cast.
TEXT_LINE_COLUMN = "line"

# A delimiter that cannot occur in text a caller would fixed-width parse, so
# every physical line arrives as exactly one field.
_TEXT_LINE_DELIMITER = "\x1f"

# Ordinary execution is local and offline. DuckDB otherwise fetches and loads
# known extensions on demand, which is a network call from inside a query.
# The database name a connection declares when it holds nothing of its own.
MEMORY_DATABASE = ":memory:"

_OFFLINE_CONNECTION_CONFIG: dict[str, Any] = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions":    False,
}


class SqlResult(NamedTuple):
    """One query's column names and rows, in the order the query declared."""

    columns: list[str]
    rows: list[tuple]


@contextmanager
def open_memory_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    """
    Yield an in-memory DuckDB connection that closes deterministically.

    The connection is caller-governed: it holds no module state, persists
    nothing, and is closed on both success and failure. Extension autoload
    and autoinstall are disabled, so ordinary execution performs no network
    access.

    Yields
    ------
    duckdb.DuckDBPyConnection
        Open connection to a private in-memory database.
    """
    conn = duckdb.connect(":memory:", config=dict(_OFFLINE_CONNECTION_CONFIG))
    try:
        yield conn
    finally:
        conn.close()


def register_csv_source(
    conn: duckdb.DuckDBPyConnection,
    source_path: Path | str,
    *,
    relation_name: str = CSV_SOURCE_RELATION,
    delimiter: str | None = None,
    encoding: str | None = None,
) -> None:
    """
    Register a normalized CSV file as a logical relation.

    The file's first row is its header — DuckDB is told so rather than left
    to discover it, because header discovery already happened upstream and
    belongs to exactly one owner. Every column is VARCHAR so transformation
    SQL casts deliberately, and short rows are padded with NULL rather than
    rejected.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open connection to register the relation on.
    source_path : Path | str
        Normalized CSV file to read. Never modified.
    relation_name : str
        Logical relation name governed SQL selects from.
    delimiter : str | None
        Field separator. DuckDB's default applies when omitted.
    encoding : str | None
        Source encoding. DuckDB's default applies when omitted.

    Raises
    ------
    DatabaseError
        If the file cannot be registered as a relation.
    """
    options: dict[str, Any] = {
        "header":       True,
        "all_varchar":  True,
        "null_padding": True,
    }
    if delimiter is not None:
        options["delimiter"] = delimiter
    if encoding is not None:
        options["encoding"] = encoding

    path = Path(source_path).expanduser()
    try:
        relation = conn.read_csv(str(path), **options)
        relation.create_view(relation_name, replace=True)
    except duckdb.Error as exc:
        raise DatabaseError(
            f"Could not register CSV source '{path}' as relation "
            f"'{relation_name}': {exc}"
        ) from exc

    _logger.debug(
        "Registered CSV relation '%s' from %s", relation_name, path
    )


def register_text_line_source(
    conn: duckdb.DuckDBPyConnection,
    source_path: Path | str,
    *,
    relation_name: str = TEXT_LINE_SOURCE_RELATION,
    encoding: str | None = None,
) -> None:
    """
    Register a fixed-width file as one raw text line per row.

    The relation has a single VARCHAR column holding the line exactly as it
    appears — quoting and embedded separators are not interpreted. Column
    positions are the referenced SQL file's business; no schema is inferred
    here and no second fixed-width parser exists.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open connection to register the relation on.
    source_path : Path | str
        Fixed-width source file. Never modified.
    relation_name : str
        Logical relation name governed SQL selects from.
    encoding : str | None
        Source encoding. DuckDB's default applies when omitted.

    Raises
    ------
    DatabaseError
        If the file cannot be registered as a relation.
    """
    options: dict[str, Any] = {
        "header":     False,
        "columns":    {TEXT_LINE_COLUMN: "VARCHAR"},
        "delimiter":  _TEXT_LINE_DELIMITER,
        "quotechar":  "",
        "escapechar": "",
    }
    if encoding is not None:
        options["encoding"] = encoding

    path = Path(source_path).expanduser()
    try:
        relation = conn.read_csv(str(path), **options)
        relation.create_view(relation_name, replace=True)
    except duckdb.Error as exc:
        raise DatabaseError(
            f"Could not register text-line source '{path}' as relation "
            f"'{relation_name}': {exc}"
        ) from exc

    _logger.debug(
        "Registered text-line relation '%s' from %s", relation_name, path
    )


# How DuckDB reads each file format. One entry per extension, so a caller with
# a path never has to know which reader its format needs.
#
# The CSV form matches register_csv_source: the first row names the columns,
# every column is VARCHAR so a caller casts deliberately, and short rows are
# padded rather than rejected.
_FILE_SOURCE_EXPRESSIONS: dict[str, str] = {
    ".csv":     "read_csv({literal}, header = true, all_varchar = true, null_padding = true)",
    ".jsonl":   "read_json_auto({literal}, format = 'newline_delimited')",
    ".ndjson":  "read_json_auto({literal}, format = 'newline_delimited')",
    ".json":    "read_json_auto({literal})",
    ".parquet": "read_parquet({literal})",
    # Spreadsheets are deliberately absent. read_xlsx lives in DuckDB's excel
    # extension, which is not in the default build and which these connections
    # do not load — they run offline. A workbook reaches the Workbench as the
    # CSV it is converted to upstream.
}


def _sql_string_literal(value: str) -> str:
    """Quote one value as a SQL string, doubling any quote it contains."""
    return "'" + str(value).replace("'", "''") + "'"


def file_source_expression(file_path: Path | str) -> str:
    """Return the DuckDB source expression that reads ``file_path``.

    The reader is chosen by extension, matched case-insensitively.

    Raises
    ------
    ConfigError
        If no path is given, or its format has no DuckDB reader.
    """
    text = str(file_path or "").strip()
    if not text:
        raise ConfigError("A file path is required to build a DuckDB query.")

    path = Path(text).expanduser()
    suffix = path.suffix.lower()
    expression = _FILE_SOURCE_EXPRESSIONS.get(suffix)
    if expression is None:
        supported = ", ".join(sorted(_FILE_SOURCE_EXPRESSIONS))
        raise ConfigError(
            f"DuckDB cannot read '{path.name}': {suffix or 'no extension'} is "
            f"not a supported file format. Supported: {supported}."
        )
    return expression.format(literal=_sql_string_literal(str(path)))


def default_file_select_sql(file_path: Path | str) -> str:
    """Return the default read-only query for one file.

    This is the statement a surface opens a file on. It is the one place that
    knows a `.parquet` needs read_parquet and a `.jsonl` needs newline-delimited
    JSON settings, so no caller builds file SQL of its own.

    Parameters
    ----------
    file_path : Path | str
        The file to read. Its extension selects the reader.

    Returns
    -------
    str
        ``SELECT * FROM <source expression>``.

    Raises
    ------
    ConfigError
        If no path is given, or its format has no DuckDB reader.
    """
    return f"SELECT * FROM {file_source_expression(file_path)}"


def load_sql_file(sql_path: Path | str) -> str:
    """
    Return the SQL text of a governed .sql file, unchanged.

    Transformation logic lives in the file, not in this module. The text is
    returned exactly as written — nothing is substituted into it and nothing
    is rewritten. A file that cannot supply SQL fails here rather than
    producing an empty or partial result downstream.

    Parameters
    ----------
    sql_path : Path | str
        Path to the governed .sql file.

    Returns
    -------
    str
        The file's SQL text.

    Raises
    ------
    ConfigError
        If the path does not end in .sql, does not exist, is not a file, or
        contains no SQL.
    """
    path = Path(sql_path).expanduser()
    if path.suffix.lower() != ".sql":
        raise ConfigError(
            f"SQL file '{path}' must use the .sql extension."
        )
    if not path.is_file():
        raise ConfigError(f"SQL file not found: {path}")

    sql_text = read_text_file(path)
    if not sql_text.strip():
        raise ConfigError(f"SQL file is empty: {path}")
    return sql_text


def fetch_sql_rows(
    conn: duckdb.DuckDBPyConnection,
    sql_text: str,
) -> SqlResult:
    """
    Execute SQL text and return its columns and rows.

    Column names and their order come from the query itself, so a caller
    writing the result out reproduces the SELECT list — including aliases —
    without restating it.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Open connection.
    sql_text : str
        SQL to execute, unchanged.

    Returns
    -------
    SqlResult
        Column names and result rows.

    Raises
    ------
    DatabaseError
        If DuckDB rejects the SQL or fails while executing it.
    """
    try:
        result  = conn.execute(sql_text)
        columns = [column[0] for column in result.description or []]
        rows    = result.fetchall()
    except duckdb.Error as exc:
        raise DatabaseError(f"SQL execution failed: {exc}") from exc
    return SqlResult(columns=columns, rows=rows)


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _db_path is None:
        raise RuntimeError(
            "duckdb_utils.init_db() must be called before using the database."
        )


def get_current_database(conn: duckdb.DuckDBPyConnection) -> str:
    """Return current DuckDB catalog/database name."""
    row = conn.execute("SELECT current_database()").fetchone()
    return str(row[0]) if row and row[0] else "main"


def list_database_objects(
    conn: duckdb.DuckDBPyConnection,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Return exportable DuckDB objects with discoverable dependencies."""
    db_name = database or get_current_database(conn)

    rows = conn.execute(
        """
        WITH objects AS (
            SELECT 'schema' AS object_type, schema_name AS schema_name, schema_name AS object_name
            FROM information_schema.schemata
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog')

            UNION ALL

            SELECT CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END,
                   table_schema,
                   table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')

            UNION ALL

            SELECT 'sequence', sequence_schema, sequence_name
            FROM information_schema.sequences
            WHERE sequence_schema NOT IN ('information_schema', 'pg_catalog')
        )
        SELECT object_type, schema_name, object_name
        FROM objects
        ORDER BY object_type, schema_name, object_name
        """
    ).fetchall()

    deps = _duckdb_dependencies(conn)

    result: list[dict[str, Any]] = []
    for object_type, schema_name, object_name in rows:
        key = f"{object_type}:{schema_name}.{object_name}"
        result.append(
            {
                "database": db_name,
                "object_type": str(object_type),
                "schema": str(schema_name),
                "name": str(object_name),
                "dependencies": deps.get(key, []),
            }
        )
    return result


def get_object_ddl(conn: duckdb.DuckDBPyConnection, obj: dict[str, Any]) -> str:
    """Return native DuckDB DDL for one object."""
    object_type = str(obj.get("object_type", "")).lower().rstrip("s")
    schema = str(obj.get("schema", "main"))
    name = str(obj.get("name", ""))

    if object_type == "schema":
        return f'CREATE SCHEMA IF NOT EXISTS "{schema}";'

    if object_type == "table":
        row = conn.execute(f'SHOW CREATE TABLE "{schema}"."{name}"').fetchone()
        ddl = row[1] if row and len(row) > 1 else (row[0] if row else "")
        return str(ddl).rstrip() + ";"

    if object_type == "view":
        row = conn.execute(f'SHOW CREATE VIEW "{schema}"."{name}"').fetchone()
        ddl = row[1] if row and len(row) > 1 else (row[0] if row else "")
        return str(ddl).rstrip() + ";"

    if object_type == "sequence":
        row = conn.execute(
            """
            SELECT start_value, increment, min_value, max_value, cycle
            FROM information_schema.sequences
            WHERE sequence_schema = ? AND sequence_name = ?
            """,
            [schema, name],
        ).fetchone()
        if not row:
            raise ValueError(f"duckdb_utils: sequence not found: {schema}.{name}")
        cycle_sql = "CYCLE" if bool(row[4]) else "NO CYCLE"
        return (
            f'CREATE SEQUENCE IF NOT EXISTS "{schema}"."{name}" '
            f'START {row[0]} INCREMENT {row[1]} MINVALUE {row[2]} MAXVALUE {row[3]} {cycle_sql};'
        )

    return f"-- Unsupported DuckDB object type '{object_type}' for {schema}.{name}."


def _duckdb_dependencies(conn: duckdb.DuckDBPyConnection) -> dict[str, list[dict[str, str]]]:
    """Return discoverable DuckDB dependencies keyed by exporter key."""
    result: dict[str, list[dict[str, str]]] = {}

    # DuckDB exposes view dependencies in duckdb_dependencies() on recent versions.
    try:
        rows = conn.execute(
            """
            SELECT
                object_schema,
                object_name,
                referenced_schema,
                referenced_object_name,
                referenced_object_type
            FROM duckdb_dependencies()
            WHERE object_schema NOT IN ('information_schema', 'pg_catalog')
            """
        ).fetchall()
    except Exception:  # noqa: BLE001 - older DuckDB versions may not expose this table function.
        return result

    for row in rows:
        object_schema, object_name, ref_schema, ref_name, ref_type = row
        src_type = "view"
        ref_type_norm = str(ref_type or "table").lower().rstrip("s")
        key = f"{src_type}:{object_schema}.{object_name}"
        dep = {
            "object_type": ref_type_norm,
            "schema": str(ref_schema),
            "name": str(ref_name),
        }
        result.setdefault(key, []).append(dep)

    for key, deps in result.items():
        uniq = {(d["object_type"], d["schema"], d["name"]): d for d in deps}
        result[key] = [uniq[k] for k in sorted(uniq)]
    return result


# ---------------------------------------------------------------------------
# Paging
#
# One page of a reader's own query, with the size of the whole result beside
# it. Everything dialect-specific lives here; DBAdapter only dispatches.
#
# This is the same capability postgres_utils offers and deliberately not the
# same implementation. Two provider facts decide that:
#
#   * a DuckDB handle is a raw duckdb.DuckDBPyConnection, not a SQLAlchemy one,
#     so the borrowed-connection helper the PostgreSQL path uses does not apply;
#   * a file-querying connection is ':memory:', and a second connect(':memory:')
#     opens a DIFFERENT, EMPTY database. Opening one here would page a database
#     that has never seen the file.
#
# DuckDB's own way to get a second handle on the SAME database is cursor(),
# which shares the catalog and any registered relation. That is what this uses.
# ---------------------------------------------------------------------------

#: The page a caller gets when it names no size, and the largest it may name.
#: The rule is the estate's -- rey_lib.logs.file_hierarchy established it and
#: postgres_utils follows it -- because a page size taken on trust from a caller
#: is an unbounded read wearing a parameter.
_DEFAULT_PAGE_ROWS = 100
_MAX_PAGE_ROWS = 500

#: What the wrapped query is called, so a filter or an order naming a column is
#: unambiguous.
_PAGE_ALIAS = "rey_page"

#: Filter operators, by the name a caller uses. The operator and the identifier
#: are written; every value is bound.
_PAGE_FILTER_OPERATORS: dict[str, str] = {
    "equals": "{column} = ?",
    "notEquals": "{column} <> ?",
    "contains": "CAST({column} AS VARCHAR) ILIKE '%' || ? || '%'",
    "notContains": "CAST({column} AS VARCHAR) NOT ILIKE '%' || ? || '%'",
    "startsWith": "CAST({column} AS VARCHAR) ILIKE ? || '%'",
    "endsWith": "CAST({column} AS VARCHAR) ILIKE '%' || ?",
    "gt": "{column} > ?",
    "gte": "{column} >= ?",
    "lt": "{column} < ?",
    "lte": "{column} <= ?",
}


def _page_identifier(name: str) -> str:
    """Return one identifier, quoted so any column name is safe to write.

    A CSV's header decides these names, so they may contain anything a
    spreadsheet allowed -- a space, a keyword, a quotation mark. Doubling the
    quote makes this total, where a pattern would refuse legitimate headers.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _validated_page_bounds(offset: int, limit: int) -> tuple[int, int]:
    """Return the page bounds, refused rather than clamped when out of range."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DatabaseError("offset must be a nonnegative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DatabaseError("limit must be a positive integer.")
    if limit > _MAX_PAGE_ROWS:
        raise DatabaseError(f"limit must not exceed {_MAX_PAGE_ROWS}.")
    return offset, limit


def _page_wrapped(sql_text: str) -> str:
    """Return the reader's query as a subquery this module can build on."""
    inner = str(sql_text or "").strip().rstrip(";").strip()
    if not inner:
        raise DatabaseError("SQL text is required.")
    return f"({inner}) AS {_PAGE_ALIAS}"


def _page_filters(
    filters: Optional[list[dict[str, Any]]],
) -> tuple[str, list[Any]]:
    """Return the WHERE clause and the values it binds."""
    if not filters:
        return "", []
    clauses: list[str] = []
    values: list[Any] = []
    for one in filters:
        column = str(one.get("column") or "").strip()
        operator = str(one.get("operator") or "equals").strip()
        if not column:
            raise DatabaseError("A filter must name a column.")
        template = _PAGE_FILTER_OPERATORS.get(operator)
        if template is None:
            raise DatabaseError(
                f"Unknown filter operator '{operator}'. "
                f"Known operators: {sorted(_PAGE_FILTER_OPERATORS)}."
            )
        clauses.append(template.format(column=_page_identifier(column)))
        values.append(one.get("value"))
    return "\nWHERE  " + "\n   AND ".join(clauses), values


def _page_order(order_by: Optional[list[dict[str, Any]]]) -> str:
    """Return the ORDER BY clause, or nothing when no order was named.

    No order is left alone rather than invented: imposing one would discard an
    ORDER BY the reader's own query already carried.
    """
    if not order_by:
        return ""
    terms: list[str] = []
    for one in order_by:
        column = str(one.get("column") or "").strip()
        if not column:
            raise DatabaseError("An ordering must name a column.")
        descending = str(one.get("direction") or "asc").lower() == "desc"
        terms.append(f"{_page_identifier(column)} {'DESC' if descending else 'ASC'}")
    return "\nORDER BY " + ", ".join(terms)


def _refuse_unpageable(handle: duckdb.DuckDBPyConnection, wrapped: str) -> None:
    """Decline here, before the reader's statement has been executed.

    DuckDB decides, not a keyword scan: EXPLAIN plans the wrapped query and
    executes none of it, so a statement that cannot be read as a subquery -- a
    batch, a definition, a call -- fails to parse and is declined without
    having run.

    The ordering is the contract, not a preference. A declination is answered
    by executing the same statement again on the eager path, and the statement
    is the reader's own, so one raised after execution had begun would apply it
    twice.
    """
    from rey_lib.errors.error_utils import UnsupportedDatabaseCapabilityError

    try:
        handle.execute(f"EXPLAIN SELECT 1 FROM {wrapped}")
    except duckdb.Error as exc:
        raise UnsupportedDatabaseCapabilityError(
            "This statement cannot be paged: it is not a query that can be "
            f"read as a subquery. {exc}"
        ) from exc


def execute_page(
    conn: duckdb.DuckDBPyConnection,
    sql_text: str,
    *,
    offset: int,
    limit: int,
    order_by: Optional[list[dict[str, Any]]] = None,
    filters: Optional[list[dict[str, Any]]] = None,
) -> Any:
    """Return one page of a query. **No total: this provider does not count.**

    Counting rows in a file means parsing the file. This used to run a
    ``count(*)`` beside every page, so opening a large JSON meant parsing it
    twice per page and the Console stopped responding. A total is worth having
    where it is cheap; here it is the most expensive thing on the path.

    So ``total_row_count`` is None -- unknown, never zero -- and continuation is
    answered by reading one row past the page. If that row is there, another
    page follows.

    **This bounds what is returned, not what is read.** ``LIMIT`` caps result
    rows; how much of the file DuckDB must inspect to produce them is its own
    affair, and a filter, an ordering or schema inference can still make it read
    far more, up to all of it. What is removed here is the guaranteed whole-file
    parse, not the cost of the read itself.

    Both statements run on a handle from ``conn.cursor()``: DuckDB's own way to
    get a second handle on the same database, catalog and registered relations
    included. Opening another connection would be wrong rather than merely
    wasteful, because a file-querying connection is ``:memory:`` and a second
    one of those is a different, empty database.

    Args:
        conn: Open DuckDB connection.
        sql_text: The query to page, exactly as the reader wrote it.
        offset: Where the page starts.
        limit: The most rows the page may hold.
        order_by: Column/direction mappings, or None to leave the query's own
            ordering alone.
        filters: Column/operator/value mappings, or None.

    Returns:
        One ``PageResult`` whose ``total_row_count`` is None and whose
        ``next_offset`` is set when a further page exists.

    Raises:
        UnsupportedDatabaseCapabilityError: If this statement cannot be paged.
            Raised before the statement is executed, never after.
        DatabaseError: If the bounds are invalid, or execution failed.
    """
    from rey_lib.db.db_adapter import PageResult

    offset, limit = _validated_page_bounds(offset, limit)
    wrapped = _page_wrapped(sql_text)
    where, bound = _page_filters(filters)
    order = _page_order(order_by)

    handle = conn.cursor()
    try:
        # Declined here or not at all. Nothing below this line may raise
        # UnsupportedDatabaseCapabilityError.
        _refuse_unpageable(handle, wrapped)
        try:
            # One row beyond the page. Its existence is the whole continuation
            # signal: it says another page follows without anyone counting the
            # rows that make it up.
            #
            # offset and limit are written in rather than bound -- they are
            # integers this module validated, so there is nothing to bind
            # against, and a page with no filter then carries no parameters.
            paged = handle.execute(
                f"SELECT * FROM {wrapped}{where}{order}"
                f"\nLIMIT {limit + 1:d} OFFSET {offset:d}",
                bound or None,
            )
            columns = [column[0] for column in paged.description or []]
            fetched = paged.fetchall()
        except duckdb.Error as exc:
            raise DatabaseError(f"The page could not be read: {exc}") from exc
    finally:
        handle.close()

    has_more = len(fetched) > limit
    rows = [dict(zip(columns, row)) for row in fetched[:limit]]
    return PageResult(
        columns=columns,
        rows=rows,
        # Not counted. Over a file that is a full parse, and it would be paid on
        # every page -- which is what made opening a large one hang. None is
        # unknown, and the surfaces above say so rather than showing a number
        # they would have had to invent.
        total_row_count=None,
        offset=offset,
        limit=limit,
        next_offset=offset + limit if has_more else None,
    )
