"""Private SQLAlchemy connection boundary for Rey database providers.

SQLAlchemy objects stay inside :mod:`rey_lib.db`.  Application callers receive
``ReyConnection``, which preserves the small connection surface they already
use while provider implementations use the helpers in this module.
"""

from __future__ import annotations

import threading

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.errors.error_utils import UnsupportedDatabaseCapabilityError

#: The bound on physical sessions per logical connection configuration (#1309):
#: one Engine per configuration, at most this many sessions in it, no overflow.
POOL_SIZE = 1
POOL_MAX_OVERFLOW = 0
#: How long a checkout waits for the bound session before it is refused, so that
#: a second checkout ends in an error rather than a hang.
POOL_CHECKOUT_SECONDS = 30.0


def _has_transaction(session: Any) -> bool:
    """Whether ``session`` holds a transaction on the server.

    SQLAlchemy begins a transaction object on every first statement, even on a
    session the driver runs in autocommit, where nothing is held on the server
    and ending the session loses nothing. Only a session the driver is NOT
    running in autocommit holds what it has begun.
    """
    probe = getattr(session, "in_transaction", None)
    if not callable(probe) or not probe():
        return False
    pooled = getattr(session, "connection", None)
    driver = getattr(pooled, "driver_connection", pooled)
    return not bool(getattr(driver, "autocommit", False))


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

    def __init__(self, provider: str, engine: Any, connection: Any = None) -> None:
        """Own the Engine for life; hold a session only while it is in use (#1309).

        ``connection`` is the session the opener verified the configuration
        with, kept as the one in use until ``release_idle``. Every operation on
        this handle uses that one session, so nested use never asks the bounded
        pool for a second.
        """
        self.provider = provider
        self._engine = engine
        self._active: Any = connection
        self._open_cursors = 0
        self._closed = False
        self._guard = threading.RLock()

    def _session(self) -> Any:
        """The session in use, checked out of the Engine's pool on first need."""
        with self._guard:
            if self._closed:
                raise DatabaseError("the connection handle is closed")
            if self._active is None:
                self._active = self._engine.connect()
            return self._active

    def _give_back(self) -> Any:
        """Return the session in use to the pool when nothing holds it; else None.

        Nothing is given back while a cursor is open on it or a transaction has
        been begun on it: those are the two ways a caller still holds it.
        """
        with self._guard:
            session = self._active
            if session is None or self._open_cursors > 0 or _has_transaction(session):
                return None
            self._active = None
        session.close()
        return session

    def release_idle(self) -> bool:
        """End the idle physical session, if the handle holds one nobody is using.

        Returns True when a session was ended. The Engine stays; the next
        operation checks a fresh session out. Never ends a session a cursor or
        a transaction still holds.
        """
        if self._closed or self._give_back() is None:
            return False
        self._engine.dispose()
        return True

    def close(self) -> None:
        if self._closed:
            return
        try:
            with self._guard:
                session, self._active = self._active, None
            if session is not None:
                session.close()
        finally:
            self._engine.dispose()
            self._closed = True

    def commit(self) -> None:
        self._session().commit()

    def rollback(self) -> None:
        self._session().rollback()

    def cursor(self, *args: Any, **kwargs: Any) -> "ReyCursor":
        """A DBAPI cursor for an approved provider-specific fallback.

        The session stays in use until the cursor is closed.
        """
        with self._guard:
            cursor = raw_dbapi_connection(self).cursor(*args, **kwargs)
            self._open_cursors += 1
        return ReyCursor(self, cursor)

    def _cursor_closed(self) -> None:
        with self._guard:
            self._open_cursors = max(0, self._open_cursors - 1)

    def __enter__(self) -> "ReyConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()


class ReyCursor:
    """A DBAPI cursor that keeps its handle's session in use until closed (#1309).

    Everything else is the driver cursor's own: execute, fetch, iteration,
    description. Closing it, once, lets the handle give its session back.
    """

    def __init__(self, handle: ReyConnection, cursor: Any) -> None:
        self._handle = handle
        self._cursor = cursor
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._cursor.close()
        finally:
            self._handle._cursor_closed()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def __enter__(self) -> "ReyCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
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
        # The bound (#1309): one session per configuration, none beyond it, a
        # bounded wait for it, and a stale pooled session noticed before use.
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_timeout=POOL_CHECKOUT_SECONDS,
        pool_pre_ping=True,
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
        raise UnsupportedDatabaseCapabilityError("connection is not SQLAlchemy-backed")
    return conn._session()


@contextmanager
def own_connection(conn: Any, *, isolation_level: str = "") -> Iterator[Any]:
    """Check out a connection of this operation's own, from the same Engine.

    For the operation that needs a transaction. The handle a caller holds is
    **shared** -- ``Connection.handle()`` returns one object to every consumer
    of a configured name, and the Console serves requests concurrently -- and
    PostgreSQL handles are opened AUTOCOMMIT precisely so that no consumer can
    leave a transaction open across another's work. Beginning one on the shared
    connection would recreate the failure that decision was made to end.

    So this borrows a second connection from the Engine the handle already
    carries, applies the isolation level to that connection alone, and closes
    it on the way out. The shared connection's state is never read or changed.

    Args:
        conn: A Rey handle, used only to reach its Engine.
        isolation_level: The level to execute under, or "" for the Engine's.

    Yields:
        A SQLAlchemy Connection owned by this operation.

    Raises:
        TypeError: If the handle is not SQLAlchemy-backed.
    """
    if not isinstance(conn, ReyConnection):
        raise UnsupportedDatabaseCapabilityError("connection is not SQLAlchemy-backed")
    # The pool is bounded to one session (#1309): the handle's idle session is
    # given back first so this checkout takes that very session, and a session
    # still held (an open cursor, a begun transaction) makes the wait end in a
    # refusal rather than a hang.
    conn._give_back()
    from sqlalchemy.exc import TimeoutError as PoolTimeout  # noqa: PLC0415

    try:
        borrowed = conn._engine.connect()
    except PoolTimeout as exc:
        raise DatabaseError(
            "no session for the operation: the connection's one session is held "
            f"by an open cursor or transaction (waited {POOL_CHECKOUT_SECONDS:g} s)"
        ) from exc
    try:
        if isolation_level:
            borrowed = borrowed.execution_options(
                isolation_level=isolation_level,
            )
        yield borrowed
    finally:
        # Returned to the pool whatever happened. A borrowed connection left
        # checked out is the leak this whole change is about.
        borrowed.close()


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
                "check_constraints": [
                    dict(value)
                    for value in inspector.get_check_constraints(table_name, schema=schema)
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


def inspect_schema_structure(conn: Any, schema: str) -> dict[str, Any]:
    """The schema's structure facts in the shape every provider answers (backlog 725).

    The provider-side implementation of ``schema_metadata`` for the providers
    SQLAlchemy inspects. Tables by name with their keys, indexes and
    constraints; views by name. No column facts (those are table_column_catalog's,
    724) and no view definitions (DatabaseView.read_ddl's) -- and nothing here
    constructs a database object.
    """
    try:
        from sqlalchemy import inspect  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise ConfigError("SQLAlchemy is required for database inspection.") from exc

    inspector = inspect(core_connection(conn))
    tables: list[dict[str, Any]] = []
    for table_name in inspector.get_table_names(schema=schema):
        pk = inspector.get_pk_constraint(table_name, schema=schema) or {}
        tables.append({
            "name": str(table_name),
            "primary_key": (
                {"name": pk.get("name"),
                 "constrained_columns": list(pk.get("constrained_columns") or [])}
                if pk.get("constrained_columns") else {}
            ),
            "foreign_keys": [
                {"name": fk.get("name"),
                 "constrained_columns": list(fk.get("constrained_columns") or []),
                 "referred_schema": fk.get("referred_schema"),
                 "referred_table": fk.get("referred_table"),
                 "referred_columns": list(fk.get("referred_columns") or [])}
                for fk in inspector.get_foreign_keys(table_name, schema=schema)
            ],
            "indexes": [
                {"name": ix.get("name"),
                 "column_names": list(ix.get("column_names") or []),
                 "unique": bool(ix.get("unique", False))}
                for ix in inspector.get_indexes(table_name, schema=schema)
            ],
            "unique_constraints": [
                {"name": uq.get("name"), "column_names": list(uq.get("column_names") or [])}
                for uq in inspector.get_unique_constraints(table_name, schema=schema)
            ],
            "check_constraints": [
                {"name": ck.get("name")}
                for ck in inspector.get_check_constraints(table_name, schema=schema)
            ],
        })
    views = [{"name": str(name)} for name in inspector.get_view_names(schema=schema)]
    return {"tables": tables, "views": views}


def inspect_primary_key(conn: Any, schema: str, table: str) -> list[str]:
    """One table's primary-key columns, in key order, as the inspector reports them.

    The same read ``inspect_schema_structure`` makes for every table, scoped to
    one table, for the providers SQLAlchemy inspects. Empty when the table has
    no primary key.
    """
    inspector, _ = _inspector(conn)
    constraint = inspector.get_pk_constraint(table, schema=schema) or {}
    return [str(name) for name in constraint.get("constrained_columns") or []]


def _inspector(conn: Any) -> tuple[Any, Any]:
    try:
        from sqlalchemy import inspect  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise ConfigError("SQLAlchemy is required for database inspection.") from exc
    sa_conn = core_connection(conn)
    return inspect(sa_conn), sa_conn


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

