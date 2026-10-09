"""
Generic MySQL connection and query execution layer.

Owns all MySQL connections, query execution, transaction handling,
and bulk loading. No raw mysql.connector calls are permitted outside
this module.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.logs import AppLogger
from rey_lib.errors.error_utils import ConfigError, StateError, UnsupportedDatabaseCapabilityError

__all__ = [
	"init_db",
	"get_connection",
	"get_current_database",
	"list_database_objects",
	"bulk_insert",
	"insert",
	"update",
	"delete",
	"call_proc",
	"call_proc_with_output",
	"load_sql",
	"fetch_rows",
	"get_table_columns",
	"quote_identifier",
	"is_truncation_error",
]

_logger = AppLogger(__name__)

_sql_dir: Path | None = None
_SQL: dict[str, str] = {}

_MAX_CONNECT_ATTEMPTS: int = 3
_CONNECT_BACKOFF_BASE: float = 1.0

_TRUNCATION_ERROR: int = 1406


def init_db(sql_dir: Path) -> None:
	global _sql_dir, _SQL

	sql_dir = Path(sql_dir).resolve()

	if not sql_dir.exists():
		raise FileNotFoundError(f"SQL directory not found: {sql_dir}")

	_sql_dir = sql_dir
	_SQL = {
		p.stem: p.read_text(encoding="utf-8")
		for p in sorted(sql_dir.glob("*.sql"))
	}

	_logger.debug(
		"mysql_utils initialised — sql_dir: %s (%d file(s) loaded)",
		sql_dir,
		len(_SQL),
	)


def get_connection(db_cfg: Any, *, ctx: Any = None) -> Any:
	timeout = int(getattr(db_cfg, "timeout", 30))
	return _connect_with_retry(db_cfg, timeout, ctx=ctx)


def query_rows(
	conn: Any,
	sql_text: str,
	*,
	limit: int | None = 1_000,
) -> tuple[list[str], list[dict[str, Any]]]:
	"""Execute one read query and normalize its result.

	``limit=None`` returns every row. A load reading its source has no
	bound to name, and naming a large one instead silently truncates the
	day the result outgrows it.
	"""
	from rey_lib.db._sqlalchemy import core_connection

	try:
		from sqlalchemy import text

		result = core_connection(conn).execute(text(sql_text))
		columns = [str(column) for column in result.keys()]
		if not columns:
			return columns, []
		values = (
			result.fetchall() if limit is None
			else result.fetchmany(max(1, int(limit)))
		)
		return columns, [dict(zip(columns, row)) for row in values]
	except Exception as exc:
		raise DatabaseError(f"DBAdapter: query failed: {exc}") from exc


def bulk_insert(
	conn: Any,
	schema: str,
	table: str,
	rows: list[dict[str, Any]],
	columns: list[str],
) -> int:
	if not rows:
		_logger.debug("bulk_insert: no rows to insert into %s.%s", schema, table)
		return 0

	_validate_identifier(schema, "schema")
	_validate_identifier(table, "table")

	for col in columns:
		_validate_identifier(col, "column")

	value_rows = _prepare_bulk_insert_rows(rows, columns)

	from rey_lib.db._sqlalchemy import core_connection

	try:
		from sqlalchemy import column, insert, table as sql_table

		statement = insert(
			sql_table(table, *(column(name) for name in columns), schema=schema)
		)
		parameter_rows = [
			{column_name: value for column_name, value in zip(columns, value_row)}
			for value_row in value_rows
		]
		core_connection(conn).execute(statement, parameter_rows)
		return len(rows)
	except Exception as exc:
		raise DatabaseError(f"bulk_insert failed for {schema}.{table}: {exc}") from exc


def _bound_value(value: Any) -> Any:
	"""A value as this provider binds it: a mapping or list travels as JSON text."""
	if isinstance(value, (dict, list)):
		import json

		return json.dumps(value, default=str)
	return value


def _validated_target(schema: str, table: str, columns: list[str]) -> None:
	"""Refuse a schema, table or column name this provider will not address."""
	_validate_identifier(schema, "schema")
	_validate_identifier(table, "table")
	for name in columns:
		_validate_identifier(name, "column")


def insert(conn: Any, schema: str, table: str, row: dict[str, Any]) -> Any:
	"""Insert one row into a table with one primary-key column; that row's key value.

	The primary key is read from the catalog through the SQLAlchemy inspector.
	MySQL returns no row from an insert, so the key value is the row's own when
	it supplies one, and otherwise the generated value the driver reports
	(``lastrowid``). Values are bound by SQLAlchemy. It does not commit.

	Raises:
		DatabaseError: If a name is not a plain identifier, the table has no
			single-column primary key, or the insert fails.
	"""
	from rey_lib.db._sqlalchemy import core_connection, inspect_primary_key
	from sqlalchemy import column, insert as sql_insert, table as sql_table
	from sqlalchemy.exc import SQLAlchemyError

	_validated_target(schema, table, list(row))
	try:
		key_columns = inspect_primary_key(conn, schema, table)
	except SQLAlchemyError as exc:
		raise DatabaseError(
			f"insert: could not read the primary key of {schema}.{table}: {exc}"
		) from exc
	if len(key_columns) != 1:
		raise DatabaseError(
			f"insert: {schema}.{table} has {len(key_columns)} primary-key column(s); "
			"insert needs exactly one."
		)
	key = key_columns[0]
	target = sql_table(table, *(column(name) for name in row), schema=schema)
	statement = sql_insert(target).values(
		{name: _bound_value(value) for name, value in row.items()}
	)
	try:
		result = core_connection(conn).execute(statement)
	except SQLAlchemyError as exc:
		raise DatabaseError(f"insert failed for {schema}.{table}: {exc}") from exc
	if row.get(key) is not None:
		return row[key]
	return result.lastrowid


def update(
	conn: Any, schema: str, table: str, values: dict[str, Any], criteria: dict[str, Any],
) -> int:
	"""Set ``values`` on the rows matching every ``criteria`` equality; the rows affected.

	Values and criteria are bound by SQLAlchemy; a criterion of None matches
	NULL. It does not commit.

	Raises:
		DatabaseError: If a name is not a plain identifier or the update fails.
	"""
	from rey_lib.db._sqlalchemy import core_connection
	from sqlalchemy import column, table as sql_table, update as sql_update
	from sqlalchemy.exc import SQLAlchemyError

	_validated_target(schema, table, [*values, *criteria])
	target = sql_table(
		table, *(column(name) for name in dict.fromkeys([*values, *criteria])), schema=schema,
	)
	statement = (
		sql_update(target)
		.where(*(target.c[name] == value for name, value in criteria.items()))
		.values({name: _bound_value(value) for name, value in values.items()})
	)
	try:
		return int(core_connection(conn).execute(statement).rowcount)
	except SQLAlchemyError as exc:
		raise DatabaseError(f"update failed for {schema}.{table}: {exc}") from exc


def delete(conn: Any, schema: str, table: str, criteria: dict[str, Any]) -> int:
	"""Remove the rows matching every ``criteria`` equality; the rows affected.

	Criteria are bound by SQLAlchemy; a criterion of None matches NULL. It
	does not commit.

	Raises:
		DatabaseError: If a name is not a plain identifier or the delete fails.
	"""
	from rey_lib.db._sqlalchemy import core_connection
	from sqlalchemy import column, delete as sql_delete, table as sql_table
	from sqlalchemy.exc import SQLAlchemyError

	_validated_target(schema, table, list(criteria))
	target = sql_table(table, *(column(name) for name in criteria), schema=schema)
	statement = sql_delete(target).where(
		*(target.c[name] == value for name, value in criteria.items())
	)
	try:
		return int(core_connection(conn).execute(statement).rowcount)
	except SQLAlchemyError as exc:
		raise DatabaseError(f"delete failed for {schema}.{table}: {exc}") from exc


def call_proc(
	conn: Any,
	proc_name: str,
	params: Optional[list[Any]] = None,
) -> Any:
	from rey_lib.db._sqlalchemy import is_sqlalchemy_connection, raw_dbapi_connection

	p = params or []
	driver_conn = raw_dbapi_connection(conn) if is_sqlalchemy_connection(conn) else conn
	cursor = driver_conn.cursor(dictionary=True)

	try:
		cursor.callproc(proc_name, p)
		_logger.debug("call_proc: %s", proc_name)
		return cursor

	except MySQLError as exc:
		cursor.close()
		raise DatabaseError(f"Stored procedure '{proc_name}' failed: {exc}") from exc


def call_proc_with_output(
	conn: mysql.connector.MySQLConnection,
	proc_name: str,
	named_input_params: list[tuple[str, Any]],
	output_param_specs: list[tuple[str, str]],
) -> dict[str, Any]:
	raise UnsupportedDatabaseCapabilityError(
		"MySQL call_proc_with_output is not implemented yet. "
		"Use call_proc() or add a MySQL OUT-param implementation when needed."
	)


def get_table_columns(conn: Any, schema: str, table: str) -> list[str]:
	from rey_lib.db._sqlalchemy import inspect_schema

	metadata = inspect_schema(conn, schema)
	table_metadata = next(
		(item for item in metadata["tables"] if item["name"] == table), None
	)
	if table_metadata is None:
		return []
	return [str(column["name"]) for column in table_metadata["columns"]]


def quote_identifier(value: str) -> str:
	return "`" + value.replace("`", "``") + "`"


def execute_statement(conn: Any, sql_text: str) -> int:
	"""Run one statement on this connection and return what it touched.

	The provider half of ``DatabaseTable.delete_all_rows`` and ``drop``: the
	table writes the statement, this runs it the way this driver runs one.
	It does NOT commit -- whether the statement sits in a transaction is the
	connection's, and is the caller's to manage.

	Args:
		conn: Open connection handle.
		sql_text: One complete statement, already composed by the adapter.

	Returns:
		The driver's affected-row count, as it reported it (a statement with
		no row count, such as DDL, reports what the driver reports for it).

	Raises:
		DatabaseError: If the statement fails.
	"""
	from rey_lib.db._sqlalchemy import core_connection

	try:
		return int(core_connection(conn).exec_driver_sql(sql_text).rowcount)
	except Exception as exc:
		raise DatabaseError(f"execute_statement failed: {exc}") from exc


def fetch_rows(conn: Any, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
	"""Run one read query with named ``:name`` parameters and return its rows.

	A connector primitive: the statement is the caller's, and binding is this
	driver's -- SQLAlchemy binds ``:name`` itself. It does NOT commit.

	Raises:
		DatabaseError: If the query fails.
	"""
	from rey_lib.db._sqlalchemy import core_connection
	from sqlalchemy import text

	try:
		result = core_connection(conn).execute(text(sql_text), dict(params))
		return [dict(row) for row in result.mappings()]
	except Exception as exc:
		raise DatabaseError(f"fetch_rows failed: {exc}") from exc


def table_native_definition(conn: Any, schema: str, table: str) -> str:
	"""The CREATE TABLE statement MySQL itself reports for one table, as it reports it.

	A metadata primitive: the server's own definition, ENGINE and character set
	included -- retrieved, never composed here. Empty when there is none.
	"""
	rows = fetch_rows(
		conn, f"SHOW CREATE TABLE {quote_identifier(schema)}.{quote_identifier(table)}", {},
	)
	return str((rows[0] if rows else {}).get("Create Table") or "")


def trigger_native_definition(conn: Any, schema: str, table: str, name: str) -> str:
	"""One trigger's CREATE TRIGGER statement as MySQL itself reports it.

	A metadata primitive: ``SHOW CREATE TRIGGER``, retrieved -- never composed
	here -- once the catalog confirms the trigger fires on ``table``. Empty
	when the table has no such trigger.
	"""
	found = fetch_rows(
		conn,
		"SELECT 1 AS present FROM information_schema.triggers "
		"WHERE trigger_schema = :schema AND event_object_table = :table AND trigger_name = :name",
		{"schema": schema, "table": table, "name": name},
	)
	if not found:
		return ""
	rows = fetch_rows(
		conn, f"SHOW CREATE TRIGGER {quote_identifier(schema)}.{quote_identifier(name)}", {},
	)
	row = rows[0] if rows else {}
	return str(row.get("SQL Original Statement") or row.get("Create Trigger") or "")


# MySQL's constraint types, in Rey's words.
_CONSTRAINT_KIND: dict[str, str] = {
	"PRIMARY KEY": "primary_key", "FOREIGN KEY": "foreign_key", "UNIQUE": "unique", "CHECK": "check",
}


def constraint_catalog(
	conn: Any, schema: str, table: str, name: str, *, database_name: str = "",
) -> dict[str, Any]:
	"""One constraint on one table as MySQL's catalog states it.

	``database_name`` is accepted and not used: a MySQL database is the schema
	(backlog 727; SQL Server's reads use it).

	A metadata primitive: what the server says, not DDL. Columns and the
	columns they reference in key order. Empty when the table has no such
	constraint.

	Returns:
		``{"kind", "columns", "referenced_schema", "referenced_table",
		"referenced_columns", "check"}``.
	"""
	params = {"schema": schema, "table": table, "name": name}
	found = fetch_rows(
		conn,
		"SELECT constraint_type AS constraint_type FROM information_schema.table_constraints "
		"WHERE constraint_schema = :schema AND table_name = :table AND constraint_name = :name",
		params,
	)
	if not found:
		return {}
	kind = str(found[0]["constraint_type"]).upper()
	keys = fetch_rows(
		conn,
		"SELECT column_name AS column_name, referenced_table_schema AS referenced_schema, "
		"referenced_table_name AS referenced_table, referenced_column_name AS referenced_column "
		"FROM information_schema.key_column_usage "
		"WHERE constraint_schema = :schema AND table_name = :table AND constraint_name = :name "
		"ORDER BY ordinal_position",
		params,
	)
	check = None
	if kind == "CHECK":
		clauses = fetch_rows(
			conn,
			"SELECT check_clause AS check_clause FROM information_schema.check_constraints "
			"WHERE constraint_schema = :schema AND constraint_name = :name",
			{"schema": schema, "name": name},
		)
		check = clauses[0]["check_clause"] if clauses else None
	first = keys[0] if keys else {}
	return {
		"kind": _CONSTRAINT_KIND.get(kind, kind.lower()),
		"columns": [str(row["column_name"]) for row in keys if row["column_name"] is not None],
		"referenced_schema": str(first.get("referenced_schema") or ""),
		"referenced_table": str(first.get("referenced_table") or ""),
		"referenced_columns": [
			str(row["referenced_column"]) for row in keys if row["referenced_column"] is not None
		],
		"check": check,
	}


def index_column_catalog(
	conn: Any, schema: str, table: str, index: str, *, database_name: str = "",
) -> dict[str, Any]:
	"""One index on one table as MySQL's catalog states it.

	``database_name`` is accepted and not used: a MySQL database is the schema
	(backlog 727; SQL Server's reads use it).

	A metadata primitive: what the server says, not DDL. Columns in key order.
	MySQL names a primary key's index PRIMARY. Empty when the table has no
	such index.

	Returns:
		``{"unique", "primary", "columns"}``.
	"""
	rows = fetch_rows(
		conn,
		"SELECT non_unique AS non_unique, column_name AS column_name "
		"FROM information_schema.statistics "
		"WHERE table_schema = :schema AND table_name = :table AND index_name = :index "
		"ORDER BY seq_in_index",
		{"schema": schema, "table": table, "index": index},
	)
	if not rows:
		return {}
	return {
		"unique": int(rows[0]["non_unique"]) == 0,
		"primary": index == "PRIMARY",
		"columns": [str(row["column_name"]) for row in rows if row["column_name"] is not None],
	}


def schema_metadata(
	conn: Any, schema: str, *, database_name: str = "",
) -> dict[str, Any]:
	"""The schema's structure facts, in the shape every provider answers (backlog 725).

	``database_name`` is accepted and not used: a MySQL database is the schema
	(backlog 727; SQL Server's reads use it).

	A metadata primitive: tables with their keys, indexes and constraints, and
	views, by name -- read through the SQLAlchemy inspector. No column facts and
	no view definitions; DatabaseSchema.read builds the objects.
	"""
	from rey_lib.db._sqlalchemy import inspect_schema_structure  # noqa: PLC0415

	return inspect_schema_structure(conn, schema)


def table_column_catalog(
	conn: Any, schema: str, table: str, *, database_name: str = "",
) -> list[dict[str, Any]]:
	"""One table's or view's columns as MySQL's catalog states them, in column order.

	``database_name`` is accepted and not used: a MySQL database is the schema
	(backlog 727; SQL Server's reads use it).

	A metadata primitive (backlog 724): what the server says, not DDL. Each type
	is the declared COLUMN_TYPE (``varchar(12)``, ``int unsigned``). MySQL
	reports a literal default unquoted, so it is returned as SQL text -- a
	literal quoted, an expression default (DEFAULT_GENERATED, CURRENT_TIMESTAMP)
	as it is -- so a column definition written from it is valid. Empty when
	there is no such table.

	Returns:
		``{"name", "type", "default", "nullable"}`` per column.
	"""
	rows = fetch_rows(
		conn,
		"SELECT column_name AS name, column_type AS type, column_default AS default_value, "
		"is_nullable AS is_nullable, extra AS extra "
		"FROM information_schema.columns "
		"WHERE table_schema = :schema AND table_name = :table "
		"ORDER BY ordinal_position",
		{"schema": schema, "table": table},
	)
	return [
		{
			"name": str(row["name"]),
			"type": str(row["type"]),
			"default": _sql_default(row["default_value"], str(row["extra"] or "")),
			"nullable": str(row["is_nullable"]).upper() == "YES",
		}
		for row in rows
	]


def _sql_default(value: Any, extra: str) -> str | None:
	"""A MySQL column default as SQL text: literals quoted, expressions as they are."""
	if value is None:
		return None
	text = str(value)
	if "DEFAULT_GENERATED" in extra.upper() or text.upper().startswith("CURRENT_TIMESTAMP"):
		return text
	return "'" + text.replace("'", "''") + "'"


def table_exists(conn: Any, catalog: str, schema: str, table: str) -> bool:
	"""Whether information_schema.tables holds a row for this schema and name.

	A metadata primitive (backlog 698): the provider's catalog answer only --
	what "exists" means is DatabaseTable's. ``catalog`` takes no part here.
	"""
	return bool(fetch_rows(
		conn,
		"SELECT 1 AS present FROM information_schema.tables "
		"WHERE table_schema = :schema AND table_name = :table",
		{"schema": schema, "table": table},
	))


def view_native_definition(conn: Any, schema: str, view: str) -> str:
	"""The CREATE VIEW statement MySQL itself reports (SHOW CREATE VIEW).

	A metadata primitive (backlog 698): retrieved, never composed here. Empty
	when there is none.
	"""
	rows = fetch_rows(
		conn, f"SHOW CREATE VIEW {quote_identifier(schema)}.{quote_identifier(view)}", {},
	)
	return str((rows[0] if rows else {}).get("Create View") or "")


def load_sql(name: str) -> str:
	_require_init()

	if name not in _SQL:
		raise ConfigError(
			f"SQL query '{name}' not found. "
			f"Available: {sorted(_SQL.keys())}"
		)

	return _SQL[name]


def is_truncation_error(exc: Exception) -> bool:
	return _is_mysql_error(exc, _TRUNCATION_ERROR)


def _require_init() -> None:
	if _sql_dir is None:
		raise StateError("mysql_utils.init_db() must be called before using the database.")


def _connect_with_retry(db_cfg: Any, timeout: int, *, ctx: Any = None) -> Any:
	last_exc: Exception | None = None

	for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
		try:
			from rey_lib.db._sqlalchemy import open_connection

			conn = open_connection(
				"mysql",
				"mysql+mysqlconnector",
				host=str(db_cfg.host),
				port=int(getattr(db_cfg, "port", 3306)),
				database=str(db_cfg.database),
				username=str(getattr(db_cfg, "user", "")),
				# Resolved as the connection is opened; a retry reads the
				# environment again rather than reusing what it read.
				password=str(resolve_env_reference(ctx, getattr(db_cfg, "password", ""))),
				connect_args={
					"connection_timeout": timeout,
					"autocommit": False,
					"allow_local_infile": bool(
						getattr(db_cfg, "allow_local_infile", False)
					),
				},
			)

			_logger.debug(
				"MySQL connected (attempt %d of %d).",
				attempt,
				_MAX_CONNECT_ATTEMPTS,
			)

			return conn

		except Exception as exc:
			last_exc = exc

			if attempt < _MAX_CONNECT_ATTEMPTS:
				delay = _CONNECT_BACKOFF_BASE * (2 ** (attempt - 1))

				_logger.warning(
					"Connection attempt %d/%d failed — retrying in %.1fs: %s",
					attempt,
					_MAX_CONNECT_ATTEMPTS,
					delay,
					exc,
				)

				time.sleep(delay)

	raise DatabaseError(
		f"MySQL connection failed after {_MAX_CONNECT_ATTEMPTS} attempts."
	) from last_exc


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


def _is_mysql_error(exc: Exception, error_code: int) -> bool:
	original = getattr(exc, "orig", exc)
	if not isinstance(original, MySQLError):
		return False

	errno = getattr(original, "errno", None)

	if errno == error_code:
		return True

	return str(error_code) in str(original)


def _validate_identifier(name: str, label: str) -> None:
	if not re.fullmatch(r"[\w]+", name):
		raise DatabaseError(
			f"Invalid MySQL identifier for {label}: '{name}'. "
			f"Only alphanumeric characters and underscores are permitted."
		)


def get_current_database(conn: Any) -> str:
	"""Return the current MySQL database."""
	from rey_lib.db._sqlalchemy import core_connection
	from sqlalchemy import text

	value = core_connection(conn).execute(text("SELECT DATABASE()")).scalar()
	return str(value) if value else ""


def list_database_objects(
	conn: Any,
	database: str | None = None,
) -> list[dict[str, Any]]:
	"""Return exportable MySQL objects and discoverable dependencies."""
	db_name = database or get_current_database(conn)
	from rey_lib.db._sqlalchemy import inspect_schema

	metadata = inspect_schema(conn, db_name)
	objects: list[dict[str, Any]] = [
		{
			"database": db_name,
			"object_type": "schema",
			"schema": db_name,
			"name": db_name,
			"dependencies": [],
		}
	]
	for table_metadata in metadata["tables"]:
		table_name = str(table_metadata["name"])
		dependency = {
			"object_type": "table",
			"schema": db_name,
			"name": table_name,
		}
		objects.append(
			{
				"database": db_name,
				"object_type": "table",
				"schema": db_name,
				"name": table_name,
				"dependencies": [],
			}
		)
		for index in table_metadata["indexes"]:
			name = str(index.get("name") or "")
			if name:
				objects.append(
					{
						"database": db_name,
						"object_type": "index",
						"schema": db_name,
						"name": name,
						"table": table_name,
						"dependencies": [dict(dependency)],
					}
				)
		constraints = [("primary_key", table_metadata["primary_key"])]
		constraints.extend(("foreign_key", entry) for entry in table_metadata["foreign_keys"])
		constraints.extend(("unique", entry) for entry in table_metadata["unique_constraints"])
		for kind, constraint in constraints:
			name = str(constraint.get("name") or "")
			if name:
				objects.append(
					{
						"database": db_name,
						"object_type": "constraint",
						"schema": db_name,
						"name": name,
						"table": table_name,
						"kind": kind,
						"dependencies": [dict(dependency)],
					}
				)
	for view in metadata["views"]:
		objects.append(
			{
				"database": db_name,
				"object_type": "view",
				"schema": db_name,
				"name": str(view["name"]),
				"dependencies": [],
			}
		)

	# Inspector has no routines/triggers API; retain the narrow catalog fallback.
	cursor = conn.cursor(dictionary=True)
	try:
		cursor.execute(
			"""
			SELECT CASE routine_type WHEN 'PROCEDURE' THEN 'procedure' ELSE 'function' END AS object_type,
				routine_schema AS schema_name, routine_name AS object_name, NULL AS table_name
			FROM information_schema.routines WHERE routine_schema = %s
			UNION ALL
			SELECT 'trigger', trigger_schema, trigger_name, event_object_table
			FROM information_schema.triggers WHERE trigger_schema = %s
			""",
			[db_name, db_name],
		)
		for row in cursor.fetchall():
			objects.append(
				{
					"database": db_name,
					"object_type": str(row["object_type"]),
					"schema": str(row["schema_name"]),
					"name": str(row["object_name"]),
					# A trigger's table: with schema and name, its identity (695).
					**({"table": str(row["table_name"])} if row["table_name"] is not None else {}),
					"dependencies": [],
				}
			)
	finally:
		cursor.close()

	fallback_dependencies = _mysql_dependencies(conn, db_name)
	for obj in objects:
		key = f"{obj['object_type']}:{obj['schema']}.{obj['name']}"
		if key in fallback_dependencies:
			obj["dependencies"] = fallback_dependencies[key]
	unique = {
		(str(obj["object_type"]), str(obj["schema"]), str(obj["name"])): obj
		for obj in objects
	}
	return [unique[key] for key in sorted(unique)]


_ROUTINE_TYPES: dict[str, str] = {"procedure": "PROCEDURE", "function": "FUNCTION"}


def list_routines(
	conn: Any,
	catalog: str,
	schema: str | None,
	kind: str,
) -> list[dict[str, Any]]:
	"""Return one routine kind from information_schema.

	MySQL does not overload routines, so a name is unique within its schema and
	the returned signature is always empty. It is present because the shared
	routine record carries it, not because MySQL needs one to address a routine.
	"""
	routine_type = _ROUTINE_TYPES.get(kind)
	if routine_type is None:
		raise DatabaseError(f"mysql_utils: unsupported routine kind '{kind}'.")

	target_schema = schema or catalog
	cursor = conn.cursor(dictionary=True)
	try:
		cursor.execute(
			"""
			SELECT routine_schema AS schema_name, routine_name AS routine_name
			FROM information_schema.routines
			WHERE routine_type = %s AND routine_schema = %s
			ORDER BY routine_schema, routine_name
			""",
			[routine_type, target_schema],
		)
		rows = cursor.fetchall()
		# The parameters (backlog 747). Ordinal 0 is a function's return value,
		# not a parameter. MySQL routine parameters take no defaults.
		cursor.execute(
			"""
			SELECT specific_name AS routine_name, ordinal_position AS ordinal,
			       parameter_name AS parameter_name, parameter_mode AS mode,
			       dtd_identifier AS data_type
			FROM information_schema.parameters
			WHERE routine_type = %s AND specific_schema = %s AND ordinal_position > 0
			ORDER BY specific_name, ordinal_position
			""",
			[routine_type, target_schema],
		)
		parameter_rows = cursor.fetchall()
	finally:
		cursor.close()

	parameters: dict[str, list[dict[str, Any]]] = {}
	for row in parameter_rows:
		parameters.setdefault(str(row["routine_name"]), []).append({
			"name": str(row["parameter_name"] or ""),
			"ordinal": int(row["ordinal"]),
			"type": str(row["data_type"] or ""),
			"mode": str(row["mode"] or "IN"),
			"has_default": False,
			"default": None,
		})

	return [
		{
			"schema": str(row["schema_name"]),
			"name": str(row["routine_name"]),
			"signature": "",
			"parameters": parameters.get(str(row["routine_name"]), []),
			# A MySQL function answers one value, never rows (backlog 822).
			"returns_rows": False,
		}
		for row in rows
	]


def get_routine_definition(
	conn: Any,
	catalog: str,
	schema: str,
	name: str,
	signature: str,
	kind: str,
) -> str:
	"""Return one routine's definition through SHOW CREATE.

	SHOW CREATE takes no parameters, so both identifiers are validated before
	they are quoted into the statement. The signature is not part of a MySQL
	routine's address and takes no part in resolving one.
	"""
	routine_type = _ROUTINE_TYPES.get(kind)
	if routine_type is None:
		raise DatabaseError(f"mysql_utils: unsupported routine kind '{kind}'.")

	target_schema = schema or catalog
	_validate_identifier(target_schema, "routine schema")
	_validate_identifier(name, "routine name")

	cursor = conn.cursor(dictionary=True)
	try:
		cursor.execute(f"SHOW CREATE {routine_type} `{target_schema}`.`{name}`")
		row = cursor.fetchone() or {}
	finally:
		cursor.close()

	definition = row.get(f"Create {routine_type.capitalize()}")
	if not definition:
		raise DatabaseError(
			f"mysql_utils: {kind} not found: {target_schema}.{name}"
		)
	return str(definition).rstrip() + ";"


def _mysql_dependencies(
	conn: mysql.connector.MySQLConnection,
	database: str,
) -> dict[str, list[dict[str, str]]]:
	"""Return discoverable dependencies keyed by exporter object key."""
	result: dict[str, list[dict[str, str]]] = {}
	cursor = conn.cursor(dictionary=True)
	try:
		cursor.execute(
			"""
			SELECT view_schema, view_name, table_schema, table_name
			FROM information_schema.view_table_usage
			WHERE view_schema = %s
			""",
			[database],
		)
		for row in cursor.fetchall():
			key = f"view:{row['view_schema']}.{row['view_name']}"
			dep = {
				"object_type": "table",
				"schema": str(row["table_schema"]),
				"name": str(row["table_name"]),
			}
			result.setdefault(key, []).append(dep)

		cursor.execute(
			"""
			SELECT trigger_schema, trigger_name, event_object_schema, event_object_table
			FROM information_schema.triggers
			WHERE trigger_schema = %s
			""",
			[database],
		)
		for row in cursor.fetchall():
			key = f"trigger:{row['trigger_schema']}.{row['trigger_name']}"
			dep = {
				"object_type": "table",
				"schema": str(row["event_object_schema"]),
				"name": str(row["event_object_table"]),
			}
			result.setdefault(key, []).append(dep)

		cursor.execute(
			"""
			SELECT constraint_schema, constraint_name, table_name
			FROM information_schema.table_constraints
			WHERE constraint_schema = %s
			""",
			[database],
		)
		for row in cursor.fetchall():
			key = f"constraint:{row['constraint_schema']}.{row['constraint_name']}"
			dep = {
				"object_type": "table",
				"schema": str(row["constraint_schema"]),
				"name": str(row["table_name"]),
			}
			result.setdefault(key, []).append(dep)

		cursor.execute(
			"""
			SELECT table_schema, index_name, table_name
			FROM information_schema.statistics
			WHERE table_schema = %s
			GROUP BY table_schema, index_name, table_name
			""",
			[database],
		)
		for row in cursor.fetchall():
			key = f"index:{row['table_schema']}.{row['index_name']}"
			dep = {
				"object_type": "table",
				"schema": str(row["table_schema"]),
				"name": str(row["table_name"]),
			}
			result.setdefault(key, []).append(dep)
	finally:
		cursor.close()

	for key, deps in result.items():
		uniq = {(d["object_type"], d["schema"], d["name"]): d for d in deps}
		result[key] = [uniq[k] for k in sorted(uniq)]
	return result
