"""Private SQLAlchemy connection boundary for Rey database providers.

SQLAlchemy objects stay inside :mod:`rey_lib.db`.  Application callers receive
``ReyConnection``, which preserves the small connection surface they already
use while provider implementations use the helpers in this module.
"""

from __future__ import annotations

from typing import Any

from rey_lib.errors.error_utils import ConfigError


def _sqlalchemy() -> tuple[Any, Any]:
    try:
        from sqlalchemy import URL, create_engine  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise ConfigError(
            "SQLAlchemy is required for PostgreSQL and MySQL connections. "
            "Install it with: pip install 'sqlalchemy>=2,<3'"
        ) from exc
    return URL, create_engine


class ReyConnection:
    """Internal-compatible connection handle returned by ``DBAdapter``.

    This is deliberately not a new application contract.  It implements the
    connection lifecycle methods existing callers use and retains DBAPI cursor
    access solely for approved vendor-specific fallbacks.
    """

    def __init__(self, provider: str, engine: Any, connection: Any) -> None:
        self.provider = provider
        self._engine = engine
        self._connection = connection
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._connection.close()
        finally:
            self._engine.dispose()
            self._closed = True

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        """Return a DBAPI cursor for an approved provider-specific fallback."""
        return raw_dbapi_connection(self).cursor(*args, **kwargs)

    def __enter__(self) -> "ReyConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()


def open_connection(
    provider: str,
    drivername: str,
    *,
    username: str,
    password: str,
    host: str,
    port: int,
    database: str,
    connect_args: dict[str, Any] | None = None,
    isolation_level: str = "",
) -> ReyConnection:
    """Create an Engine and return its connection behind the Rey handle.

    ``isolation_level`` is applied to the Engine, so it is in force before any
    statement runs and is never toggled per call. PostgreSQL passes AUTOCOMMIT:
    its connections are shared, and an implicit transaction left open by a
    failed statement made every later consumer fail whatever its own SQL said.
    """
    URL, create_engine = _sqlalchemy()
    url = URL.create(
        drivername=drivername,
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    engine = create_engine(
        url,
        connect_args=connect_args or {},
        **({"isolation_level": isolation_level} if isolation_level else {}),
    )
    try:
        connection = engine.connect()
    except Exception:
        engine.dispose()
        raise
    return ReyConnection(provider, engine, connection)


def core_connection(conn: Any) -> Any:
    """Return the private SQLAlchemy Connection for a Rey handle."""
    if not isinstance(conn, ReyConnection):
        raise TypeError("connection is not SQLAlchemy-backed")
    return conn._connection


def raw_dbapi_connection(conn: Any) -> Any:
    """Return the driver connection for a narrow vendor-specific fallback."""
    sa_conn = core_connection(conn)
    pool_connection = sa_conn.connection
    return getattr(pool_connection, "driver_connection", pool_connection)


def is_sqlalchemy_connection(conn: Any) -> bool:
    return isinstance(conn, ReyConnection)


def inspect_schema(conn: Any, schema: str) -> dict[str, Any]:
    """Return normalized Inspector metadata without exposing SA objects."""
    try:
        from sqlalchemy import inspect  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise ConfigError("SQLAlchemy is required for database inspection.") from exc

    sa_conn = core_connection(conn)
    inspector = inspect(sa_conn)
    tables: list[dict[str, Any]] = []
    for table_name in inspector.get_table_names(schema=schema):
        columns = []
        for column in inspector.get_columns(table_name, schema=schema):
            column_type = column.get("type")
            if hasattr(column_type, "compile"):
                type_name = str(column_type.compile(dialect=sa_conn.dialect))
            else:
                type_name = str(column_type or "")
            columns.append(
                {
                    "name": str(column.get("name", "")),
                    "type": type_name,
                    "nullable": bool(column.get("nullable", True)),
                    "default": column.get("default"),
                }
            )
        tables.append(
            {
                "name": str(table_name),
                "columns": columns,
                "primary_key": dict(
                    inspector.get_pk_constraint(table_name, schema=schema) or {}
                ),
                "foreign_keys": [
                    dict(value)
                    for value in inspector.get_foreign_keys(table_name, schema=schema)
                ],
                "indexes": [
                    dict(value)
                    for value in inspector.get_indexes(table_name, schema=schema)
                ],
                "unique_constraints": [
                    dict(value)
                    for value in inspector.get_unique_constraints(table_name, schema=schema)
                ],
            }
        )

    views = [
        {
            "name": str(view_name),
            "definition": inspector.get_view_definition(view_name, schema=schema),
        }
        for view_name in inspector.get_view_names(schema=schema)
    ]
    return {
        "schemas": [str(name) for name in inspector.get_schema_names()],
        "tables": tables,
        "views": views,
    }


def _inspector(conn: Any) -> tuple[Any, Any]:
    try:
        from sqlalchemy import inspect  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise ConfigError("SQLAlchemy is required for database inspection.") from exc
    sa_conn = core_connection(conn)
    return inspect(sa_conn), sa_conn


def _schema_name(inspector: Any, schema: str | None) -> str:
    value = schema if schema is not None else inspector.default_schema_name
    return str(value or "")


def _name_list(values: Any) -> list[str]:
    return [str(value) for value in (values or []) if value is not None]


def _optional_name(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _primitive(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def metadata_list_schemas(conn: Any, catalog: str) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    return [
        {"catalog": str(catalog), "name": str(name)}
        for name in inspector.get_schema_names()
    ]


def metadata_list_tables(
    conn: Any,
    catalog: str,
    schema: str | None,
) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    schema_name = _schema_name(inspector, schema)
    return [
        {"catalog": str(catalog), "schema": schema_name, "name": str(name)}
        for name in inspector.get_table_names(schema=schema or None)
    ]


def metadata_list_views(
    conn: Any,
    catalog: str,
    schema: str | None,
) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    schema_name = _schema_name(inspector, schema)
    return [
        {"catalog": str(catalog), "schema": schema_name, "name": str(name)}
        for name in inspector.get_view_names(schema=schema or None)
    ]


def metadata_get_columns(
    conn: Any,
    catalog: str,
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    inspector, sa_conn = _inspector(conn)
    result: list[dict[str, Any]] = []
    for position, column in enumerate(
        inspector.get_columns(table, schema=schema), start=1
    ):
        column_type = column.get("type")
        type_text = (
            str(column_type.compile(dialect=sa_conn.dialect))
            if hasattr(column_type, "compile")
            else str(column_type or "")
        )
        result.append(
            {
                "catalog": str(catalog),
                "schema": str(schema),
                "table": str(table),
                "name": str(column.get("name", "")),
                "ordinal_position": position,
                "type": type_text,
                "nullable": bool(column.get("nullable", True)),
                "default": _primitive(column.get("default")),
            }
        )
    return result


def metadata_get_primary_key(
    conn: Any,
    catalog: str,
    schema: str,
    table: str,
) -> dict[str, Any]:
    inspector, _sa_conn = _inspector(conn)
    value = inspector.get_pk_constraint(table, schema=schema) or {}
    return {
        "catalog": str(catalog),
        "schema": str(schema),
        "table": str(table),
        "name": _optional_name(value.get("name")),
        "columns": _name_list(value.get("constrained_columns")),
    }


def metadata_get_foreign_keys(
    conn: Any,
    catalog: str,
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    return [
        {
            "catalog": str(catalog),
            "schema": str(schema),
            "table": str(table),
            "name": _optional_name(value.get("name")),
            "columns": _name_list(value.get("constrained_columns")),
            "referenced_catalog": str(catalog),
            "referenced_schema": _optional_name(value.get("referred_schema")),
            "referenced_table": str(value.get("referred_table") or ""),
            "referenced_columns": _name_list(value.get("referred_columns")),
        }
        for value in inspector.get_foreign_keys(table, schema=schema)
    ]


def metadata_get_indexes(
    conn: Any,
    catalog: str,
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    return [
        {
            "catalog": str(catalog),
            "schema": str(schema),
            "table": str(table),
            "name": _optional_name(value.get("name")),
            "columns": _name_list(value.get("column_names")),
            "unique": bool(value.get("unique", False)),
        }
        for value in inspector.get_indexes(table, schema=schema)
    ]


def metadata_get_unique_constraints(
    conn: Any,
    catalog: str,
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    inspector, _sa_conn = _inspector(conn)
    return [
        {
            "catalog": str(catalog),
            "schema": str(schema),
            "table": str(table),
            "name": _optional_name(value.get("name")),
            "columns": _name_list(value.get("column_names")),
        }
        for value in inspector.get_unique_constraints(table, schema=schema)
    ]
