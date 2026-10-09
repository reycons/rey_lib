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
from the environment by this module before get_connection() connects —
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
fetch_rows(conn, sql_text, params)
    Run one read query with named :name parameters and return its rows.
is_truncation_error(exc)
    Return True when exc is the SQL Server truncation error (8152) — the
    caller decides what remediation (proc / SQL) to run, driven by
    config.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

import pyodbc

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import DatabaseError, ConfigError
from rey_lib.logs import AppLogger
from rey_lib.errors.error_utils import StateError

__all__ = [
    "init_db",
    "get_connection",
    "get_current_database",
    "list_database_objects",
    "execute",
    "fetch",
    "bulk_insert",
    "insert",
    "update",
    "delete",
    "call_proc",
    "call_proc_with_output",
    "load_sql",
    "fetch_rows",
    "is_truncation_error",
]

_logger = AppLogger(__name__)

# ---------------------------------------------------------------------------
# Neutral → SQL Server type map
#
# Callers may pass backend-neutral type names (e.g. 'TEXT', 'TIMESTAMP').
# Anything not in this map is used as-is, so native SQL Server types
# (e.g. 'NVARCHAR(150)') already produced by _build_column_defs continue
# to work without change.
# ---------------------------------------------------------------------------

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


def get_connection(db_cfg: Any, *, ctx: Any = None) -> pyodbc.Connection:
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
            password  str   — optional; may name an environment variable
    ctx : Any
        Application context, needed only to read a credential that names an
        environment variable. Windows Authentication needs none.

    Returns
    -------
    pyodbc.Connection
        Open connection with autocommit disabled.

    Raises
    ------
    DatabaseError
        If all connection attempts are exhausted.
    """
    conn_str = _build_connection_string(db_cfg, ctx=ctx)
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
	"""
	if not rows:
		_logger.debug(
			"bulk_insert: no rows to insert into %s.%s",
			schema,
			table,
		)
		return 0

	col_list = ", ".join(columns)
	placeholders = ", ".join("?" * len(columns))

	sql = (
		f"INSERT INTO {schema}.{table} ({col_list}) "
		f"VALUES ({placeholders})"
	)

	value_rows = _prepare_bulk_insert_rows(rows, columns)

	cursor = conn.cursor()

	try:
		cursor.fast_executemany = True
		cursor.executemany(sql, value_rows)

		row_count = len(rows)

		_logger.debug(
			"bulk_insert: %d row(s) → %s.%s",
			row_count,
			schema,
			table,
		)

		return row_count

	except pyodbc.Error as exc:
		# Propagated, so not reported here: the AppError boundary reports it
		# once (rule 79). Row values are never logged.
		raise DatabaseError(
			f"bulk_insert failed for {schema}.{table}: {exc}"
		) from exc

	finally:
		cursor.close()
          

def _bound_value(value: Any) -> Any:
	"""A value as this provider binds it: a mapping or list travels as JSON text."""
	if isinstance(value, (dict, list)):
		import json

		return json.dumps(value, default=str)
	return value


def _single_primary_key(conn: pyodbc.Connection, schema: str, table: str) -> str:
	"""The table's one primary-key column, read from this provider's schema metadata.

	Raises:
		DatabaseError: When the table has no primary key, or more than one column in it.
	"""
	tables = schema_metadata(conn, schema).get("tables") or []
	entry = next((item for item in tables if item.get("name") == table), {})
	columns = list((entry.get("primary_key") or {}).get("constrained_columns") or [])
	if len(columns) != 1:
		raise DatabaseError(
			f"insert: {schema}.{table} has {len(columns)} primary-key column(s); "
			"insert needs exactly one."
		)
	return str(columns[0])


def _matching(criteria: dict[str, Any]) -> tuple[str, list[Any]]:
	"""The equality conditions for ``criteria``, joined with AND, and their bound values.

	A criterion of None matches NULL.
	"""
	conditions: list[str] = []
	values: list[Any] = []
	for name, value in criteria.items():
		if value is None:
			conditions.append(f"{quote_identifier(name)} IS NULL")
		else:
			conditions.append(f"{quote_identifier(name)} = ?")
			values.append(_bound_value(value))
	return " AND ".join(conditions), values


def insert(conn: pyodbc.Connection, schema: str, table: str, row: dict[str, Any]) -> Any:
	"""Insert one row into a table with one primary-key column; that row's key value.

	The primary key is read from this provider's schema metadata, and the row's
	value of it comes back from the insert itself (``OUTPUT INSERTED``). Every
	value is a bound parameter; a mapping or list is bound as JSON text. It does
	not commit.

	Raises:
		DatabaseError: If the table has no single-column primary key or the insert fails.
	"""
	key = _single_primary_key(conn, schema, table)
	columns = list(row)
	target = f"{quote_identifier(schema)}.{quote_identifier(table)}"
	listed = ", ".join(quote_identifier(name) for name in columns)
	placeholders = ", ".join("?" for _ in columns)
	sql = (
		f"INSERT INTO {target} ({listed}) "
		f"OUTPUT INSERTED.{quote_identifier(key)} VALUES ({placeholders})"
	)
	cursor = conn.cursor()
	try:
		cursor.execute(sql, [_bound_value(row[name]) for name in columns])
		returned = cursor.fetchone()
	except pyodbc.Error as exc:
		raise DatabaseError(f"insert failed for {schema}.{table}: {exc}") from exc
	finally:
		cursor.close()
	if not returned:
		raise DatabaseError(f"insert: {schema}.{table} returned no {key}.")
	return returned[0]


def update(
	conn: pyodbc.Connection,
	schema: str,
	table: str,
	values: dict[str, Any],
	criteria: dict[str, Any],
) -> int:
	"""Set ``values`` on the rows matching every ``criteria`` equality; the rows affected.

	Every value is a bound parameter. It does not commit.

	Raises:
		DatabaseError: If the update fails.
	"""
	assignments = ", ".join(f"{quote_identifier(name)} = ?" for name in values)
	condition, condition_values = _matching(criteria)
	target = f"{quote_identifier(schema)}.{quote_identifier(table)}"
	sql = f"UPDATE {target} SET {assignments} WHERE {condition}"
	cursor = conn.cursor()
	try:
		cursor.execute(sql, [*(_bound_value(value) for value in values.values()), *condition_values])
		return int(cursor.rowcount)
	except pyodbc.Error as exc:
		raise DatabaseError(f"update failed for {schema}.{table}: {exc}") from exc
	finally:
		cursor.close()


def delete(conn: pyodbc.Connection, schema: str, table: str, criteria: dict[str, Any]) -> int:
	"""Remove the rows matching every ``criteria`` equality; the rows affected.

	Every value is a bound parameter. It does not commit.

	Raises:
		DatabaseError: If the delete fails.
	"""
	condition, condition_values = _matching(criteria)
	target = f"{quote_identifier(schema)}.{quote_identifier(table)}"
	sql = f"DELETE FROM {target} WHERE {condition}"
	cursor = conn.cursor()
	try:
		cursor.execute(sql, condition_values)
		return int(cursor.rowcount)
	except pyodbc.Error as exc:
		raise DatabaseError(f"delete failed for {schema}.{table}: {exc}") from exc
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

def get_table_columns(conn: Any, schema: str, table: str) -> list[str]:
	"""
	Return SQL Server table columns in ordinal order.

	``schema`` may be either:
	- schema
	- database.schema
	"""

	database_name = None
	schema_name = schema

	if "." in schema:
		parts = schema.split(".", 1)

		database_name = parts[0]
		schema_name = parts[1]

	if database_name:
		sql = f"""
			SELECT
				column_name
			FROM {database_name}.information_schema.columns
			WHERE table_schema = ?
				AND table_name = ?
			ORDER BY ordinal_position
		"""
	else:
		sql = """
			SELECT
				column_name
			FROM information_schema.columns
			WHERE table_schema = ?
				AND table_name = ?
			ORDER BY ordinal_position
		"""

	cur = conn.cursor()

	cur.execute(sql, schema_name, table)

	return [row[0] for row in cur.fetchall()]


def quote_identifier(value: str) -> str:
	return "[" + value.replace("]", "]]") + "]"


def execute_statement(conn: pyodbc.Connection, sql_text: str) -> int:
	"""Run one statement on this connection and return what it touched.

	The provider half of ``DatabaseTable.delete_all_rows`` and ``drop``: the
	table writes the statement, this runs it the way pyodbc runs one. It does
	NOT commit -- the transaction is the caller's to manage.

	Args:
		conn: Open pyodbc connection.
		sql_text: One complete statement, already composed by the adapter.

	Returns:
		The cursor's affected-row count, as pyodbc reported it (-1 where the
		statement has none, such as DDL).

	Raises:
		DatabaseError: If the statement fails.
	"""
	try:
		cursor = conn.cursor()
		try:
			cursor.execute(sql_text)
			return int(cursor.rowcount)
		finally:
			cursor.close()
	except pyodbc.Error as exc:
		raise DatabaseError(f"execute_statement failed: {exc}") from exc


#: A named ``:name`` parameter -- never a ``::`` cast or part of a word.
_NAMED_PARAMETER = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


def fetch_rows(
	conn: pyodbc.Connection, sql_text: str, params: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Run one read query with named ``:name`` parameters and return its rows.

	A connector primitive: the statement is the caller's, and binding is this
	driver's -- pyodbc takes positional ``?`` markers, so each ``:name`` becomes
	one, bound in the order it appears. Values are always bound, never
	interpolated. It does NOT commit.

	Raises:
		DatabaseError: If a named parameter has no value, or the query fails.
	"""
	ordered: list[Any] = []

	def positional(match: re.Match) -> str:
		name = match.group(1)
		if name not in params:
			raise DatabaseError(f"fetch_rows: no value for parameter ':{name}'.")
		ordered.append(params[name])
		return "?"

	statement = _NAMED_PARAMETER.sub(positional, sql_text)
	try:
		cursor = conn.cursor()
		try:
			cursor.execute(statement, ordered)
			names = [column[0] for column in cursor.description or []]
			return [dict(zip(names, row)) for row in cursor.fetchall()]
		finally:
			cursor.close()
	except pyodbc.Error as exc:
		raise DatabaseError(f"fetch_rows failed: {exc}") from exc


def _database_prefix(database_name: str) -> str:
	"""``[database].`` before a catalog view, or nothing for the connected database.

	The same qualification ``table_exists`` writes for a named database. An
	identifier cannot be bound, so it is quoted here instead (backlog 727).
	"""
	return f"{quote_identifier(database_name)}." if database_name else ""


def table_column_catalog(
	conn: pyodbc.Connection, schema: str, table: str, *, database_name: str = "",
) -> list[dict[str, Any]]:
	"""One table's or view's columns as SQL Server's catalog states them, in column order.

	A ``database_name`` names the database whose catalog is read -- the one a
	table in another database on this server sits in (backlog 727); empty
	reads the connected database.

	A metadata primitive: what the server says, not DDL. Each type is the
	declaration SQL Server uses for it, parameters included. Empty when there
	is no such table or view (backlog 726).

	Returns:
		``{"name", "type", "default", "nullable"}`` per column.
	"""
	db = _database_prefix(database_name)
	rows = fetch_rows(
		conn,
		"SELECT c.name AS name, ty.name AS type_name, c.max_length AS max_length, "
		"c.precision AS precision, c.scale AS scale, c.is_nullable AS is_nullable, "
		"dc.definition AS default_value "
		f"FROM {db}sys.columns c "
		f"JOIN {db}sys.types ty ON ty.user_type_id = c.user_type_id "
		f"LEFT JOIN {db}sys.default_constraints dc ON dc.object_id = c.default_object_id "
		f"JOIN {db}sys.objects o ON o.object_id = c.object_id AND o.type IN ('U', 'V') "
		f"JOIN {db}sys.schemas s ON s.schema_id = o.schema_id "
		"WHERE s.name = :schema AND o.name = :table "
		"ORDER BY c.column_id",
		{"schema": schema, "table": table},
	)
	return [
		{
			"name": str(row["name"]),
			"type": _sqlserver_column_type(
				row["type_name"], row["max_length"], row["precision"], row["scale"],
			),
			"default": row["default_value"] or None,
			"nullable": bool(row["is_nullable"]),
		}
		for row in rows
	]


def schema_metadata(
	conn: pyodbc.Connection, schema: str, *, database_name: str = "",
) -> dict[str, Any]:
	"""The schema's structure facts, in the shape every provider answers (backlog 725).

	A ``database_name`` names the database whose catalog is read -- the one a
	table in another database on this server sits in (backlog 727); empty
	reads the connected database.

	A metadata primitive, from SQL Server's own catalog views: tables with their
	primary key, foreign keys, indexes (other than a key's or unique
	constraint's backing index), unique and check constraints; views by name.
	No column facts and no view definitions; nothing here constructs a database
	object.
	"""
	db = _database_prefix(database_name)
	where = {"schema": schema}
	tables: dict[str, dict[str, Any]] = {
		str(row["name"]): {
			"name": str(row["name"]), "primary_key": {}, "foreign_keys": [], "indexes": [],
			"unique_constraints": [], "check_constraints": [],
		}
		for row in fetch_rows(
			conn,
			f"SELECT t.name AS name FROM {db}sys.tables t JOIN {db}sys.schemas s "
			"ON s.schema_id = t.schema_id WHERE s.name = :schema ORDER BY t.name",
			where,
		)
	}
	keys: dict[tuple[str, str], dict[str, Any]] = {}
	for row in fetch_rows(
		conn,
		"SELECT t.name AS table_name, k.name AS key_name, k.type AS key_type, "
		f"c.name AS column_name FROM {db}sys.key_constraints k "
		f"JOIN {db}sys.tables t ON t.object_id = k.parent_object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		f"JOIN {db}sys.index_columns ic ON ic.object_id = k.parent_object_id "
		"AND ic.index_id = k.unique_index_id "
		f"JOIN {db}sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
		"WHERE s.name = :schema ORDER BY t.name, k.name, ic.key_ordinal",
		where,
	):
		table = tables.get(str(row["table_name"]))
		if table is None:
			continue
		key = keys.get((table["name"], str(row["key_name"])))
		if key is None:
			if str(row["key_type"]).strip() == "PK":
				key = table["primary_key"] = {"name": row["key_name"], "constrained_columns": []}
			else:
				key = {"name": row["key_name"], "column_names": []}
				table["unique_constraints"].append(key)
			keys[(table["name"], str(row["key_name"]))] = key
		key.get("constrained_columns", key.get("column_names")).append(str(row["column_name"]))
	foreign: dict[tuple[str, str], dict[str, Any]] = {}
	for row in fetch_rows(
		conn,
		"SELECT t.name AS table_name, fk.name AS fk_name, pc.name AS column_name, "
		"rs.name AS referred_schema, rt.name AS referred_table, rc.name AS referred_column "
		f"FROM {db}sys.foreign_keys fk "
		f"JOIN {db}sys.tables t ON t.object_id = fk.parent_object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		f"JOIN {db}sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
		f"JOIN {db}sys.columns pc ON pc.object_id = fkc.parent_object_id "
		"AND pc.column_id = fkc.parent_column_id "
		f"JOIN {db}sys.tables rt ON rt.object_id = fkc.referenced_object_id "
		f"JOIN {db}sys.schemas rs ON rs.schema_id = rt.schema_id "
		f"JOIN {db}sys.columns rc ON rc.object_id = fkc.referenced_object_id "
		"AND rc.column_id = fkc.referenced_column_id "
		"WHERE s.name = :schema ORDER BY t.name, fk.name, fkc.constraint_column_id",
		where,
	):
		table = tables.get(str(row["table_name"]))
		if table is None:
			continue
		fk = foreign.get((table["name"], str(row["fk_name"])))
		if fk is None:
			fk = {"name": row["fk_name"], "constrained_columns": [],
				"referred_schema": row["referred_schema"],
				"referred_table": row["referred_table"], "referred_columns": []}
			table["foreign_keys"].append(fk)
			foreign[(table["name"], str(row["fk_name"]))] = fk
		fk["constrained_columns"].append(str(row["column_name"]))
		fk["referred_columns"].append(str(row["referred_column"]))
	indexes: dict[tuple[str, str], dict[str, Any]] = {}
	for row in fetch_rows(
		conn,
		"SELECT t.name AS table_name, i.name AS index_name, i.is_unique AS is_unique, "
		f"c.name AS column_name FROM {db}sys.indexes i "
		f"JOIN {db}sys.tables t ON t.object_id = i.object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		f"JOIN {db}sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
		f"JOIN {db}sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
		"WHERE s.name = :schema AND i.name IS NOT NULL AND i.type > 0 "
		"AND i.is_primary_key = 0 AND i.is_unique_constraint = 0 AND ic.key_ordinal > 0 "
		"ORDER BY t.name, i.name, ic.key_ordinal",
		where,
	):
		table = tables.get(str(row["table_name"]))
		if table is None:
			continue
		index = indexes.get((table["name"], str(row["index_name"])))
		if index is None:
			index = {"name": row["index_name"], "column_names": [],
				"unique": bool(row["is_unique"])}
			table["indexes"].append(index)
			indexes[(table["name"], str(row["index_name"]))] = index
		index["column_names"].append(str(row["column_name"]))
	for row in fetch_rows(
		conn,
		f"SELECT t.name AS table_name, cc.name AS check_name FROM {db}sys.check_constraints cc "
		f"JOIN {db}sys.tables t ON t.object_id = cc.parent_object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		"WHERE s.name = :schema ORDER BY t.name, cc.name",
		where,
	):
		table = tables.get(str(row["table_name"]))
		if table is not None:
			table["check_constraints"].append({"name": row["check_name"]})
	views = [{"name": str(row["name"])} for row in fetch_rows(
		conn,
		f"SELECT v.name AS name FROM {db}sys.views v JOIN {db}sys.schemas s "
		"ON s.schema_id = v.schema_id WHERE s.name = :schema ORDER BY v.name",
		where,
	)]
	return {"tables": list(tables.values()), "views": views}


def table_exists(conn: pyodbc.Connection, catalog: str, schema: str, table: str) -> bool:
	"""Whether information_schema.tables holds a row for this schema and name.

	A metadata primitive (backlog 698): the provider's catalog answer only --
	what "exists" means is DatabaseTable's. A ``catalog`` names the database
	whose own information_schema is asked.
	"""
	prefix = f"{quote_identifier(catalog)}." if catalog else ""
	return bool(fetch_rows(
		conn,
		f"SELECT 1 AS present FROM {prefix}information_schema.tables "
		"WHERE table_schema = :schema AND table_name = :table",
		{"schema": schema, "table": table},
	))


def view_native_definition(conn: pyodbc.Connection, schema: str, view: str) -> str:
	"""The view's CREATE VIEW as SQL Server stores it (OBJECT_DEFINITION).

	A metadata primitive (backlog 698): retrieved by name, never composed here.
	Empty when there is none.
	"""
	rows = fetch_rows(
		conn,
		"SELECT OBJECT_DEFINITION(OBJECT_ID(:qualified)) AS definition",
		{"qualified": f"{quote_identifier(schema)}.{quote_identifier(view)}"},
	)
	definition = rows[0]["definition"] if rows else None
	return str(definition) if definition else ""


# Each routine kind's listing: the sys.objects types it answers for, fixed.
_ROUTINE_LISTINGS: dict[str, str] = {
	kind: (
		"SELECT s.name AS schema_name, o.name AS routine_name, o.type AS object_type "
		"FROM sys.objects o "
		"JOIN sys.schemas s ON s.schema_id = o.schema_id "
		f"WHERE o.type IN ({object_types}) AND o.is_ms_shipped = 0 "
		"AND (:schema IS NULL OR s.name = :schema) "
		"ORDER BY s.name, o.name"
	)
	for kind, object_types in (
		("procedure", "'P'"),
		("function", "'FN', 'IF', 'TF'"),
	)
}

#: Each listed routine's parameters (backlog 747), over the same T-SQL object
#: types as its listing; no CLR type is listed. parameter_id 0 is a function's
#: return value, not a parameter.
_ROUTINE_PARAMETERS: dict[str, str] = {
	"procedure": (
		"SELECT s.name AS schema_name, o.name AS routine_name, p.parameter_id AS ordinal, "
		"p.name AS parameter_name, TYPE_NAME(p.user_type_id) AS data_type, "
		"p.is_output AS is_output "
		"FROM sys.parameters p "
		"JOIN sys.objects o ON o.object_id = p.object_id "
		"JOIN sys.schemas s ON s.schema_id = o.schema_id "
		"WHERE o.type IN ('P') AND o.is_ms_shipped = 0 AND p.parameter_id > 0 "
		"AND (:schema IS NULL OR s.name = :schema) "
		"ORDER BY s.name, o.name, p.parameter_id"
	),
	"function": (
		"SELECT s.name AS schema_name, o.name AS routine_name, p.parameter_id AS ordinal, "
		"p.name AS parameter_name, TYPE_NAME(p.user_type_id) AS data_type, "
		"p.is_output AS is_output "
		"FROM sys.parameters p "
		"JOIN sys.objects o ON o.object_id = p.object_id "
		"JOIN sys.schemas s ON s.schema_id = o.schema_id "
		"WHERE o.type IN ('FN', 'IF', 'TF') AND o.is_ms_shipped = 0 AND p.parameter_id > 0 "
		"AND (:schema IS NULL OR s.name = :schema) "
		"ORDER BY s.name, o.name, p.parameter_id"
	),
}


def _returns_rows(object_type: Any) -> bool | None:
	"""Whether a routine of this sys.objects type answers rows; None when unstated."""
	stated = str(object_type or "").strip()
	return (stated in ("IF", "TF")) if stated else None


def list_routines(
	conn: pyodbc.Connection,
	catalog: str,
	schema: str | None,
	kind: str,
) -> list[dict[str, Any]]:
	"""Return one routine kind from the connected database's catalog (backlog 726).

	SQL Server does not overload a routine name, so a name is unique within its
	schema and the returned signature is always empty -- the same reason
	``get_routine_definition`` takes no part of the signature in finding one.
	The connection is to ``catalog`` already; it is not otherwise used.

	Raises:
		DatabaseError: When ``kind`` is not a routine kind.
	"""
	statement = _ROUTINE_LISTINGS.get(kind)
	if statement is None:
		raise DatabaseError(f"sqlserver_utils: unsupported routine kind '{kind}'.")

	rows = fetch_rows(conn, statement, {"schema": schema or None})
	# The parameters (backlog 747). A T-SQL OUTPUT parameter is also passed in,
	# so it is INOUT. SQL Server keeps has_default_value and default_value only
	# for CLR routines, and none is listed, so whether a default exists is
	# unknown (None) rather than false, and no expression is exposed.
	parameters: dict[tuple[str, str], list[dict[str, Any]]] = {}
	for row in fetch_rows(conn, _ROUTINE_PARAMETERS[kind], {"schema": schema or None}):
		routine = (str(row["schema_name"]), str(row["routine_name"]))
		parameters.setdefault(routine, []).append({
			"name": str(row["parameter_name"] or ""),
			"ordinal": int(row["ordinal"]),
			"type": str(row["data_type"] or ""),
			"mode": "INOUT" if row["is_output"] else "IN",
			"has_default": None,
			"default": None,
		})
	return [
		{
			"schema": str(row["schema_name"]),
			"name": str(row["routine_name"]),
			"signature": "",
			"parameters": parameters.get(
				(str(row["schema_name"]), str(row["routine_name"])), [],
			),
			# Inline (IF) and multi-statement (TF) table-valued functions answer
			# rows; a scalar function (FN) one value; an unstated type is
			# unknown, not "one value" (backlog 822).
			"returns_rows": _returns_rows(row.get("object_type")),
		}
		for row in rows
	]


def get_routine_definition(
	conn: pyodbc.Connection,
	catalog: str,
	schema: str,
	name: str,
	signature: str,
	kind: str,
) -> str:
	"""One routine's definition as SQL Server itself stores it.

	A metadata primitive: the server's own source, retrieved by name through
	``OBJECT_DEFINITION`` -- never composed here. SQL Server does not overload
	a routine name, so the signature takes no part in finding one.

	Raises:
		DatabaseError: When there is no such routine.
	"""
	qualified = f"{quote_identifier(schema)}.{quote_identifier(name)}"
	rows = fetch_rows(
		conn,
		"SELECT OBJECT_DEFINITION(OBJECT_ID(:qualified)) AS definition",
		{"qualified": qualified},
	)
	definition = rows[0]["definition"] if rows else None
	if not definition:
		raise DatabaseError(f"sqlserver_utils: {kind} not found: {schema}.{name}")
	return str(definition)


def user_type_catalog(conn: pyodbc.Connection, schema: str, name: str) -> dict[str, Any]:
	"""One user-defined type as SQL Server's catalog states it.

	A metadata primitive: what the server says, not DDL. An alias type's base
	is spelled the way SQL Server declares it, parameters included. A table or
	CLR type is reported by that kind. Empty when there is no such type.

	Returns:
		``{"kind", "base_type", "default", "not_null", "labels"}``.
	"""
	rows = fetch_rows(
		conn,
		"SELECT base.name AS base_name, t.max_length AS max_length, t.precision AS precision, "
		"t.scale AS scale, t.is_table_type AS is_table_type, t.is_assembly_type AS is_assembly_type "
		"FROM sys.types t "
		"JOIN sys.schemas s ON s.schema_id = t.schema_id "
		"LEFT JOIN sys.types base ON base.user_type_id = t.system_type_id "
		"WHERE s.name = :schema AND t.name = :name AND t.is_user_defined = 1",
		{"schema": schema, "name": name},
	)
	if not rows:
		return {}
	row = rows[0]
	if row["is_table_type"]:
		kind, base_type = "table", ""
	elif row["is_assembly_type"]:
		kind, base_type = "clr", ""
	else:
		kind = "alias"
		base_type = _sqlserver_column_type(
			row["base_name"], row["max_length"], row["precision"], row["scale"],
		)
	return {"kind": kind, "base_type": base_type, "default": None, "not_null": False, "labels": []}


def sequence_catalog(conn: pyodbc.Connection, schema: str, name: str) -> dict[str, Any]:
	"""One sequence as SQL Server's catalog states it.

	A metadata primitive: what the server says, not DDL. Empty when there is
	no such sequence.

	Returns:
		``{"start", "increment", "minimum", "maximum", "cycle"}``.
	"""
	rows = fetch_rows(
		conn,
		"SELECT seq.start_value AS start_value, seq.increment AS increment, "
		"seq.minimum_value AS minimum_value, seq.maximum_value AS maximum_value, "
		"seq.is_cycling AS is_cycling "
		"FROM sys.sequences seq JOIN sys.schemas s ON s.schema_id = seq.schema_id "
		"WHERE s.name = :schema AND seq.name = :name",
		{"schema": schema, "name": name},
	)
	if not rows:
		return {}
	row = rows[0]
	return {
		"start": row["start_value"], "increment": row["increment"],
		"minimum": row["minimum_value"], "maximum": row["maximum_value"],
		"cycle": bool(row["is_cycling"]),
	}


def trigger_native_definition(
	conn: pyodbc.Connection, schema: str, table: str, name: str,
) -> str:
	"""One trigger's CREATE TRIGGER statement as SQL Server itself stores it.

	A metadata primitive: ``OBJECT_DEFINITION``, retrieved by the trigger's
	table and name -- never by a handle a caller carries, and never composed
	here. Empty when the table has no such trigger.
	"""
	rows = fetch_rows(
		conn,
		"SELECT OBJECT_DEFINITION(tr.object_id) AS definition FROM sys.triggers tr "
		"JOIN sys.objects p ON p.object_id = tr.parent_id "
		"JOIN sys.schemas s ON s.schema_id = p.schema_id "
		"WHERE s.name = :schema AND p.name = :table AND tr.name = :name",
		{"schema": schema, "table": table, "name": name},
	)
	return str((rows[0] if rows else {}).get("definition") or "")


# SQL Server's constraint object types, in Rey's words.
_CONSTRAINT_KIND: dict[str, str] = {
	"PK": "primary_key", "F": "foreign_key", "UQ": "unique", "C": "check", "D": "default",
}


def constraint_catalog(
	conn: pyodbc.Connection, schema: str, table: str, name: str,
	*, database_name: str = "",
) -> dict[str, Any]:
	"""One constraint on one table as SQL Server's catalog states it.

	A ``database_name`` names the database whose catalog is read -- the one a
	table in another database on this server sits in (backlog 727); empty
	reads the connected database.

	A metadata primitive: what the server says, not DDL. Columns and the
	columns they reference in key order. Empty when the table has no such
	constraint.

	Returns:
		``{"kind", "columns", "referenced_schema", "referenced_table",
		"referenced_columns", "check"}``.
	"""
	db = _database_prefix(database_name)
	found = fetch_rows(
		conn,
		f"SELECT o.object_id AS object_id, o.type AS type FROM {db}sys.objects o "
		f"JOIN {db}sys.tables t ON t.object_id = o.parent_object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		"WHERE s.name = :schema AND t.name = :table AND o.name = :name",
		{"schema": schema, "table": table, "name": name},
	)
	if not found:
		return {}
	object_id = found[0]["object_id"]
	kind = _CONSTRAINT_KIND.get(str(found[0]["type"]).strip(), str(found[0]["type"]).strip())
	catalog: dict[str, Any] = {
		"kind": kind, "columns": [], "referenced_schema": "", "referenced_table": "",
		"referenced_columns": [], "check": None,
	}
	if kind in ("primary_key", "unique"):
		catalog["columns"] = [str(row["column_name"]) for row in fetch_rows(
			conn,
			f"SELECT c.name AS column_name FROM {db}sys.key_constraints k "
			f"JOIN {db}sys.index_columns ic ON ic.object_id = k.parent_object_id "
			"AND ic.index_id = k.unique_index_id "
			f"JOIN {db}sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
			"WHERE k.object_id = :object_id ORDER BY ic.key_ordinal",
			{"object_id": object_id},
		)]
	elif kind == "foreign_key":
		keys = fetch_rows(
			conn,
			"SELECT pc.name AS column_name, rs.name AS referenced_schema, "
			"rt.name AS referenced_table, rc.name AS referenced_column "
			f"FROM {db}sys.foreign_key_columns fkc "
			f"JOIN {db}sys.columns pc ON pc.object_id = fkc.parent_object_id "
			"AND pc.column_id = fkc.parent_column_id "
			f"JOIN {db}sys.tables rt ON rt.object_id = fkc.referenced_object_id "
			f"JOIN {db}sys.schemas rs ON rs.schema_id = rt.schema_id "
			f"JOIN {db}sys.columns rc ON rc.object_id = fkc.referenced_object_id "
			"AND rc.column_id = fkc.referenced_column_id "
			"WHERE fkc.constraint_object_id = :object_id ORDER BY fkc.constraint_column_id",
			{"object_id": object_id},
		)
		if keys:
			catalog["columns"] = [str(row["column_name"]) for row in keys]
			catalog["referenced_schema"] = str(keys[0]["referenced_schema"])
			catalog["referenced_table"] = str(keys[0]["referenced_table"])
			catalog["referenced_columns"] = [str(row["referenced_column"]) for row in keys]
	elif kind == "check":
		clauses = fetch_rows(
			conn,
			f"SELECT definition AS check_clause FROM {db}sys.check_constraints "
			"WHERE object_id = :object_id",
			{"object_id": object_id},
		)
		catalog["check"] = clauses[0]["check_clause"] if clauses else None
	return catalog


def index_column_catalog(
	conn: pyodbc.Connection, schema: str, table: str, index: str,
	*, database_name: str = "",
) -> dict[str, Any]:
	"""One index on one table as SQL Server's catalog states it.

	A ``database_name`` names the database whose catalog is read -- the one a
	table in another database on this server sits in (backlog 727); empty
	reads the connected database.

	A metadata primitive: what the server says, not DDL. Columns in key order.
	Empty when the table has no such index.

	Returns:
		``{"unique", "primary", "columns"}``.
	"""
	db = _database_prefix(database_name)
	rows = fetch_rows(
		conn,
		"SELECT i.is_unique AS is_unique, i.is_primary_key AS is_primary, c.name AS column_name "
		f"FROM {db}sys.indexes i "
		f"JOIN {db}sys.tables t ON t.object_id = i.object_id "
		f"JOIN {db}sys.schemas s ON s.schema_id = t.schema_id "
		f"JOIN {db}sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
		f"JOIN {db}sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
		"WHERE s.name = :schema AND t.name = :table AND i.name = :index "
		"ORDER BY ic.key_ordinal",
		{"schema": schema, "table": table, "index": index},
	)
	if not rows:
		return {}
	return {
		"unique": bool(rows[0]["is_unique"]),
		"primary": bool(rows[0]["is_primary"]),
		"columns": [str(row["column_name"]) for row in rows],
	}


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
        raise ConfigError(
            f"SQL query '{name}' not found. "
            f"Available: {sorted(_SQL.keys())}"
        )
    return _SQL[name]


def is_truncation_error(exc: Exception) -> bool:
    """
    Return True if ``exc`` is the SQL Server truncation error (8152).

    Used by DBAdapter to surface a backend-neutral signal that a failing
    bulk insert is recoverable by widening one or more columns. The
    actual remediation (which proc / SQL to run) is driven by the app
    via a configured sql_config — this function only classifies the
    exception.
    """
    return _is_truncation_error(exc, _TRUNCATION_ERROR)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _require_init() -> None:
    """Raise RuntimeError if init_db() has not been called."""
    if _sql_dir is None:
        raise StateError(
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


def _build_connection_string(db_cfg: Any, *, ctx: Any = None) -> str:
    """
    Build a pyodbc connection string from a db_cfg Namespace.

    Authentication is driven by the authentication.type key in the
    connection config — never inferred from the absence of a user field.

    Supported authentication types:
        trusted_connection  — Windows Authentication (Trusted_Connection=yes)
        sql_server          — SQL Server Authentication (UID/PWD from the
                              environment, read as the string is built)

    Parameters
    ----------
    db_cfg : Any
        Single connection Namespace from the configured connections.
    ctx : Any
        Application context, for reading credentials that name environment
        variables.

    Returns
    -------
    str
        Fully-formed pyodbc connection string. Holds the credential, so it
        goes straight to the driver and is never logged or stored.

    Raises
    ------
    ConfigError
        If authentication.type is missing or not a recognised value, or a
        credential names an environment variable that cannot be read now.
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
        # Credentials name environment variables in YAML — never hardcoded.
        # Read here, into the string that opens the connection and nowhere else.
        user     = str(resolve_env_reference(ctx, getattr(db_cfg, "user",     ""))).strip()
        password = str(resolve_env_reference(ctx, getattr(db_cfg, "password", ""))).strip()
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


def _normalize_db_nulls(value: Any) -> Any:
	if value == "":
		return None

	return value


def _prepare_bulk_insert_rows(
	rows: list[dict[str, Any]],
	columns: list[str],
) -> list[list[Any]]:
	return [
		[
			_normalize_db_nulls(row.get(col))
			for col in columns
		]
		for row in rows
	]

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


def get_current_database(conn: pyodbc.Connection) -> str:
    """Return the active SQL Server database name."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DB_NAME()")
        row = cursor.fetchone()
        return str(row[0]) if row and row[0] else ""
    finally:
        cursor.close()


def list_database_objects(
    conn: pyodbc.Connection,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Return exportable SQL Server objects with discoverable dependencies."""
    del database
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            WITH objs AS (
                SELECT 'schema' AS object_type, s.name AS schema_name, s.name AS object_name, NULL AS object_id
                FROM sys.schemas s
                WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')

                UNION ALL

                SELECT
                    CASE o.type
                        WHEN 'U' THEN 'table'
                        WHEN 'V' THEN 'view'
                        WHEN 'P' THEN 'procedure'
                        WHEN 'FN' THEN 'function'
                        WHEN 'TF' THEN 'function'
                        WHEN 'IF' THEN 'function'
                        WHEN 'TR' THEN 'trigger'
                        WHEN 'SO' THEN 'sequence'
                        WHEN 'PK' THEN 'constraint'
                        WHEN 'UQ' THEN 'constraint'
                        WHEN 'F' THEN 'constraint'
                        WHEN 'C' THEN 'constraint'
                        ELSE NULL
                    END AS object_type,
                    s.name AS schema_name,
                    o.name AS object_name,
                    o.object_id,
                    -- A constraint or trigger names its table; a constraint its
                    -- Rey kind (backlog 695).
                    CASE WHEN o.type IN ('PK', 'UQ', 'F', 'C', 'TR')
                        THEN OBJECT_NAME(o.parent_object_id) END AS table_name,
                    CASE o.type
                        WHEN 'PK' THEN 'primary_key'
                        WHEN 'UQ' THEN 'unique'
                        WHEN 'F' THEN 'foreign_key'
                        WHEN 'C' THEN 'check'
                    END AS constraint_kind
                FROM sys.objects o
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
                    -- 'D', a column default, is part of its table's DDL and not
                    -- exported as an object of its own (backlog 695).
                    AND o.type IN ('U', 'V', 'P', 'FN', 'TF', 'IF', 'TR', 'SO', 'PK', 'UQ', 'F', 'C')

                UNION ALL

                SELECT 'index', s.name, i.name, i.object_id, t.name, NULL
                FROM sys.indexes i
                JOIN sys.tables t ON t.object_id = i.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE i.name IS NOT NULL
                    AND i.type > 0
                    -- A primary key's or unique constraint's backing index is
                    -- that constraint, listed as such; not an index of its own
                    -- (backlog 695).
                    AND i.is_primary_key = 0
                    AND i.is_unique_constraint = 0
                    AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')

                UNION ALL

                SELECT 'type', s.name, t.name, NULL, NULL, NULL
                FROM sys.types t
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE t.is_user_defined = 1
                    -- Alias types only: a table or CLR type is not a kind
                    -- DatabaseUserDefinedType represents (backlog 695).
                    AND t.is_table_type = 0
                    AND t.is_assembly_type = 0
                    AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
            )
            SELECT object_type, schema_name, object_name, object_id, table_name, constraint_kind
            FROM objs
            WHERE object_type IS NOT NULL
            ORDER BY object_type, schema_name, object_name
            """
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    deps = _sqlserver_dependencies(conn)
    db_name = get_current_database(conn)
    objects: list[dict[str, Any]] = []
    for row in rows:
        object_type = str(row[0])
        schema_name = str(row[1])
        object_name = str(row[2])
        object_id = int(row[3]) if row[3] is not None else None
        key = f"{object_type}:{schema_name}.{object_name}"
        objects.append(
            {
                "database": db_name,
                "object_type": object_type,
                "schema": schema_name,
                "name": object_name,
                "object_id": object_id,
                **({"table": str(row[4])} if row[4] is not None else {}),
                **({"kind": str(row[5])} if row[5] is not None else {}),
                "dependencies": deps.get(key, []),
            }
        )
    return objects


def _sqlserver_column_type(type_name: str, max_len: int, precision: int, scale: int) -> str:
    """Return SQL Server type declaration with parameters when applicable."""
    t = str(type_name).lower()
    if t in {"nvarchar", "varchar", "nchar", "char", "varbinary", "binary"}:
        if max_len == -1:
            return f"{type_name}(MAX)"
        if t.startswith("n") and max_len and max_len > 0:
            return f"{type_name}({int(max_len // 2)})"
        return f"{type_name}({int(max_len)})"
    if t in {"decimal", "numeric"}:
        return f"{type_name}({int(precision)},{int(scale)})"
    if t in {"datetime2", "time", "datetimeoffset"}:
        return f"{type_name}({int(scale)})"
    return str(type_name)


def _sqlserver_dependencies(conn: pyodbc.Connection) -> dict[str, list[dict[str, str]]]:
    """Return dependency map keyed by exporter object key."""
    cursor = conn.cursor()
    result: dict[str, list[dict[str, str]]] = {}
    try:
        cursor.execute(
            """
            SELECT
                src_t.object_type,
                src_s.name,
                src_o.name,
                ref_t.object_type,
                ref_s.name,
                ref_o.name
            FROM sys.sql_expression_dependencies d
            JOIN sys.objects src_o ON src_o.object_id = d.referencing_id
            JOIN sys.schemas src_s ON src_s.schema_id = src_o.schema_id
            JOIN sys.objects ref_o ON ref_o.object_id = d.referenced_id
            JOIN sys.schemas ref_s ON ref_s.schema_id = ref_o.schema_id
            CROSS APPLY (
                SELECT CASE src_o.type
                    WHEN 'U' THEN 'table'
                    WHEN 'V' THEN 'view'
                    WHEN 'P' THEN 'procedure'
                    WHEN 'FN' THEN 'function'
                    WHEN 'TF' THEN 'function'
                    WHEN 'IF' THEN 'function'
                    WHEN 'TR' THEN 'trigger'
                    ELSE 'table' END AS object_type
            ) src_t
            CROSS APPLY (
                SELECT CASE ref_o.type
                    WHEN 'U' THEN 'table'
                    WHEN 'V' THEN 'view'
                    WHEN 'P' THEN 'procedure'
                    WHEN 'FN' THEN 'function'
                    WHEN 'TF' THEN 'function'
                    WHEN 'IF' THEN 'function'
                    WHEN 'TR' THEN 'trigger'
                    ELSE 'table' END AS object_type
            ) ref_t
            WHERE src_s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
              AND ref_s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
            """
        )
        for row in cursor.fetchall():
            src_key = f"{row[0]}:{row[1]}.{row[2]}"
            dep = {
                "object_type": str(row[3]),
                "schema": str(row[4]),
                "name": str(row[5]),
            }
            result.setdefault(src_key, []).append(dep)
    finally:
        cursor.close()

    for key, deps in result.items():
        uniq = {(d["object_type"], d["schema"], d["name"]): d for d in deps}
        result[key] = [uniq[k] for k in sorted(uniq)]
    return result

