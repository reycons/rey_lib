"""
PostgreSQL connection and execution layer.

Owns all PostgreSQL connections and control database calls. No raw psycopg2
calls are permitted outside this module.

Connection details are passed as a Namespace object resolved from ctx at
call time — this module has no knowledge of ctx structure or application
config layout. Passwords must be injected from .env before get_connection()
is called — they are never read from YAML.

psycopg2 is an optional dependency. Install with:
    pip install psycopg2-binary

Function calls (SELECT) and procedure calls (CALL) are kept separate to
match the PostgreSQL distinction between functions and procedures.

Public API
----------
get_connection(db_cfg)
    Return an open psycopg2 connection.
execute_function(conn, routine, named_params)
    Call a PostgreSQL function via SELECT and return the scalar result.
execute_procedure(conn, routine, named_params)
    Call a PostgreSQL procedure via CALL.
is_truncation_error(exc)
    Return True when exc is a PostgreSQL string truncation error.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = [
    "get_connection",
    "get_current_database",
    "list_database_objects",
    "get_object_ddl",
    "execute_function",
    "execute_procedure",
    "is_truncation_error",
]

_logger = get_logger(__name__)

# PostgreSQL error code for string-data-right-truncation.
_TRUNCATION_SQLSTATE = "22001"


def _psycopg2() -> Any:
    """Lazy-import psycopg2 with a clear install hint if absent."""
    try:
        import psycopg2  # noqa: PLC0415
        return psycopg2
    except ImportError as exc:
        raise ConfigError(
            "psycopg2 is required for PostgreSQL connections. "
            "Install it with: pip install psycopg2-binary"
        ) from exc


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection(db_cfg: Any) -> Any:
    """
    Open a psycopg2 connection from a connection config Namespace.

    Parameters
    ----------
    db_cfg : Any
        Connection config Namespace. Required fields: host, database, username.
        Optional fields: port (default 5432), password.

    Returns
    -------
    psycopg2.connection
        Open database connection with autocommit disabled.

    Raises
    ------
    ConfigError
        If psycopg2 is not installed or required fields are missing.
    DatabaseError
        If the connection attempt fails.
    """
    psycopg2 = _psycopg2()

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

    try:
        conn = psycopg2.connect(
            host=host,
            port=int(port),
            dbname=database,
            user=username,
            password=password,
        )
        conn.autocommit = False
        return conn
    except Exception as exc:
        raise DatabaseError(
            f"postgres_utils: failed to connect to '{database}' on '{host}': {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Function and procedure calls
# ---------------------------------------------------------------------------


def execute_function(
    conn: Any,
    routine: str,
    named_params: dict[str, Any],
) -> Optional[Any]:
    """
    Call a PostgreSQL function via SELECT and return the scalar result.

    Builds: SELECT routine(%(p_name)s, ...)
    Dict/list values are serialised to JSON strings for jsonb parameters.

    Parameters
    ----------
    conn : Any
        Open psycopg2 connection.
    routine : str
        Fully-qualified function name (e.g. control.f_start_batch).
    named_params : dict[str, Any]
        DB parameter name → value. Order must match the function signature.

    Returns
    -------
    Any | None
        Scalar value returned by the function, or None if no row returned.

    Raises
    ------
    DatabaseError
        If execution fails.
    """
    try:
        cursor = conn.cursor()
        serialised = _serialise_jsonb(named_params)

        if serialised:
            placeholders = ", ".join(f"%({k})s" for k in serialised)
            sql = f"SELECT {routine}({placeholders})"
            cursor.execute(sql, serialised)
        else:
            cursor.execute(f"SELECT {routine}()")

        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None

    except Exception as exc:
        conn.rollback()
        raise DatabaseError(
            f"postgres_utils: execute_function failed for '{routine}': {exc}"
        ) from exc


def execute_procedure(
    conn: Any,
    routine: str,
    named_params: dict[str, Any],
) -> None:
    """
    Call a PostgreSQL procedure via CALL.

    Dict/list values are serialised to JSON strings for jsonb parameters.

    Parameters
    ----------
    conn : Any
        Open psycopg2 connection.
    routine : str
        Fully-qualified procedure name (e.g. control.p_end_batch).
    named_params : dict[str, Any]
        DB parameter name → value.

    Raises
    ------
    DatabaseError
        If execution fails.
    """
    try:
        cursor = conn.cursor()
        serialised = _serialise_jsonb(named_params)

        if serialised:
            placeholders = ", ".join(f"%({k})s" for k in serialised)
            sql = f"CALL {routine}({placeholders})"
            cursor.execute(sql, serialised)
        else:
            cursor.execute(f"CALL {routine}()")

        conn.commit()

    except Exception as exc:
        conn.rollback()
        raise DatabaseError(
            f"postgres_utils: execute_procedure failed for '{routine}': {exc}"
        ) from exc


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
    pgcode = getattr(exc, "pgcode", None) or getattr(
        getattr(exc, "pgerror", None), "pgcode", None
    )
    return pgcode == _TRUNCATION_SQLSTATE


# ---------------------------------------------------------------------------
# DDL exporter provider interface
# ---------------------------------------------------------------------------


def get_current_database(conn: Any) -> str:
    """Return the connected PostgreSQL database name."""
    dbname = getattr(getattr(conn, "info", None), "dbname", None)
    if dbname:
        return str(dbname)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT current_database()")
        row = cursor.fetchone()
        return str(row[0]) if row else "postgres"
    finally:
        cursor.close()


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

    PostgreSQL jsonb parameters must be passed as JSON strings when using
    psycopg2 without extras.register_default_jsonb.
    """
    result: dict[str, Any] = {}
    for k, v in params.items():
        result[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    return result


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


def _postgres_table_ddl(conn: Any, schema: str, table: str) -> str:
    """Build CREATE TABLE DDL from the catalog for base tables.

    Read column metadata from ``pg_attribute`` rather than
    ``information_schema.columns`` so DDL is generated regardless of the
    connecting role's privileges; the standard view is privilege-filtered.
    ``format_type`` yields the exact column type, including length and
    precision modifiers.
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
