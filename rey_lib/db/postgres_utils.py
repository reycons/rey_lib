"""
PostgreSQL connection and execution layer.

Owns all PostgreSQL connections and control database calls. SQLAlchemy Core
owns generic execution; raw DBAPI cursors remain only for approved catalog
and DDL fallbacks.

Connection details are passed as a Namespace object resolved from ctx at
call time — this module has no knowledge of ctx structure or application
config layout. A password naming an environment variable is read here, as the
connection is opened, through the one resolver in rey_lib.config; ctx is
carried for that and is otherwise untouched. Passwords are never read from
YAML.

psycopg is an optional dependency. Install with:
    pip install 'psycopg[binary]'

Function calls (SELECT) and procedure calls (CALL) are kept separate to
match the PostgreSQL distinction between functions and procedures.

Public API
----------
get_connection(db_cfg, ctx=None)
    Return an internal-compatible Rey connection handle.
render_and_execute(conn, call)
    Render one normalized RoutineCall in this dialect and execute it. The
    invocation shape is decided by the connector; this only writes it.
is_truncation_error(exc)
    Return True when exc is a PostgreSQL string truncation error.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.db.routine_call import InvocationShape, RoutineCall
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = [
    "get_connection",
    "get_current_database",
    "list_database_objects",
    "get_object_ddl",
    "render_and_execute",
    "query_rows",
    "is_truncation_error",
]

_logger = get_logger(__name__)

# PostgreSQL error code for string-data-right-truncation.
_TRUNCATION_SQLSTATE = "22001"


def _psycopg() -> Any:
    """Lazy-import Psycopg with a clear install hint if absent."""
    try:
        import psycopg  # noqa: PLC0415
        return psycopg
    except ImportError as exc:
        raise ConfigError(
            "psycopg is required for PostgreSQL connections. "
            "Install it with: pip install 'psycopg[binary]'"
        ) from exc


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _refuse_unsupported_autocommit(db_cfg: Any) -> None:
    """Refuse a configuration that asserts behaviour this provider does not give.

    PostgreSQL connections are autocommit by contract. Accepting
    ``autocommit: false`` and ignoring it would let a file claim a transaction
    lifecycle the runtime does not provide, which is worse than refusing it. A
    non-boolean is refused rather than coerced, for the same reason.
    """
    if not hasattr(db_cfg, "autocommit"):
        return
    setting = getattr(db_cfg, "autocommit", None)
    if setting is None:
        return
    name = getattr(db_cfg, "name", "<unnamed>")
    if not isinstance(setting, bool):
        raise ConfigError(
            f"postgres_utils: connection '{name}' sets autocommit to "
            f"{setting!r}; it must be a boolean."
        )
    if setting is False:
        raise ConfigError(
            f"postgres_utils: connection '{name}': PostgreSQL connections are "
            "always autocommit; autocommit: false is not supported."
        )


def get_connection(db_cfg: Any, *, ctx: Any = None) -> Any:
    """
    Open a SQLAlchemy-backed connection from a connection config Namespace.

    Parameters
    ----------
    db_cfg : Any
        Connection config Namespace. Required fields: host, database, username.
        Optional fields: port (default 5432), password.
    ctx : Any
        Application context, needed only to read a password that names an
        environment variable. A configuration holding its password literally
        connects without one.

    Returns
    -------
    ReyConnection
        Connection-like Rey handle with SQLAlchemy kept internal.

    Raises
    ------
    ConfigError
        If psycopg is not installed, required fields are missing, or a
        password names an environment variable that cannot be read now.
    DatabaseError
        If the connection attempt fails.
    """
    _psycopg()

    host     = getattr(db_cfg, "host",     None)
    port     = getattr(db_cfg, "port",     5432)
    database = getattr(db_cfg, "database", None)
    username = getattr(db_cfg, "username", None)
    password = getattr(db_cfg, "password", None) or ""

    if not host or not database or not username:
        name = getattr(db_cfg, "name", "<unnamed>")
        raise ConfigError(
            f"postgres_utils: connection '{name}' is missing required fields "
            "(host, database, username)."
        )

    _refuse_unsupported_autocommit(db_cfg)

    try:
        from rey_lib.db._sqlalchemy import open_connection  # noqa: PLC0415

        return open_connection(
            "postgres",
            "postgresql+psycopg",
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            # Resolved as the connection is opened, and held no longer than
            # the call that uses it.
            password=str(resolve_env_reference(ctx, password)),
            # The contract, applied before any statement: these connections are
            # shared, and an implicit transaction left open by one failure made
            # every later consumer fail whatever its own SQL said.
            isolation_level="AUTOCOMMIT",
        )
    except Exception as exc:
        raise DatabaseError(
            f"postgres_utils: failed to connect to '{database}' on '{host}': {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Function and procedure calls
# ---------------------------------------------------------------------------


#: How each invocation shape is written in this dialect, and how its result is
#: read. The shape is decided by the connector; this only renders it.
_STATEMENT = {
    InvocationShape.PROCEDURE:       "CALL {routine}({arguments})",
    InvocationShape.SCALAR_FUNCTION: "SELECT {routine}({arguments})",
    InvocationShape.ROW_FUNCTION:    "SELECT * FROM {routine}({arguments})",
}


def render_and_execute(conn: Any, call: RoutineCall) -> Any:
    """Render one normalized routine call in PostgreSQL and execute it.

    Arguments are stated by name -- ``name => :name`` -- because the procedure
    map binds parameter names. Passing them positionally made the map's order
    load-bearing and its names decorative: a binding listing the right names in
    the wrong order put every value in the next parameter's slot, and one
    naming a parameter the routine does not declare failed with "procedure does
    not exist" instead of saying which name was wrong.

    Parameters
    ----------
    conn : Any
        Open Rey connection handle.
    call : RoutineCall
        The already-decided call. Nothing here inspects a result mode or
        chooses a statement form.

    Returns
    -------
    Any
        A procedure's OUT row (or None), a scalar function's value, or a
        row-returning function's rows -- whichever its shape produces.

    Raises
    ------
    DatabaseError
        If execution fails.
    """
    from rey_lib.db._sqlalchemy import core_connection
    from sqlalchemy import text

    serialised = _serialise_jsonb(call.arguments)
    arguments = ", ".join(f"{key} => :{key}" for key in serialised)
    sql = _STATEMENT[call.shape].format(routine=call.routine, arguments=arguments)
    try:
        result = core_connection(conn).execute(text(sql), serialised)
        if call.shape is InvocationShape.ROW_FUNCTION:
            answer: Any = [dict(row) for row in result.mappings().all()]
        elif call.shape is InvocationShape.SCALAR_FUNCTION:
            row = result.first()
            answer = row[0] if row else None
        else:
            # A procedure with no OUT parameters returns no row at all, and
            # asking such a result for one raises rather than returning empty.
            row = result.mappings().first() if result.returns_rows else None
            answer = dict(row) if row is not None else None
        # No commit and no rollback: one routine call is one statement, and the
        # connection is in AUTOCOMMIT. A procedure's own writes are inside that
        # single call and are atomic without help from here.
        return answer
    except Exception as exc:
        raise DatabaseError(
            f"postgres_utils: {call.shape.value} call failed for "
            f"'{call.routine}': {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Ad hoc SQL execution
# ---------------------------------------------------------------------------


def run_sql(
    conn: Any,
    sql_text: str,
    params: Optional[list[Any]] = None,
) -> int:
    """
    Execute an ad hoc SQL statement (or script) and commit.

    Used for executing generated DDL files where a named query or stored
    procedure is not appropriate. Execution still goes through the managed
    connection obtained from get_connection(). The whole text is sent as a
    single execute() call, so multi-statement DDL scripts run in one
    transaction and are committed together.

    Parameters
    ----------
    conn : Any
        Open Rey connection handle.
    sql_text : str
        SQL statement(s) to execute.
    params : Optional[list[Any]]
        Positional query parameters, or None for parameterless DDL.

    Returns
    -------
    int
        Number of rows affected by the last statement (cursor.rowcount), or
        -1 when the backend does not report a row count.

    Raises
    ------
    DatabaseError
        If execution fails. The transaction is rolled back before raising.
    """
    from rey_lib.db._sqlalchemy import core_connection

    core = core_connection(conn)
    # The one operation with a real atomicity contract, so the one place
    # transaction language belongs. Issued as SQL because the connection is in
    # AUTOCOMMIT: SQLAlchemy emits no BEGIN there, but PostgreSQL honours one
    # sent as a statement.
    try:
        core.exec_driver_sql("BEGIN")
        try:
            parameters = tuple(params) if params else None
            result = core.exec_driver_sql(sql_text, parameters)
            rowcount = result.rowcount
            core.exec_driver_sql("COMMIT")
            return rowcount
        except Exception:
            # The script failed. A rollback that also fails must not replace
            # the reason the script did.
            try:
                core.exec_driver_sql("ROLLBACK")
            except Exception:  # noqa: BLE001 -- the original error is the answer
                pass
            raise
    except Exception as exc:
        raise DatabaseError(
            f"postgres_utils: run_sql failed: {exc}"
        ) from exc


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
        # No rollback: the connection is in AUTOCOMMIT, so a failed read leaves
        # no transaction to clear and the next consumer is unaffected. The
        # rollback that used to be here existed only to undo the implicit
        # transaction this contract removed.
        raise DatabaseError(f"DBAdapter: query failed: {exc}") from exc


def execute_statements(
    conn: Any,
    sql_text: str,
    *,
    limit: int = 1_000,
) -> list[Any]:
    """Execute SQL text as written and return every result set it exposes.

    A DBAPI cursor rather than ``exec_driver_sql``. Two reasons, and both are
    about the batch:

    - nothing is bound. The text is the reader's own, so ``text``'s ``:name``
      scan would break a definition body or any literal containing a colon,
      and a cursor binds nothing when given no parameters.
    - a batch has to be **traversed**. SQLAlchemy's result wraps the first set
      and closes its cursor once that set is exhausted or empty, so there is
      nothing left to advance; the driver's own cursor is what steps through
      the sequence PostgreSQL returned.

    PostgreSQL returns a response sequence per command. Psycopg 3 exposes every
    one of them through ``nextset``; psycopg2 exposed only the last and raised
    from ``nextset``, which is the whole of what this migration changed.
    """
    from rey_lib.db._sqlalchemy import raw_dbapi_connection
    from rey_lib.db.db_adapter import StatementResult

    cursor = None
    try:
        cursor = raw_dbapi_connection(conn).cursor()
        cursor.execute(sql_text)
        collected: list[Any] = []
        while _advanced(cursor) if collected else True:
            description = getattr(cursor, "description", None) or []
            columns = [str(column[0]) for column in description]
            values = cursor.fetchmany(max(1, int(limit))) if columns else []
            collected.append(StatementResult(
                columns=columns,
                rows=[dict(zip(columns, row)) for row in values],
                row_count=_affected(cursor),
            ))
            if len(collected) >= _MAX_RESULT_SETS:
                raise DatabaseError(
                    "postgres_utils: the driver reported more than "
                    f"{_MAX_RESULT_SETS} result sets from one execution."
                )
        return collected
    except Exception as exc:
        # No rollback: the connection is in AUTOCOMMIT, so a failed statement
        # leaves no transaction to clear and the next consumer is unaffected.
        raise DatabaseError(f"postgres_utils: execution failed: {exc}") from exc
    finally:
        if cursor is not None and hasattr(cursor, "close"):
            cursor.close()


#: The ceiling on one execution's result sets, as the shared path has.
#:
#: DBAPI says ``nextset`` answers true or None. A driver answering true forever
#: would spin inside a request thread, and that is a broken driver rather than a
#: batch of that size.
_MAX_RESULT_SETS = 256


def _advanced(cursor: Any) -> bool:
    """Whether the cursor moved to another result set.

    A presence check, and no ``try``: a driver that *has* the method and fails
    while advancing has failed mid-batch, which is an execution error and not
    the end of the results. Swallowing it would report a truncated batch as a
    complete one.

    Psycopg 3 always has it, so the check never fires there. It is written this
    way because a cursor without it exposes one set, which is an answer, and a
    total function is worth more than an assumption about who is calling.
    """
    nextset = getattr(cursor, "nextset", None)
    return bool(nextset()) if callable(nextset) else False


def _affected(result: Any) -> int | None:
    """What the statement affected, or None where the driver said nothing real.

    -1 is DBAPI's "unknown", and a SELECT's rowcount is not a promise. Only a
    meaningful count crosses; the rest is reported as unknown.
    """
    count = getattr(result, "rowcount", None)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    return None if count < 0 else count


#: How much of a statement identifies it in a log. Enough to recognise which
#: query was running; not the whole text, which can be long and can carry
#: literals a caller inlined.
_STATEMENT_GLIMPSE = 160


def _statement_glimpse(sql_text: str) -> str:
    """One statement, shortened to what identifies it."""
    said = " ".join(str(sql_text or "").split())
    return said if len(said) <= _STATEMENT_GLIMPSE else f"{said[:_STATEMENT_GLIMPSE]}..."


def execute_named_sql(
    conn: Any,
    sql_text: str,
    named_params: dict[str, Any],
    result_mode: str,
) -> Any:
    """
    Execute parameter-bound SQL text and return a value per ``result_mode``.

    Named ``:param`` placeholders (never ``::type`` casts) are bound safely by
    SQLAlchemy Core; values are always bound and never interpolated.

    Parameters
    ----------
    conn : Any
        Open Rey connection handle.
    sql_text : str
        SQL statement with ``:name`` bind placeholders.
    named_params : dict[str, Any]
        Bind name → value (jsonb values are serialised).
    result_mode : str
        ``no_return`` (commit, return None), ``scalar_result`` (return one
        value), or ``dataset_result`` (return list[dict] rows).

    Returns
    -------
    Any
        None, a scalar, or a list of column→value dict rows.

    Raises
    ------
    DatabaseError
        On execution failure, an unsupported result_mode, or a scalar_result
        that returns no row / more than one value.
    """
    serialised = _serialise_jsonb(named_params or {})
    from rey_lib.db._sqlalchemy import core_connection

    try:
        from sqlalchemy import text

        # The estate's database boundary. Stated before control crosses it,
        # because what the driver raises says what went wrong and never what
        # was being attempted -- and the error boundary that finally catches it
        # is further still from knowing.
        #
        # Identify and locate, not replay: the statement's opening and the
        # names of its binds. Bound values are deliberately absent; they are
        # the literals this is careful not to write to disk.
        _logger.debug(
            "attempting database execute mode=%s binds=%s statement=%s",
            result_mode, sorted(serialised), _statement_glimpse(sql_text),
        )
        result = core_connection(conn).execute(text(sql_text), serialised)
        if result_mode == "no_return":
            return None
        if result_mode == "scalar_result":
            row = result.first()
            if row is None:
                raise DatabaseError(
                    "execute_named_sql: scalar_result expected a row but none was returned."
                )
            if len(row) > 1:
                raise DatabaseError(
                    "execute_named_sql: scalar_result expected one "
                    f"value but {len(row)} were returned."
                )
            return row[0]
        if result_mode == "dataset_result":
            rows = [dict(row) for row in result.mappings().all()]
            return rows
        raise DatabaseError(
            f"execute_named_sql: unsupported result_mode '{result_mode}'."
        )
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(
            f"postgres_utils: execute_named_sql failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Bulk load
# ---------------------------------------------------------------------------


#: How many rows one bulk insert sends per execute. Chosen so that an ordinary
#: load -- every caller in the estate except the code index -- still travels in
#: one batch, while a six-figure load is handed over in pieces the server can
#: interleave other work between.
BULK_INSERT_BATCH_SIZE = 5000


def bulk_insert(
    conn: Any,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    batch_size: int = BULK_INSERT_BATCH_SIZE,
) -> int:
    """Insert many rows into one table in as few round trips as the driver allows.

    The adapter's bulk contract, answered for PostgreSQL: the caller supplies
    the table and the column order, and this owns quoting, placeholders and
    batching. A caller that built its own multi-row statement would be a second
    implementation of a mechanism that already exists.

    **An empty string is inserted as an empty string.** The MySQL and SQL Server
    implementations map it to NULL, which they do for their own loading
    history; PostgreSQL distinguishes the two and so does this estate -- an
    empty ``owner`` means a symbol declared at top level, and turning it into
    NULL would both lose that and violate a NOT NULL column. Provider detail,
    decided in the provider.

    Sent in batches, not one statement and not one per row. It does not commit
    -- it takes the same path ``execute_named_sql`` does and inherits the same
    behaviour.

    **Why a batch size at all.** The estate stages roughly 95,000 rows in one
    indexing run, 86% of them reference edges. Handed over as a single
    execute, the whole set is materialized as one parameter block and pushed at
    the server in one uninterruptible burst; the database is on the same host
    as everything else here, so the rest of the estate waits on it. Batching
    bounds the memory a load holds and leaves the server able to answer other
    callers between batches. The default is generous enough that ordinary
    loads, which are far smaller, still travel in a single batch and behave
    exactly as they did.

    Args:
        conn: Open connection handle.
        schema: Target schema.
        table: Target table.
        rows: Row dicts. Keys must include every entry of ``columns``; anything
            else is ignored. An empty list inserts nothing and is not an error.
        columns: Column names defining the insert order.
        batch_size: How many rows travel per execute. Must be positive.

    Returns:
        How many rows were inserted.

    Raises:
        DatabaseError: If an identifier is not a plain name, or the insert
            fails.
    """
    if batch_size < 1:
        raise DatabaseError(
            f"bulk_insert: batch_size must be positive, got {batch_size}."
        )
    if not rows:
        _logger.debug("bulk_insert: no rows to insert into %s.%s", schema, table)
        return 0

    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    for name in columns:
        _validate_identifier(name, "column")

    from rey_lib.db._sqlalchemy import core_connection

    try:
        from sqlalchemy import column, insert, table as sql_table

        statement = insert(
            sql_table(table, *(column(name) for name in columns), schema=schema)
        )
        # Only the declared columns, in the declared order. A row carrying more
        # than it was asked for inserts what it was asked for.
        parameters = [{name: row[name] for name in columns} for row in rows]
        connection = core_connection(conn)
        for start in range(0, len(parameters), batch_size):
            connection.execute(statement, parameters[start:start + batch_size])
        return len(rows)
    except KeyError as exc:
        raise DatabaseError(
            f"bulk_insert: a row for {schema}.{table} is missing column {exc}."
        ) from exc
    except Exception as exc:
        raise DatabaseError(f"bulk_insert failed for {schema}.{table}: {exc}") from exc


def _validate_identifier(name: str, label: str) -> None:
    """Refuse anything that is not a plain identifier.

    Args:
        name: The candidate.
        label: What it names, for the message.

    Raises:
        DatabaseError: If it is not word characters alone. Identifiers are
            composed into SQL text rather than bound, so this is the boundary
            that keeps them safe -- values are always bound and never
            interpolated.
    """
    if not re.fullmatch(r"\w+", name):
        raise DatabaseError(
            f"Invalid PostgreSQL identifier for {label}: '{name}'. "
            "Only alphanumeric characters and underscores are permitted."
        )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def is_truncation_error(exc: Exception) -> bool:
    """
    Return True when exc is a PostgreSQL string-data-right-truncation error.

    Parameters
    ----------
    exc : Exception
        Exception from a prior execute call.

    Returns
    -------
    bool
        True if the exception is SQLSTATE 22001.
    """
    original = getattr(exc, "orig", exc)
    pgcode = getattr(original, "pgcode", None) or getattr(
        getattr(original, "pgerror", None), "pgcode", None
    )
    return pgcode == _TRUNCATION_SQLSTATE


# ---------------------------------------------------------------------------
# DDL exporter provider interface
# ---------------------------------------------------------------------------


def get_current_database(conn: Any) -> str:
    """Return the connected PostgreSQL database name."""
    from rey_lib.db._sqlalchemy import core_connection
    from sqlalchemy import text

    value = core_connection(conn).execute(text("SELECT current_database()")).scalar()
    return str(value) if value else "postgres"


def list_database_objects(conn: Any, database: str | None = None) -> list[dict[str, Any]]:
    """Return PostgreSQL objects and dependencies for DDL export.

    Tables are enumerated from the catalog (``pg_class``). Their supporting
    objects — indexes and constraints — are then discovered from the catalog
    for each listed table, so a supporting object can only ever depend on a
    table that was itself listed. Other primary objects (schemas, types,
    sequences, views, functions, procedures, triggers) are listed separately;
    those requiring an oid for DDL generation are read from the catalog.
    """
    db_name = database or get_current_database(conn)
    objects: list[dict[str, Any]] = _list_primary_objects(conn, db_name)
    tables = _list_tables(conn, db_name)
    objects.extend(tables)
    for table in tables:
        schema = str(table["schema"])
        name = str(table["name"])
        objects.extend(_list_table_indexes(conn, db_name, schema, name))
        objects.extend(_list_table_constraints(conn, db_name, schema, name))
    return objects


def _list_tables(conn: Any, db_name: str) -> list[dict[str, Any]]:
    """Return base tables from the catalog, keyed for DDL export.

    Read from ``pg_class`` rather than ``information_schema.tables`` so tables
    are listed regardless of the connecting role's privileges; the standard
    view is privilege-filtered and hides tables the role does not own or hold
    a grant on.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
                AND NOT c.relispartition
                AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                AND n.nspname NOT LIKE 'pg_toast%'
                AND n.nspname NOT LIKE 'pg_temp%'
            ORDER BY n.nspname, c.relname
            """
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    return [
        {
            "database": db_name,
            "object_type": "tables",
            "schema": str(schema_name),
            "name": str(table_name),
            "object_oid": None,
            "dependencies": [],
        }
        for schema_name, table_name in rows
    ]


def _list_table_indexes(
    conn: Any, db_name: str, schema: str, table: str
) -> list[dict[str, Any]]:
    """Return one table's indexes from the catalog, each depending on the table."""
    dependency = {"object_type": "tables", "schema": schema, "name": table}
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT i.schemaname, i.indexname, c.oid
            FROM pg_indexes i
            JOIN pg_class c ON c.relname = i.indexname
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = i.schemaname
            WHERE i.schemaname = %s AND i.tablename = %s
            ORDER BY i.indexname
            """,
            [schema, table],
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    return [
        {
            "database": db_name,
            "object_type": "indexes",
            "schema": str(schema_name),
            "name": str(index_name),
            "object_oid": int(index_oid) if index_oid is not None else None,
            "dependencies": [dict(dependency)],
        }
        for schema_name, index_name, index_oid in rows
    ]


def _list_table_constraints(
    conn: Any, db_name: str, schema: str, table: str
) -> list[dict[str, Any]]:
    """Return one table's constraints from the catalog, each depending on the table."""
    dependency = {"object_type": "tables", "schema": schema, "name": table}
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT n.nspname, con.conname
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
            ORDER BY con.conname
            """,
            [schema, table],
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    return [
        {
            "database": db_name,
            "object_type": "constraints",
            "schema": str(schema_name),
            "name": str(constraint_name),
            "object_oid": None,
            "dependencies": [dict(dependency)],
        }
        for schema_name, constraint_name in rows
    ]


def _list_primary_objects(conn: Any, db_name: str) -> list[dict[str, Any]]:
    """Return non-table primary objects with discoverable dependencies.

    Covers schemas, types, sequences, views, functions, procedures, and
    triggers. Objects whose DDL is generated from a catalog oid (functions,
    procedures, triggers) and enum/domain types (which have no
    information_schema view) are read from the catalog.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            WITH base AS (
                SELECT 'schemas'::text AS object_type, n.nspname AS schema_name, n.nspname AS object_name, NULL::oid AS object_oid
                FROM pg_namespace n
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                    AND n.nspname NOT LIKE 'pg_toast%'
                    AND n.nspname NOT LIKE 'pg_temp%'
                UNION ALL
                SELECT 'types'::text, n.nspname, t.typname, t.oid
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                    AND t.typtype IN ('e', 'd')
                UNION ALL
                SELECT 'sequences'::text, s.sequence_schema, s.sequence_schema || '.' || s.sequence_name, NULL::oid
                FROM information_schema.sequences s
                WHERE s.sequence_schema NOT IN ('pg_catalog', 'information_schema')
                UNION ALL
                SELECT 'views'::text, v.table_schema, v.table_name, c.oid
                FROM information_schema.views v
                JOIN pg_class c ON c.relname = v.table_name
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = v.table_schema
                WHERE v.table_schema NOT IN ('pg_catalog', 'information_schema')
                UNION ALL
                SELECT
                    CASE WHEN p.prokind = 'p' THEN 'procedures' ELSE 'functions' END AS object_type,
                    n.nspname,
                    p.proname AS object_name,
                    p.oid
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                    AND p.prokind IN ('f', 'p')
                UNION ALL
                SELECT DISTINCT 'triggers'::text, t.trigger_schema, t.trigger_name, tr.oid
                FROM information_schema.triggers t
                JOIN pg_trigger tr ON tr.tgname = t.trigger_name
                JOIN pg_class c ON c.oid = tr.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = t.event_object_schema
                WHERE t.trigger_schema NOT IN ('pg_catalog', 'information_schema')
                    AND NOT tr.tgisinternal
            )
            SELECT object_type, schema_name, object_name, object_oid
            FROM base
            ORDER BY object_type, schema_name, object_name
            """
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    objects: list[dict[str, Any]] = []
    for object_type, schema_name, object_name, object_oid in rows:
        oid = int(object_oid) if object_oid is not None else None
        dependencies = _postgres_dependencies(
            conn,
            object_type=str(object_type),
            schema=str(schema_name),
            name=str(object_name),
            object_oid=oid,
        )
        objects.append(
            {
                "database": db_name,
                "object_type": str(object_type),
                "schema": str(schema_name),
                "name": str(object_name),
                "object_oid": oid,
                "dependencies": dependencies,
            }
        )
    return objects


_ROUTINE_PROKIND: dict[str, str] = {"procedure": "p", "function": "f"}


def list_routines(
    conn: Any,
    catalog: str,
    schema: str | None,
    kind: str,
) -> list[dict[str, str]]:
    """Return one routine kind from the catalog, with its identity arguments.

    Read from ``pg_proc`` so overloaded routines are listed once each, and
    carry ``pg_get_function_identity_arguments`` as the signature. That is the
    argument list PostgreSQL itself uses to name one routine among its
    overloads, so a routine is addressable later without an oid.
    """
    prokind = _ROUTINE_PROKIND.get(kind)
    if prokind is None:
        raise DatabaseError(f"postgres_utils: unsupported routine kind '{kind}'.")

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT n.nspname,
                   p.proname,
                   pg_get_function_identity_arguments(p.oid),
                   -- The qualified name as PostgreSQL itself writes it, so a
                   -- mixed-case or otherwise quoted name is quoted correctly
                   -- without anything here deciding when quoting is needed.
                   format('%%I.%%I', n.nspname, p.proname),
                   p.proretset,
                   -- The two ways this dialect writes an argument list, and
                   -- the two facts that decide between them. The query knows
                   -- the catalog; the choice is made where it can be tested.
                   call.positional,
                   call.named,
                   call.all_named,
                   call.has_variadic
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            LEFT JOIN LATERAL (
                SELECT
                    bool_or(a.amode = 'v') AS has_variadic,
                    bool_and(a.aname IS NOT NULL AND a.aname <> '') AS all_named,
                    -- Cast per argument either way: the type is what names one
                    -- overload rather than resolving to whichever shares the
                    -- name, and what stops a call rendering as a zero-argument
                    -- namesake.
                    string_agg('NULL::' || format_type(a.atype, NULL),
                               ', ' ORDER BY a.ord) AS positional,
                    string_agg(quote_ident(COALESCE(a.aname, ''))
                               || ' => NULL::' || format_type(a.atype, NULL),
                               ', ' ORDER BY a.ord) AS named
                FROM (
                    -- proargmodes indexes proallargtypes, not proargtypes, and
                    -- both are null exactly when every argument is a plain IN.
                    -- Coalescing both is what lets a routine with an INOUT
                    -- output -- the ordinary shape here -- render at all.
                    SELECT t.ord,
                           t.oid AS atype,
                           COALESCE((p.proargmodes)[t.ord], 'i') AS amode,
                           (p.proargnames)[t.ord] AS aname
                    FROM unnest(COALESCE(p.proallargtypes, p.proargtypes::oid[]))
                         WITH ORDINALITY AS t(oid, ord)
                ) a
                -- What a call carries. A function's OUT and TABLE arguments
                -- are its result, not its call. A procedure's OUT arguments
                -- are part of its call and are written NULL, which is
                -- accepted because they are not evaluated.
                WHERE a.amode <> 't'
                    AND (p.prokind = 'p' OR a.amode <> 'o')
            ) call ON true
            WHERE p.prokind = %s
                AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                AND (%s::text IS NULL OR n.nspname = %s::text)
            ORDER BY n.nspname, p.proname
            """,
            [prokind, schema, schema],
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    listed: list[dict[str, str]] = []
    for (
        schema_name, routine_name, signature, qualified, returns_set,
        positional, named, all_named, has_variadic,
    ) in rows:
        routine = {
            "schema": str(schema_name),
            "name": str(routine_name),
            "signature": str(signature or ""),
        }
        rendered = _routine_invocation(
            kind=kind,
            qualified=str(qualified),
            returns_set=bool(returns_set),
            positional=positional,
            named=named,
            all_named=all_named,
            has_variadic=has_variadic,
        )
        if rendered:
            routine["invocation"] = rendered
        listed.append(routine)
    return listed


def _routine_invocation(
    *,
    kind: str,
    qualified: str,
    returns_set: bool,
    positional: str | None,
    named: str | None,
    all_named: bool | None,
    has_variadic: bool | None,
) -> str:
    """One routine's call, or nothing for the one shape left unrendered.

    Rendered from the catalog rather than from the signature string. The
    signature is one piece of text -- ``p_amount numeric(10,2), p_tags text[]``
    -- and recovering arguments from it means splitting on commas that also
    appear inside types. The arguments arrive already separate, so nothing here
    splits anything.

    **Named notation where every carried argument has a name.** That is how
    calls are already written in this module: ``render_and_execute`` states the
    reason, and it holds for a template just as much -- a name says what to
    fill in, and order stops being load-bearing. Positional is the fallback for
    a routine whose arguments are unnamed, and both cast every argument, which
    is what names one overload rather than whichever shares the name and what
    stops a call rendering as a zero-argument namesake.

    **Nothing is rendered for a routine with a VARIADIC argument**: named
    notation does not accept one, and its keyword form is a second convention
    rather than a variation of this one. That routine carries no statement and
    keeps the refusal, which is an ordinary answer and not a gap.

    The shape comes from the catalog because a listed routine has no
    declaration to take it from: a procedure is CALLed, a set-returning function
    is selected from, and a scalar function is selected.

    Args:
        kind: The routine kind being listed.
        qualified: The routine's name, already quoted as this dialect quotes it.
        returns_set: Whether the routine returns a set.
        positional: The argument list written positionally.
        named: The same arguments written by name.
        all_named: Whether every carried argument has a name. Null for a
            routine carrying none, where the two forms are the same empty list.
        has_variadic: Whether any carried argument is VARIADIC. Null likewise.

    Returns:
        The statement that calls the routine, or empty where none is rendered.
    """
    if has_variadic:
        return ""
    if _ROUTINE_PROKIND[kind] == "p":
        shape = InvocationShape.PROCEDURE
    else:
        shape = (
            InvocationShape.ROW_FUNCTION if returns_set
            else InvocationShape.SCALAR_FUNCTION
        )
    # ``all_named`` is null for a routine with no arguments at all, which is
    # not an unnamed one: both forms are the same empty list, so either serves.
    arguments = positional if all_named is False else named
    return _STATEMENT[shape].format(
        routine=qualified, arguments=str(arguments or ""),
    ) + ";"


def get_routine_definition(
    conn: Any,
    catalog: str,
    schema: str,
    name: str,
    signature: str,
    kind: str,
) -> str:
    """Return one routine's definition, resolved by schema, name and signature.

    The oid is resolved here rather than carried by the caller, so an overloaded
    routine is addressed by the same provider-neutral identity the listing
    returned and no provider handle leaves this module.
    """
    prokind = _ROUTINE_PROKIND.get(kind)
    if prokind is None:
        raise DatabaseError(f"postgres_utils: unsupported routine kind '{kind}'.")

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s
                AND p.proname = %s
                AND pg_get_function_identity_arguments(p.oid) = %s
                AND p.prokind = %s
            """,
            [schema, name, signature, prokind],
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    if not rows:
        raise DatabaseError(
            f"postgres_utils: {kind} not found: {schema}.{name}({signature})"
        )
    if len(rows) > 1:
        raise DatabaseError(
            f"postgres_utils: {kind} identity is ambiguous: "
            f"{schema}.{name}({signature})"
        )
    return str(rows[0][0]).rstrip() + ";"


def get_object_ddl(conn: Any, obj: dict[str, Any]) -> str:
    """Return provider-native PostgreSQL DDL for one object."""
    object_type = str(obj["object_type"])
    schema = str(obj["schema"])
    name = str(obj["name"])
    oid = obj.get("object_oid")
    cursor = conn.cursor()
    try:
        if object_type == "schemas":
            return f'CREATE SCHEMA IF NOT EXISTS "{schema}";'

        if object_type == "types":
            return _postgres_type_ddl(conn, schema, name)

        if object_type == "tables":
            return _postgres_table_ddl(conn, schema, name)

        if object_type == "views":
            cursor.execute(
                "SELECT pg_get_viewdef(c.oid, true) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                [schema, name],
            )
            row = cursor.fetchone()
            view_sql = row[0] if row else ""
            return f'CREATE OR REPLACE VIEW "{schema}"."{name}" AS\n{view_sql};'

        if object_type == "sequences":
            seq_name = name.split(".", 1)[-1]
            cursor.execute(
                """
                SELECT start_value, increment_by, minimum_value, maximum_value, cycle_option
                FROM information_schema.sequences
                WHERE sequence_schema = %s AND sequence_name = %s
                """,
                [schema, seq_name],
            )
            row = cursor.fetchone()
            if not row:
                raise DatabaseError(f"postgres_utils: sequence not found: {schema}.{seq_name}")
            return (
                f'CREATE SEQUENCE IF NOT EXISTS "{schema}"."{seq_name}" '\
                f'START WITH {row[0]} INCREMENT BY {row[1]} MINVALUE {row[2]} MAXVALUE {row[3]} '\
                f"{'CYCLE' if str(row[4]).upper() == 'YES' else 'NO CYCLE'};"
            )

        if object_type in ("functions", "procedures"):
            if oid is None:
                raise DatabaseError(f"postgres_utils: routine oid missing for {schema}.{name}")
            cursor.execute("SELECT pg_get_functiondef(%s::oid)", [oid])
            row = cursor.fetchone()
            if not row:
                raise DatabaseError(f"postgres_utils: routine not found for oid {oid}")
            return str(row[0]).rstrip() + ";"

        if object_type == "triggers":
            if oid is None:
                raise DatabaseError(f"postgres_utils: trigger oid missing for {schema}.{name}")
            cursor.execute("SELECT pg_get_triggerdef(%s::oid, true)", [oid])
            row = cursor.fetchone()
            if not row:
                raise DatabaseError(f"postgres_utils: trigger not found for oid {oid}")
            return str(row[0]).rstrip() + ";"

        if object_type == "indexes":
            if oid is None:
                raise DatabaseError(f"postgres_utils: index oid missing for {schema}.{name}")
            cursor.execute("SELECT pg_get_indexdef(%s::oid)", [oid])
            row = cursor.fetchone()
            if not row:
                raise DatabaseError(f"postgres_utils: index not found for oid {oid}")
            return str(row[0]).rstrip() + ";"

        if object_type == "constraints":
            cursor.execute(
                """
                SELECT ns.nspname, cls.relname, con.conname, pg_get_constraintdef(con.oid, true)
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE con.conname = %s AND con.connamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = %s LIMIT 1
                )
                LIMIT 1
                """,
                [name, schema],
            )
            row = cursor.fetchone()
            if not row:
                raise DatabaseError(f"postgres_utils: constraint not found: {schema}.{name}")
            return f'ALTER TABLE "{row[0]}"."{row[1]}" ADD CONSTRAINT "{row[2]}" {row[3]};'

        raise DatabaseError(f"postgres_utils: unsupported export object type '{object_type}'.")
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _serialise_jsonb(params: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of params with dict/list values serialised to JSON strings.

    A jsonb parameter is handed over as JSON text rather than as the Python
    value, so the driver never adapts it. That matters under Psycopg 3, which
    would otherwise send a list as a PostgreSQL *array* and refuse a dict
    outright; serialising here means neither case can arise.
    """
    result: dict[str, Any] = {}
    for k, v in params.items():
        result[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    return result


def inspect_database_references(
    conn: Any,
    schema: str | None = None,
) -> dict[str, Any]:
    """Everything the database index needs about one PostgreSQL database.

    One capability rather than a menu of getters. A caller receives normalized
    objects, members and reference *observations*; it never learns whether a
    fact came from the catalog, from ``pg_depend``, from ``pg_trigger`` or from
    a parse tree. That is what keeps the index provider-independent.

    Three rules govern the composition, and all three exist to stop catalog
    metadata overstating or understating what is known:

    **The catalog never weakens an analyzer fact.** ``pg_depend`` says a routine
    *depends on* a relation; it cannot say whether the routine reads or writes
    it. Where body analysis established ``write``, the catalog dependency for
    that same pair becomes corroborating evidence on the typed edge -- a second
    ``fact_source`` -- never a competing generic ``reference`` edge and never a
    downgrade.

    **Status is owned by analysis, not by resolution.** A routine analyzed
    completely stays ``complete`` even when what it names lies outside the
    indexed scope. Unresolved evidence qualifies a *negative query*; it does not
    retroactively make the analysis incomplete.

    **Catalog certainty does not override snapshot scope.** ``tgfoid`` may name
    the invoked function exactly, but if that function was not enumerated here
    there is no node to point at: the observation is recorded ``unresolved``
    with the authoritative identity preserved, and no edge is claimed.

    Args:
        conn: An open PostgreSQL connection.
        schema: One schema to confine the scan to, or ``None`` for all
            non-system schemas.

    Returns:
        ``{"objects": [...], "members": [...], "observations": [...]}`` in the
        database index's own vocabulary.

    Raises:
        DatabaseError: When a definition cannot be retrieved for an object the
            provider claims to support. That is an infrastructure failure and
            must stop the run -- publishing it as ``unsupported`` would launder
            a permissions or connection defect into a capability statement.
    """
    from rey_lib.db.postgres_references import ANALYZABLE_LANGUAGES, analyse_routine

    objects: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    #: Where body analysis established a typed relationship, so a later catalog
    #: dependency for the same pair corroborates rather than competes.
    typed: dict[tuple[str, str, str, str], str] = {}

    routines = _reference_routines(conn, schema)
    relations = _reference_relations(conn, schema)
    triggers = _reference_triggers(conn, schema)
    # (schema, name) -> every object of that name. A list rather than a set
    # because identity includes object_type and signature: a body names
    # `control.f_x` and the snapshot must supply the rest, or say it cannot.
    in_scope: dict[tuple[str, str], list[dict[str, str]]] = {}
    for candidate in (*relations, *routines, *triggers):
        in_scope.setdefault(
            (candidate["schema"], candidate["name"]), []
        ).append({
            "schema": candidate["schema"], "name": candidate["name"],
            "object_type": candidate["object_type"],
            "signature": candidate.get("signature", ""),
        })

    for trigger in triggers:
        objects.append({
            "schema_name": trigger["schema"], "object_name": trigger["name"],
            "object_type": "trigger", "signature": "",
            "provider_object_id": str(trigger["oid"]),
            "definition_hash": trigger["definition_hash"],
            # Both of a trigger's relationships come from the catalog, so
            # analysis is complete by construction. It is enumerated as an
            # object because its edges originate from it -- an observation
            # whose source is not in the snapshot has nowhere to hang.
            "reference_analysis_status": "complete",
        })

    for rel in relations:
        objects.append({
            "schema_name": rel["schema"], "object_name": rel["name"],
            "object_type": rel["object_type"], "signature": "",
            "provider_object_id": str(rel["oid"]),
            "definition_hash": rel["definition_hash"],
            # A relation has no body to analyze, and its outgoing kinds come
            # from the catalog, so analysis is complete by construction.
            "reference_analysis_status": "complete",
        })
        for column in rel["columns"]:
            members.append({
                "schema_name": rel["schema"], "object_name": rel["name"],
                "object_type": rel["object_type"], "signature": "",
                "member_kind": "column", "member_name": column["name"],
                "ordinal": column["ordinal"], "data_type": column["data_type"],
                "member_mode": "", "provider_member_id": "",
            })

    for routine in routines:
        language = str(routine["language"]).lower()
        analysis = (
            analyse_routine(routine["definition"], language)
            if language in ANALYZABLE_LANGUAGES
            else _unsupported()
        )
        objects.append({
            "schema_name": routine["schema"], "object_name": routine["name"],
            "object_type": routine["object_type"], "signature": routine["signature"],
            "provider_object_id": str(routine["oid"]),
            "definition_hash": routine["definition_hash"],
            "reference_analysis_status": analysis.status,
        })
        origin = (routine["schema"], routine["name"], routine["object_type"],
                  routine["signature"])
        for kind, names in (("read", analysis.reads), ("write", analysis.writes),
                            ("call", analysis.calls)):
            for written in sorted(names):
                target = _target(written, in_scope, kind)
                if target is None:
                    continue
                typed[(origin[0], origin[1], target["schema"], target["name"])] = kind
                observations.append(_observation(
                    origin, target, kind, routine["definition_hash"],
                    fact_source="parsed", evidence="routine_definition",
                    observed_target=written,
                ))
        for gap in analysis.gaps:
            observations.append(_observation(
                origin, None, "call", routine["definition_hash"],
                fact_source="parsed", evidence="analysis_gap",
                observed_target=gap,
            ))

    observations.extend(
        _catalog_observations(conn, schema, in_scope, typed, relations,
                              routines, triggers)
    )
    return {"objects": objects, "members": members, "observations": observations}


def _unsupported() -> Any:
    from rey_lib.db.postgres_references import RoutineAnalysis

    return RoutineAnalysis(status="unsupported")


_UNRESOLVED = {"schema": "", "name": "", "object_type": "", "signature": "",
               "resolution": "unresolved"}


def _target(
    written: str, in_scope: dict[tuple[str, str], list[dict[str, str]]], kind: str,
) -> dict[str, Any]:
    """One name as written, resolved against this scan's own object set.

    **An unqualified name never resolves.** "Unique in what we happened to
    index" is not proof that PostgreSQL bound the reference there -- resolution
    is ``search_path`` dependent, and this scan has no authoritative binding
    information. It is recorded unresolved with the name preserved.

    A qualified name resolves only when it names exactly one object. Two
    objects of that name -- overloaded routines -- is ``ambiguous``, not a
    coin toss: binding to the wrong one would be worse than admitting the
    uncertainty, and an ambiguous observation still records what was seen.

    Resolution also supplies the ``object_type`` and ``signature`` the body
    never stated. Identity includes both, so an observation carrying only a
    name cannot be joined to anything.
    """
    if "." not in written:
        return dict(_UNRESOLVED)
    schema, _, name = written.rpartition(".")
    candidates = in_scope.get((schema, name), ())
    if len(candidates) == 1:
        return {**candidates[0], "resolution": "exact"}
    if len(candidates) > 1:
        return {**dict(_UNRESOLVED), "resolution": "ambiguous"}
    return dict(_UNRESOLVED)


def _observation(
    origin: tuple[str, str, str, str],
    target: dict[str, Any] | None,
    kind: str,
    definition_hash: str,
    *,
    fact_source: str,
    evidence: str,
    observed_target: str,
    statement_ordinal: int = 0,
) -> dict[str, Any]:
    """One staged observation, in the index's own vocabulary."""
    resolved = bool(target and target["resolution"] == "exact")
    return {
        "from_schema_name": origin[0], "from_object_name": origin[1],
        "from_object_type": origin[2], "from_signature": origin[3],
        "from_member_kind": "", "from_member_name": "", "from_ordinal": 0,
        "to_schema_name": target["schema"] if resolved else "",
        "to_object_name": target["name"] if resolved else "",
        "to_object_type": target["object_type"] if resolved else "",
        "to_signature": target["signature"] if resolved else "",
        "to_member_kind": "", "to_member_name": "", "to_ordinal": 0,
        "reference_kind": kind,
        "observed_target": observed_target,
        "resolution": (
            target["resolution"] if target else "unresolved"
        ) if not resolved else "exact",
        "observed_definition_hash": definition_hash,
        "statement_ordinal": statement_ordinal,
        "source_line": 0, "source_column": 0,
        "fact_source": fact_source, "evidence": evidence,
    }

#: Relation kinds the index admits as objects. Indexes and constraints are
#: deliberately absent: a foreign key is already an edge between members, an
#: index answers none of the questions the graph exists for, and admitting them
#: would add nodes nothing traverses.
_RELKIND_TO_OBJECT_TYPE = {
    "r": "table", "p": "table", "v": "view", "m": "materialized_view",
    "S": "sequence",
}
_PROKIND_TO_OBJECT_TYPE = {"f": "function", "p": "procedure", "a": "function",
                           "w": "function"}


def _reference_relations(conn: Any, schema: str | None) -> list[dict[str, Any]]:
    """Relations in scope, with their columns and a definition hash.

    A table has no definition, so its hash is empty -- the freshness anchor for
    a relation is its catalog identity, and inventing a hash would imply an
    analysis that never happened.
    """
    import hashlib

    rows = _reference_rows(conn, """
        SELECT c.oid, n.nspname, c.relname, c.relkind,
               CASE WHEN c.relkind IN ('v','m')
                    THEN pg_get_viewdef(c.oid, true) ELSE '' END AS definition
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = ANY(%s)
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_%%'
          AND (%s::text IS NULL OR n.nspname = %s::text)
        ORDER BY n.nspname, c.relname
    """, [list(_RELKIND_TO_OBJECT_TYPE), schema, schema])

    relations = []
    for oid, nsp, name, relkind, definition in rows:
        columns = _reference_rows(conn, """
            SELECT a.attname, a.attnum, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """, [oid])
        relations.append({
            "oid": oid, "schema": nsp, "name": name,
            "object_type": _RELKIND_TO_OBJECT_TYPE[relkind],
            "definition_hash": (
                hashlib.sha256(definition.encode("utf-8")).hexdigest()
                if definition else ""
            ),
            "columns": [
                {"name": c[0], "ordinal": int(c[1]), "data_type": str(c[2])}
                for c in columns
            ],
        })
    return relations


def _reference_routines(conn: Any, schema: str | None) -> list[dict[str, Any]]:
    """Routines in scope, with the whole definition their analysis needs.

    The definition is retrieved, not the body: PL/pgSQL is parsed against its
    declared return type, so a body lifted out and re-wrapped makes an ordinary
    ``RETURN expr`` unparseable.

    Raises:
        DatabaseError: when a definition cannot be retrieved. The provider
            supports these routines, so a failure here is infrastructure, not a
            capability limit, and it must stop the run rather than be published
            as ``unsupported``.
    """
    import hashlib

    rows = _reference_rows(conn, """
        SELECT p.oid, n.nspname, p.proname, p.prokind, l.lanname,
               pg_get_function_identity_arguments(p.oid)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_%%'
          AND (%s::text IS NULL OR n.nspname = %s::text)
        ORDER BY n.nspname, p.proname
    """, [schema, schema])

    routines = []
    for oid, nsp, name, prokind, language, signature in rows:
        try:
            definition = _reference_rows(
                conn, "SELECT pg_get_functiondef(%s)", [oid])[0][0]
        except Exception as exc:
            raise DatabaseError(
                f"postgres_utils: the definition of {nsp}.{name} could not be "
                f"retrieved, so this scan cannot state what it references: "
                f"{exc}"
            ) from exc
        routines.append({
            "oid": oid, "schema": nsp, "name": name,
            "object_type": _PROKIND_TO_OBJECT_TYPE.get(prokind, "function"),
            "signature": str(signature or ""),
            "language": language, "definition": definition,
            "definition_hash": hashlib.sha256(
                definition.encode("utf-8")).hexdigest(),
        })
    return routines


def _reference_triggers(conn: Any, schema: str | None) -> list[dict[str, Any]]:
    """Triggers in scope.

    Enumerated separately because a trigger is not a ``pg_class`` row, and
    omitted for a long time without anyone noticing: this estate has none, so
    the gap was latent. It matters because a trigger's two relationships
    ORIGINATE from it, and an observation whose source object is absent from
    the snapshot has nothing to attach to.
    """
    import hashlib

    rows = _reference_rows(conn, """
        SELECT t.oid, n.nspname, t.tgname, pg_get_triggerdef(t.oid)
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND (%s::text IS NULL OR n.nspname = %s::text)
        ORDER BY n.nspname, t.tgname
    """, [schema, schema])
    return [
        {
            "oid": oid, "schema": nsp, "name": name, "object_type": "trigger",
            "definition_hash": hashlib.sha256(
                str(definition or "").encode("utf-8")).hexdigest(),
        }
        for oid, nsp, name, definition in rows
    ]


def _catalog_observations(
    conn: Any,
    schema: str | None,
    in_scope: set,
    typed: dict,
    relations: list[dict[str, Any]],
    routines: list[dict[str, Any]],
    triggers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """What the catalog knows, without overstating it.

    A view dependency and a foreign key are authoritative. A routine's
    ``pg_depend`` row on a relation is **not** typed: it says the definition
    depends on the relation and nothing about access direction, so it publishes
    as ``reference`` -- unless body analysis already established a typed
    relationship for that pair, in which case it corroborates that edge instead
    of competing with a weaker one.
    """
    out: list[dict[str, Any]] = []
    by_oid = {r["oid"]: r for r in relations}
    by_oid.update({r["oid"]: r for r in routines})

    def origin_of(row: dict[str, Any]) -> tuple[str, str, str, str]:
        return (row["schema"], row["name"], row["object_type"],
                row.get("signature", ""))

    # Views: pg_rewrite/pg_depend is authoritative for what a view references.
    for oid, dep_oid in _reference_rows(conn, """
        SELECT DISTINCT v.oid, t.oid
        FROM pg_class v
        JOIN pg_namespace n ON n.oid = v.relnamespace
        JOIN pg_rewrite rw ON rw.ev_class = v.oid
        JOIN pg_depend d ON d.objid = rw.oid
        JOIN pg_class t ON t.oid = d.refobjid
        WHERE v.relkind IN ('v','m') AND t.relkind IN ('r','p','v','m')
          AND v.oid <> t.oid
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND (%s::text IS NULL OR n.nspname = %s::text)
    """, [schema, schema]):
        view, target = by_oid.get(oid), by_oid.get(dep_oid)
        if view is None:
            continue
        out.append(_catalog_edge(origin_of(view), target, "reference",
                                 view["definition_hash"], "view_definition"))

    # Triggers: BOTH relationships. tgrelid is the relation it is attached to;
    # tgfoid is the function it invokes -- the edge that makes
    # "table write -> trigger -> function" reachable at all.
    known_triggers = {(t["schema"], t["name"]) for t in (triggers or ())}
    for tgname, nsp, rel_oid, fn_oid in _reference_rows(conn, """
        SELECT t.tgname, n.nspname, t.tgrelid, t.tgfoid
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND (%s::text IS NULL OR n.nspname = %s::text)
    """, [schema, schema]):
        if (nsp, tgname) not in known_triggers:
            # Its source object is not in this snapshot, so the observation has
            # nowhere to originate. Emitting it would be silently discarded by
            # publication's join, which is the failure mode this whole design
            # refuses.
            continue
        origin = (nsp, tgname, "trigger", "")
        out.append(_catalog_edge(origin, by_oid.get(rel_oid), "trigger", "",
                                 "pg_trigger"))
        out.append(_catalog_edge(origin, by_oid.get(fn_oid), "call", "",
                                 "pg_trigger"))

    # Foreign keys, member to member.
    for row in _reference_rows(conn, """
        SELECT sn.nspname, st.relname, sa.attname,  -- noqa: fk source
               tn.nspname, tt.relname, ta.attname
        FROM pg_constraint k
        JOIN pg_class st ON st.oid = k.conrelid
        JOIN pg_namespace sn ON sn.oid = st.relnamespace
        JOIN pg_class tt ON tt.oid = k.confrelid
        JOIN pg_namespace tn ON tn.oid = tt.relnamespace
        JOIN unnest(k.conkey) WITH ORDINALITY AS sk(attnum, ord) ON true
        JOIN unnest(k.confkey) WITH ORDINALITY AS tk(attnum, ord)
             ON tk.ord = sk.ord
        JOIN pg_attribute sa ON sa.attrelid = st.oid AND sa.attnum = sk.attnum
        JOIN pg_attribute ta ON ta.attrelid = tt.oid AND ta.attnum = tk.attnum
        WHERE k.contype = 'f'
          AND sn.nspname NOT IN ('pg_catalog','information_schema')
          AND (%s::text IS NULL OR sn.nspname = %s::text)
    """, [schema, schema]):
        s_nsp, s_rel, s_col, t_nsp, t_rel, t_col = row
        resolved = (t_nsp, t_rel) in in_scope
        out.append({
            "from_schema_name": s_nsp, "from_object_name": s_rel,
            "from_object_type": "table", "from_signature": "",
            "from_member_kind": "column", "from_member_name": s_col,
            "from_ordinal": 0,
            "to_schema_name": t_nsp if resolved else "",
            "to_object_name": t_rel if resolved else "",
            "to_object_type": "table" if resolved else "",
            "to_signature": "",
            "to_member_kind": "column" if resolved else "",
            "to_member_name": t_col if resolved else "", "to_ordinal": 0,
            "reference_kind": "foreign_key",
            "observed_target": f"{t_nsp}.{t_rel}.{t_col}",
            "resolution": "exact" if resolved else "unresolved",
            "observed_definition_hash": "", "statement_ordinal": 0,
            "source_line": 0, "source_column": 0,
            "fact_source": "catalog", "evidence": "foreign_key",
        })
    return out


def _catalog_edge(
    origin: tuple[str, str, str, str],
    target: dict[str, Any] | None,
    kind: str,
    definition_hash: str,
    evidence: str,
) -> dict[str, Any]:
    """One catalog fact, unresolved where its target is out of scope."""
    resolved = target is not None
    return {
        "from_schema_name": origin[0], "from_object_name": origin[1],
        "from_object_type": origin[2], "from_signature": origin[3],
        "from_member_kind": "", "from_member_name": "", "from_ordinal": 0,
        "to_schema_name": target["schema"] if resolved else "",
        "to_object_name": target["name"] if resolved else "",
        "to_object_type": target["object_type"] if resolved else "",
        "to_signature": target.get("signature", "") if resolved else "",
        "to_member_kind": "", "to_member_name": "", "to_ordinal": 0,
        "reference_kind": kind,
        "observed_target": (
            f"{target['schema']}.{target['name']}" if resolved
            else "(outside indexed scope)"
        ),
        "resolution": "exact" if resolved else "unresolved",
        "observed_definition_hash": definition_hash,
        "statement_ordinal": 0, "source_line": 0, "source_column": 0,
        "fact_source": "catalog", "evidence": evidence,
    }


def _reference_rows(conn: Any, sql: str, params: list[Any]) -> list[tuple]:
    """One catalog query, through the connection the caller already owns."""
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _postgres_dependencies(
    conn: Any,
    *,
    object_type: str,
    schema: str,
    name: str,
    object_oid: int | None,
) -> list[dict[str, str]]:
    """Return discoverable dependencies for one PostgreSQL object."""
    deps: list[dict[str, str]] = []
    cursor = conn.cursor()
    try:
        if object_type == "views":
            cursor.execute(
                """
                SELECT DISTINCT 'tables'::text, sn.nspname, sc.relname
                FROM pg_class vc
                JOIN pg_namespace vn ON vn.oid = vc.relnamespace
                JOIN pg_rewrite rw ON rw.ev_class = vc.oid
                JOIN pg_depend dep ON dep.objid = rw.oid
                JOIN pg_class sc ON sc.oid = dep.refobjid
                JOIN pg_namespace sn ON sn.oid = sc.relnamespace
                WHERE vn.nspname = %s AND vc.relname = %s
                    AND sc.relkind IN ('r', 'v', 'm')
                    AND sn.nspname NOT IN ('pg_catalog', 'information_schema')
                """,
                [schema, name],
            )
            deps.extend(
                {"object_type": str(r[0]), "schema": str(r[1]), "name": str(r[2])}
                for r in cursor.fetchall()
            )

        elif object_type == "triggers":
            cursor.execute(
                """
                SELECT DISTINCT 'tables'::text, ns.nspname, cls.relname
                FROM pg_trigger tr
                JOIN pg_class cls ON cls.oid = tr.tgrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE tr.tgname = %s AND ns.nspname = %s
                """,
                [name, schema],
            )
            deps.extend(
                {"object_type": str(r[0]), "schema": str(r[1]), "name": str(r[2])}
                for r in cursor.fetchall()
            )

        elif object_type == "indexes":
            cursor.execute(
                """
                SELECT DISTINCT 'tables'::text, ns.nspname, tbl.relname
                FROM pg_class idx
                JOIN pg_index i ON i.indexrelid = idx.oid
                JOIN pg_class tbl ON tbl.oid = i.indrelid
                JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
                WHERE idx.relname = %s AND ns.nspname = %s
                """,
                [name, schema],
            )
            deps.extend(
                {"object_type": str(r[0]), "schema": str(r[1]), "name": str(r[2])}
                for r in cursor.fetchall()
            )

        elif object_type == "constraints":
            cursor.execute(
                """
                SELECT DISTINCT 'tables'::text, ns.nspname, cls.relname
                FROM information_schema.table_constraints tc
                JOIN pg_constraint c ON c.conname = tc.constraint_name
                JOIN pg_class cls ON cls.oid = c.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE tc.constraint_schema = %s AND tc.constraint_name = %s
                """,
                [schema, name],
            )
            deps.extend(
                {"object_type": str(r[0]), "schema": str(r[1]), "name": str(r[2])}
                for r in cursor.fetchall()
            )

        elif object_type in ("functions", "procedures") and object_oid is not None:
            cursor.execute(
                """
                SELECT DISTINCT
                    CASE
                        WHEN ref_cls.relkind = 'r' THEN 'tables'
                        WHEN ref_cls.relkind IN ('v', 'm') THEN 'views'
                        ELSE 'functions'
                    END,
                    ref_ns.nspname,
                    COALESCE(ref_cls.relname, ref_proc.proname)
                FROM pg_depend dep
                LEFT JOIN pg_class ref_cls ON ref_cls.oid = dep.refobjid
                LEFT JOIN pg_proc ref_proc ON ref_proc.oid = dep.refobjid
                LEFT JOIN pg_namespace ref_ns ON ref_ns.oid = COALESCE(ref_cls.relnamespace, ref_proc.pronamespace)
                WHERE dep.objid = %s::oid
                    AND ref_ns.nspname IS NOT NULL
                    AND ref_ns.nspname NOT IN ('pg_catalog', 'information_schema')
                """,
                [object_oid],
            )
            deps.extend(
                {"object_type": str(r[0]), "schema": str(r[1]), "name": str(r[2])}
                for r in cursor.fetchall()
                if r[2]
            )

        unique = {
            (d["object_type"], d["schema"], d["name"]): d
            for d in deps
            if d.get("name")
        }
        return [unique[k] for k in sorted(unique)]
    finally:
        cursor.close()


def get_column_ddl(conn: Any, schema: str, table: str, column: str) -> str:
    """Return the definition of one column, as it appears in its table.

    A fragment and not a statement: a column has no standalone CREATE, and
    pretending otherwise would hand a caller SQL that does not run. What comes
    back is the same line ``_postgres_table_ddl`` writes for this column, read
    from the same catalog -- name, ``format_type`` of the resolved type, any
    default, and whether it is nullable.

    Returns an empty string when the column does not exist, which is what a
    caller asking about a dropped column should get rather than an error.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                a.attname,
                format_type(a.atttypid, a.atttypmod),
                a.attnotnull,
                pg_get_expr(ad.adbin, ad.adrelid)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s
                AND a.attnum > 0
                AND NOT a.attisdropped
            """,
            [schema, table, column],
        )
        row = cursor.fetchone()
    finally:
        cursor.close()

    if not row:
        return ""
    default = f" DEFAULT {row[3]}" if row[3] is not None else ""
    nullable = "NOT NULL" if bool(row[2]) else "NULL"
    return f'"{row[0]}" {row[1]}{default} {nullable}'


def _postgres_table_ddl(conn: Any, schema: str, table: str) -> str:
    """Build CREATE TABLE DDL from the catalog for base tables.

    Read column metadata from ``pg_attribute`` rather than
    ``information_schema.columns`` so DDL is generated regardless of the
    connecting role's privileges; the standard view is privilege-filtered.
    ``format_type`` yields the exact column type, including length and
    precision modifiers.
    """
    from rey_lib.db._sqlalchemy import inspect_schema, is_sqlalchemy_connection

    if is_sqlalchemy_connection(conn):
        metadata = inspect_schema(conn, schema)
        table_metadata = next(
            (item for item in metadata["tables"] if item["name"] == table), None
        )
        if table_metadata is None:
            raise DatabaseError(f"postgres_utils: table not found: {schema}.{table}")
        columns = []
        for column in table_metadata["columns"]:
            nullable = "NULL" if column["nullable"] else "NOT NULL"
            default = (
                f" DEFAULT {column['default']}"
                if column["default"] is not None
                else ""
            )
            col_name = str(column["name"]).replace('"', '""')
            columns.append(
                f'    "{col_name}" {column["type"]}{default} {nullable}'
            )
        return (
            f'CREATE TABLE "{schema}"."{table}" (\n'
            + ",\n".join(columns)
            + "\n);"
        )

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                a.attname,
                format_type(a.atttypid, a.atttypmod),
                a.attnotnull,
                pg_get_expr(ad.adbin, ad.adrelid)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            WHERE n.nspname = %s AND c.relname = %s
                AND a.attnum > 0
                AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            [schema, table],
        )
        rows = cursor.fetchall()
        if not rows:
            raise DatabaseError(f"postgres_utils: table not found: {schema}.{table}")

        columns: list[str] = []
        for row in rows:
            col_name = str(row[0])
            data_type = str(row[1])
            nullable = "NOT NULL" if bool(row[2]) else "NULL"
            default = f" DEFAULT {row[3]}" if row[3] is not None else ""
            columns.append(f'    "{col_name}" {data_type}{default} {nullable}')

        return (
            f'CREATE TABLE "{schema}"."{table}" (\n'
            + ",\n".join(columns)
            + "\n);"
        )
    finally:
        cursor.close()


def _postgres_type_ddl(conn: Any, schema: str, type_name: str) -> str:
    """Build CREATE TYPE DDL for enums/domains."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT t.typtype, format_type(t.typbasetype, t.typtypmod), t.typnotnull,
                   pg_get_expr(t.typdefaultbin, 0)
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = %s AND t.typname = %s
            """,
            [schema, type_name],
        )
        row = cursor.fetchone()
        if not row:
            raise DatabaseError(f"postgres_utils: type not found: {schema}.{type_name}")

        typtype = str(row[0])
        if typtype == "e":
            cursor.execute(
                """
                SELECT enumlabel
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = %s AND t.typname = %s
                ORDER BY enumsortorder
                """,
                [schema, type_name],
            )
            labels = ["'" + str(r[0]).replace("'", "''") + "'" for r in cursor.fetchall()]
            return f'CREATE TYPE "{schema}"."{type_name}" AS ENUM ({", ".join(labels)});'

        if typtype == "d":
            base_type = str(row[1])
            not_null = " NOT NULL" if bool(row[2]) else ""
            default_expr = f" DEFAULT {row[3]}" if row[3] is not None else ""
            return (
                f'CREATE DOMAIN "{schema}"."{type_name}" AS {base_type}'
                f"{default_expr}{not_null};"
            )

        return f"-- Unsupported PostgreSQL type kind '{typtype}' for {schema}.{type_name}."
    finally:
        cursor.close()
