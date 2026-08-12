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

from rey_lib.errors.error_utils import DatabaseError
from rey_lib.logs import get_logger

__all__ = [
	"init_db",
	"get_connection",
	"get_current_database",
	"list_database_objects",
	"get_object_ddl",
	"fetch_dicts",
	"bulk_insert",
	"call_proc",
	"call_proc_with_output",
	"load_sql",
	"create_staging_table_if_not_exists",
	"get_table_columns",
	"quote_identifier",
	"is_truncation_error",
]

_logger = get_logger(__name__)

_NEUTRAL_TYPE_MAP: dict[str, str] = {
	"TEXT":      "TEXT",
	"VARCHAR":   "VARCHAR(500)",
	"TIMESTAMP": "DATETIME",
	"INTEGER":   "INT",
}

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


def get_connection(db_cfg: Any) -> Any:
	timeout = int(getattr(db_cfg, "timeout", 30))
	return _connect_with_retry(db_cfg, timeout)


def fetch_dicts(
	conn: Any,
	sql_name: str,
	params: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
	from rey_lib.db._sqlalchemy import core_connection

	sql = load_sql(sql_name)
	try:
		result = core_connection(conn).exec_driver_sql(sql, tuple(params or []))
		return [dict(row) for row in result.mappings().all()]
	except Exception as exc:
		raise DatabaseError(f"Query '{sql_name}' failed: {exc}") from exc


def query_rows(
	conn: Any,
	sql_text: str,
	*,
	limit: int = 1_000,
) -> tuple[list[str], list[dict[str, Any]]]:
	"""Execute one bounded read query and normalize its result."""
	from rey_lib.db._sqlalchemy import core_connection

	try:
		from sqlalchemy import text

		result = core_connection(conn).execute(text(sql_text))
		columns = [str(column) for column in result.keys()]
		values = result.fetchmany(max(1, int(limit))) if columns else []
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
	raise NotImplementedError(
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


def load_sql(name: str) -> str:
	_require_init()

	if name not in _SQL:
		raise KeyError(
			f"SQL query '{name}' not found. "
			f"Available: {sorted(_SQL.keys())}"
		)

	return _SQL[name]


def create_staging_table_if_not_exists(
	conn: Any,
	schema: str,
	table: str,
	column_defs: list[tuple[str, str]],
) -> bool:
	_validate_identifier(schema, "schema")
	_validate_identifier(table, "table")

	for col_name, _ in column_defs:
		_validate_identifier(col_name, "column")

	col_sql = ",\n\t".join(
		f"{quote_identifier(col_name)} {_map_type(sql_type)} NULL"
		for col_name, sql_type in column_defs
	)

	ddl = (
		f"CREATE TABLE IF NOT EXISTS {quote_identifier(schema)}.{quote_identifier(table)} (\n"
		f"\t{col_sql}\n"
		f")"
	)

	from rey_lib.db._sqlalchemy import core_connection

	try:
		core_connection(conn).exec_driver_sql(ddl)
		conn.commit()
		_logger.info("Staging table ready: %s.%s", schema, table)
		return True
	except Exception as exc:
		conn.rollback()
		raise DatabaseError(
			f"Failed to create staging table '{schema}.{table}': {exc}"
		) from exc


def is_truncation_error(exc: Exception) -> bool:
	return _is_mysql_error(exc, _TRUNCATION_ERROR)


def _require_init() -> None:
	if _sql_dir is None:
		raise RuntimeError("mysql_utils.init_db() must be called before using the database.")


def _connect_with_retry(db_cfg: Any, timeout: int) -> Any:
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
				password=str(getattr(db_cfg, "password", "")),
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


def _map_type(sql_type: str) -> str:
	upper = sql_type.strip().upper()

	if upper in _NEUTRAL_TYPE_MAP:
		return _NEUTRAL_TYPE_MAP[upper]

	return sql_type


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
						"dependencies": [dict(dependency)],
					}
				)
		constraints = [table_metadata["primary_key"]]
		constraints.extend(table_metadata["foreign_keys"])
		constraints.extend(table_metadata["unique_constraints"])
		for constraint in constraints:
			name = str(constraint.get("name") or "")
			if name:
				objects.append(
					{
						"database": db_name,
						"object_type": "constraint",
						"schema": db_name,
						"name": name,
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
				routine_schema AS schema_name, routine_name AS object_name
			FROM information_schema.routines WHERE routine_schema = %s
			UNION ALL
			SELECT 'trigger', trigger_schema, trigger_name
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
) -> list[dict[str, str]]:
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
	finally:
		cursor.close()

	return [
		{
			"schema": str(row["schema_name"]),
			"name": str(row["routine_name"]),
			"signature": "",
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


def get_object_ddl(
	conn: mysql.connector.MySQLConnection,
	obj: dict[str, Any],
) -> str:
	"""Return native MySQL DDL for one object."""
	object_type = str(obj.get("object_type", "")).lower().rstrip("s")
	schema = str(obj.get("schema", ""))
	name = str(obj.get("name", ""))
	cursor = conn.cursor(dictionary=True)
	try:
		if object_type == "schema":
			return f"CREATE SCHEMA IF NOT EXISTS `{schema}`;"

		if object_type in ("table", "view"):
			cursor.execute(f"SHOW CREATE TABLE `{schema}`.`{name}`")
			row = cursor.fetchone() or {}
			ddl = row.get("Create Table") or row.get("Create View") or ""
			return str(ddl).rstrip() + ";"

		if object_type in ("procedure", "function"):
			cursor.execute(f"SHOW CREATE {object_type.upper()} `{schema}`.`{name}`")
			row = cursor.fetchone() or {}
			ddl = row.get("Create Procedure") or row.get("Create Function") or ""
			return str(ddl).rstrip() + ";"

		if object_type == "trigger":
			cursor.execute(f"SHOW CREATE TRIGGER `{schema}`.`{name}`")
			row = cursor.fetchone() or {}
			ddl = row.get("SQL Original Statement") or row.get("Create Trigger") or ""
			return str(ddl).rstrip() + ";"

		if object_type == "index":
			cursor.execute(
				"""
				SELECT table_name, non_unique,
					GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ', ') AS columns,
					index_type
				FROM information_schema.statistics
				WHERE table_schema = %s AND index_name = %s
				GROUP BY table_name, non_unique, index_type
				LIMIT 1
				""",
				[schema, name],
			)
			row = cursor.fetchone()
			if not row:
				raise DatabaseError(f"mysql_utils: index not found: {schema}.{name}")
			table_name = row["table_name"]
			non_unique = int(row["non_unique"])
			cols = str(row["columns"])
			prefix = "UNIQUE " if non_unique == 0 and name != "PRIMARY" else ""
			if name == "PRIMARY":
				return f"ALTER TABLE `{schema}`.`{table_name}` ADD PRIMARY KEY ({cols});"
			return f"CREATE {prefix}INDEX `{name}` ON `{schema}`.`{table_name}` ({cols});"

		if object_type == "constraint":
			cursor.execute(
				"""
				SELECT tc.table_name, tc.constraint_type,
					GROUP_CONCAT(kcu.column_name ORDER BY kcu.ordinal_position SEPARATOR ', ') AS columns,
					rc.referenced_table_name,
					GROUP_CONCAT(kcu.referenced_column_name ORDER BY kcu.ordinal_position SEPARATOR ', ') AS ref_columns
				FROM information_schema.table_constraints tc
				LEFT JOIN information_schema.key_column_usage kcu
					ON tc.constraint_schema = kcu.constraint_schema
					AND tc.constraint_name = kcu.constraint_name
					AND tc.table_name = kcu.table_name
				LEFT JOIN information_schema.referential_constraints rc
					ON tc.constraint_schema = rc.constraint_schema
					AND tc.constraint_name = rc.constraint_name
				WHERE tc.constraint_schema = %s
					AND tc.constraint_name = %s
				GROUP BY tc.table_name, tc.constraint_type, rc.referenced_table_name
				LIMIT 1
				""",
				[schema, name],
			)
			row = cursor.fetchone()
			if not row:
				raise DatabaseError(f"mysql_utils: constraint not found: {schema}.{name}")
			table_name = row["table_name"]
			constraint_type = str(row["constraint_type"]).upper()
			cols = row.get("columns") or ""
			if constraint_type == "PRIMARY KEY":
				return f"ALTER TABLE `{schema}`.`{table_name}` ADD PRIMARY KEY ({cols});"
			if constraint_type == "UNIQUE":
				return f"ALTER TABLE `{schema}`.`{table_name}` ADD CONSTRAINT `{name}` UNIQUE ({cols});"
			if constraint_type == "FOREIGN KEY":
				ref_table = row.get("referenced_table_name")
				ref_cols = row.get("ref_columns") or ""
				return (
					f"ALTER TABLE `{schema}`.`{table_name}` "
					f"ADD CONSTRAINT `{name}` FOREIGN KEY ({cols}) "
					f"REFERENCES `{schema}`.`{ref_table}` ({ref_cols});"
				)
			return f"-- Unsupported MySQL constraint type {constraint_type} for `{schema}`.`{name}`."

		return f"-- Unsupported MySQL object type '{object_type}' for `{schema}`.`{name}`."
	finally:
		cursor.close()


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
