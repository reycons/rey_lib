"""
Generic MySQL connection and query execution layer.

Owns all MySQL connections, query execution, transaction handling,
and bulk loading. No raw mysql.connector calls are permitted outside
this module.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from rey_lib.errors.error_utils import DatabaseError, ConfigError

__all__ = [
	"init_db",
	"get_connection",
	"execute",
	"fetch",
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

_logger = logging.getLogger(__name__)

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


def get_connection(db_cfg: Any) -> mysql.connector.MySQLConnection:
	timeout = int(getattr(db_cfg, "timeout", 30))
	return _connect_with_retry(db_cfg, timeout)


def execute(
	conn: mysql.connector.MySQLConnection,
	sql_name: str,
	params: Optional[list[Any]] = None,
) -> Any:
	sql = load_sql(sql_name)
	return _run_cursor(conn, sql, params, error_context=f"Query '{sql_name}'")


def fetch(
	conn: mysql.connector.MySQLConnection,
	sql_name: str,
	params: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
	cursor = execute(conn, sql_name, params)

	try:
		return cursor.fetchall()
	finally:
		cursor.close()


def fetch_dicts(
	conn: mysql.connector.MySQLConnection,
	sql_name: str,
	params: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
	return fetch(conn, sql_name, params)


def bulk_insert(
	conn: mysql.connector.MySQLConnection,
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

	col_list = ", ".join(quote_identifier(col) for col in columns)
	placeholders = ", ".join(["%s"] * len(columns))

	sql = (
		f"INSERT INTO {quote_identifier(schema)}.{quote_identifier(table)} "
		f"({col_list}) VALUES ({placeholders})"
	)

	value_rows = _prepare_bulk_insert_rows(rows, columns)

	cursor = conn.cursor()

	try:
		cursor.executemany(sql, value_rows)

		row_count = len(rows)

		_logger.debug(
			"bulk_insert: %d row(s) → %s.%s",
			row_count,
			schema,
			table,
		)

		return row_count

	except MySQLError as exc:
		_logger.error("bulk_insert failed table=%s.%s", schema, table)
		_logger.error("bulk_insert columns=%s", columns)

		if value_rows:
			for col, val in zip(columns, value_rows[0]):
				_logger.error("bulk_insert first_row column=%s value=%r", col, val)

		raise DatabaseError(f"bulk_insert failed for {schema}.{table}: {exc}") from exc

	finally:
		cursor.close()


def call_proc(
	conn: mysql.connector.MySQLConnection,
	proc_name: str,
	params: Optional[list[Any]] = None,
) -> Any:
	p = params or []
	cursor = conn.cursor(dictionary=True)

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
	sql = """
		SELECT
			column_name
		FROM information_schema.columns
		WHERE table_schema = %s
			AND table_name = %s
		ORDER BY ordinal_position
	"""

	cursor = conn.cursor()

	try:
		cursor.execute(sql, [schema, table])
		return [row[0] for row in cursor.fetchall()]
	finally:
		cursor.close()


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
	conn: mysql.connector.MySQLConnection,
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

	cursor = conn.cursor()

	try:
		cursor.execute(ddl)
		conn.commit()

		_logger.info("Staging table ready: %s.%s", schema, table)

		return True

	except MySQLError as exc:
		conn.rollback()
		raise DatabaseError(
			f"Failed to create staging table '{schema}.{table}': {exc}"
		) from exc

	finally:
		cursor.close()


def is_truncation_error(exc: Exception) -> bool:
	return _is_mysql_error(exc, _TRUNCATION_ERROR)


def _require_init() -> None:
	if _sql_dir is None:
		raise RuntimeError("mysql_utils.init_db() must be called before using the database.")


def _run_cursor(
	conn: mysql.connector.MySQLConnection,
	sql: str,
	params: Optional[list[Any]],
	error_context: str,
) -> Any:
	cursor = conn.cursor(dictionary=True)

	try:
		cursor.execute(sql, params or [])
		_logger.debug("_run_cursor: %s", error_context)
		return cursor

	except MySQLError as exc:
		cursor.close()
		raise DatabaseError(f"{error_context} failed: {exc}") from exc


def _connect_with_retry(db_cfg: Any, timeout: int) -> mysql.connector.MySQLConnection:
	last_exc: Exception | None = None

	for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
		try:
			conn = mysql.connector.connect(
				host=str(db_cfg.host),
				port=int(getattr(db_cfg, "port", 3306)),
				database=str(db_cfg.database),
				user=str(getattr(db_cfg, "user", "")),
				password=str(getattr(db_cfg, "password", "")),
				connection_timeout=timeout,
				autocommit=False,
				allow_local_infile=bool(getattr(db_cfg, "allow_local_infile", False)),
			)

			_logger.debug(
				"MySQL connected (attempt %d of %d).",
				attempt,
				_MAX_CONNECT_ATTEMPTS,
			)

			return conn

		except MySQLError as exc:
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
	if not isinstance(exc, MySQLError):
		return False

	errno = getattr(exc, "errno", None)

	if errno == error_code:
		return True

	return str(error_code) in str(exc)


def _validate_identifier(name: str, label: str) -> None:
	if not re.fullmatch(r"[\w]+", name):
		raise DatabaseError(
			f"Invalid MySQL identifier for {label}: '{name}'. "
			f"Only alphanumeric characters and underscores are permitted."
		)