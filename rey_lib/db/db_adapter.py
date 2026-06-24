"""
Backend-agnostic database adapter.

DBAdapter is the single point through which all DB work flows in rey_lib's
file pipeline and any app code that opts in. Each method resolves the
backend from the registry and delegates — no provider-specific logic lives
here.

Adding a new backend
--------------------
1. Create ``rey_lib/db/{provider}_utils.py`` implementing the interface
   (see existing sqlserver_utils / duckdb_utils as reference).
2. Add one entry to ``_REGISTRY`` below.
3. Add one entry to ``_MODULE_PREFIXES`` so open connections are recognised.
4. Set ``provider: {name}`` in the connection config YAML.

No other changes to this file are required.

Provider resolution
-------------------
``get_connection`` inspects ``db_cfg.provider`` (case-insensitive). When that
field is absent, the adapter falls back to inferring the provider from the
``driver`` string — any driver containing ``"sql server"`` resolves to
``sqlserver``. Other backends must declare ``provider`` explicitly.

All other methods detect the provider from the open connection object via
``_provider_for_conn``.
"""

from __future__ import annotations

import importlib
import re
from typing import Any, Optional

from rey_lib.errors.error_utils import ConfigError, DatabaseError

# NOTE: rey_lib.files imports are deferred into the DDL-export methods below.
# rey_lib.files.file_loader imports DBAdapter from this module, so importing
# rey_lib.files at module top creates a circular import that breaks any caller
# that imports db_adapter before rey_lib.files (e.g. control / procedure_map).

__all__ = ["DBAdapter"]


# ---------------------------------------------------------------------------
# Backend registry
#
# Maps provider name → fully-qualified module path.
# All backend modules must implement the interface defined by the existing
# sqlserver_utils and duckdb_utils modules.
#
# To add a new backend: add one entry here and create the utils module.
# Nothing else in this file needs to change.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, str] = {
	"sqlserver": "rey_lib.db.sqlserver_utils",
	"duckdb":    "rey_lib.db.duckdb_utils",
	"mysql":     "rey_lib.db.mysql_utils",
	"postgres":  "rey_lib.db.postgres_utils",
}

# Maps connection object module prefix → provider name.
# Used by _provider_for_conn when conn.provider is not set.
# Add one entry here per new backend.
_MODULE_PREFIXES: dict[str, str] = {
	"pyodbc":          "sqlserver",
	"duckdb":          "duckdb",
	"mysql.connector": "mysql",
	"psycopg2":        "postgres",
}

_DEFAULT_BUILD_ORDER: list[str] = [
    "schemas",
    "types",
    "sequences",
    "tables",
    "constraints",
    "indexes",
    "views",
    "functions",
    "procedures",
    "triggers",
]

_TYPE_ALIASES: dict[str, str] = {
    "schema": "schemas",
    "schemas": "schemas",
    "type": "types",
    "types": "types",
    "sequence": "sequences",
    "sequences": "sequences",
    "table": "tables",
    "tables": "tables",
    "constraint": "constraints",
    "constraints": "constraints",
    "index": "indexes",
    "indexes": "indexes",
    "view": "views",
    "views": "views",
    "function": "functions",
    "functions": "functions",
    "procedure": "procedures",
    "procedures": "procedures",
    "trigger": "triggers",
    "triggers": "triggers",
}

def _backend(provider: str) -> Any:
    """Return the backend module for provider, importing lazily on first use."""
    path = _REGISTRY.get(provider)
    if path is None:
        raise ConfigError(
            f"DBAdapter: unsupported provider '{provider}'. "
            f"Registered providers: {sorted(_REGISTRY)}."
        )
    return importlib.import_module(path)


class DBAdapter:
    """
    Dispatch DB operations to the correct backend based on connection config.

    A single instance can serve any mix of backends — provider lookup happens
    per call. Stateless and safe to share across threads of a single run.
    """

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def get_connection(self, db_cfg: Any) -> Any:
        """
        Open a backend-appropriate connection for ``db_cfg``.

        Parameters
        ----------
        db_cfg : Any
            Connection config Namespace. Must expose a ``provider`` field.
            Falls back to inferring from ``driver`` for backwards compatibility
            with older SQL Server configs that pre-date the provider field.

        Returns
        -------
        Any
            An open backend connection (e.g. ``pyodbc.Connection``).

        Raises
        ------
        ConfigError
            If the provider is missing, unknown, or cannot be inferred.
        """
        provider = self._provider_for_cfg(db_cfg)
        conn = _backend(provider).get_connection(db_cfg)
        try:
            setattr(conn, "provider", provider)
        except (AttributeError, TypeError):
            pass
        return conn

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def fetch_dicts(
        self,
        conn:     Any,
        sql_name: str,
        params:   Optional[list[Any]] = None,
    ) -> list[dict[str, Any]]:
        """Execute a named SQL query and return all rows as a list of dicts.

        Delegates to the backend's ``fetch_dicts`` implementation so the
        return type is always ``list[dict[str, Any]]`` regardless of backend.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        sql_name : str
            SQL filename stem without .sql extension.
        params : Optional[list[Any]]
            Positional query parameters.

        Returns
        -------
        list[dict[str, Any]]
            All result rows as column → value dicts.
        """
        return _backend(self._provider_for_conn(conn)).fetch_dicts(conn, sql_name, params)

    def run_sql(
        self,
        conn:     Any,
        sql_text: str,
        params:   Optional[list[Any]] = None,
    ) -> Any:
        """Execute ad hoc SQL text (e.g. a generated DDL file) and commit.

        Delegates to the backend's ``run_sql`` implementation. Use for raw
        SQL where a named query or stored procedure is not appropriate, such
        as applying generated DDL files.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        sql_text : str
            SQL statement(s) to execute.
        params : Optional[list[Any]]
            Positional query parameters, or ``None`` for parameterless SQL.

        Returns
        -------
        Any
            Backend-specific result (e.g. rows affected for DDL).

        Raises
        ------
        NotImplementedError
            If the connection's provider has no ad hoc SQL support.
        """
        backend = _backend(self._provider_for_conn(conn))
        if not hasattr(backend, "run_sql"):
            raise NotImplementedError(
                f"DBAdapter: provider '{self._provider_for_conn(conn)}' "
                "does not support run_sql."
            )
        return backend.run_sql(conn, sql_text, params)

    # ------------------------------------------------------------------
    # Stored procedures
    # ------------------------------------------------------------------

    def call_proc(
        self,
        conn: Any,
        proc_name: str,
        params: Optional[list[Any]] = None,
    ) -> Any:
        """
        Execute a stored procedure by name with positional parameters.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        proc_name : str
            Fully-qualified procedure name.
        params : Optional[list[Any]]
            Positional input parameters; ``None`` for zero-parameter procs.

        Returns
        -------
        Any
            Backend-specific cursor or result handle.

        Raises
        ------
        NotImplementedError
            If the connection's provider has no stored-procedure support.
        """
        return _backend(self._provider_for_conn(conn)).call_proc(
            conn, proc_name, params
        )

    def call_proc_with_output(
        self,
        conn: Any,
        proc_name: str,
        named_inputs: list[tuple[str, Any]],
        output_specs: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """
        Execute a stored procedure and capture named OUTPUT parameter values.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        proc_name : str
            Fully-qualified procedure name.
        named_inputs : list[tuple[str, Any]]
            Input parameters as ``(name, value)`` pairs in declaration order.
        output_specs : list[tuple[str, str]]
            Output parameters as ``(name, sql_type)`` pairs.

        Returns
        -------
        dict[str, Any]
            Output parameter values keyed by parameter name.

        Raises
        ------
        NotImplementedError
            If the connection's provider has no stored-procedure support.
        """
        return _backend(self._provider_for_conn(conn)).call_proc_with_output(
            conn, proc_name, named_inputs, output_specs
        )

    # ------------------------------------------------------------------
    # Function and procedure execution (PostgreSQL control database)
    # ------------------------------------------------------------------

    def execute_function(
        self,
        conn: Any,
        routine: str,
        named_params: dict[str, Any],
    ) -> Any:
        """
        Call a database function and return its scalar result.

        Uses SELECT for PostgreSQL functions. Other backends must implement
        this method; raises NotImplementedError if unsupported.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        routine : str
            Fully-qualified function name.
        named_params : dict[str, Any]
            DB parameter name → value mapping.

        Returns
        -------
        Any
            Scalar return value from the function.
        """
        return _backend(self._provider_for_conn(conn)).execute_function(
            conn, routine, named_params
        )

    def execute_procedure(
        self,
        conn: Any,
        routine: str,
        named_params: dict[str, Any],
    ) -> None:
        """
        Call a database procedure with no return value.

        Uses CALL for PostgreSQL procedures. Other backends must implement
        this method; raises NotImplementedError if unsupported.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        routine : str
            Fully-qualified procedure name.
        named_params : dict[str, Any]
            DB parameter name → value mapping.
        """
        _backend(self._provider_for_conn(conn)).execute_procedure(
            conn, routine, named_params
        )

    # ------------------------------------------------------------------
    # Staging / bulk insert
    # ------------------------------------------------------------------

    def get_table_columns(
        self,
        conn: Any,
        schema: str,
        table: str,
    ) -> list[str]:
        """Return table columns in ordinal order for the connection backend."""
        return _backend(self._provider_for_conn(conn)).get_table_columns(
            conn, schema, table
        )

    def create_staging_table_if_not_exists(
        self,
        conn: Any,
        schema: str,
        table: str,
        column_defs: list[tuple[str, str]],
    ) -> bool:
        """
        Create a staging table on the connection's backend if it does not
        already exist.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        schema : str
            Target schema (or database.schema, for SQL Server).
        table : str
            Target table name.
        column_defs : list[tuple[str, str]]
            ``(column_name, sql_type)`` pairs. Use neutral types (VARCHAR(n),
            INTEGER, DECIMAL(p,s), DATE, TEXT, TIMESTAMP) — each backend maps
            them to its native equivalents.

        Returns
        -------
        bool
            ``True`` if the table was created on this call; ``False`` if it
            already existed.
        """
        return _backend(self._provider_for_conn(conn)).create_staging_table_if_not_exists(
            conn, schema, table, column_defs
        )

    def bulk_insert(
        self,
        conn: Any,
        schema: str,
        table: str,
        rows: list[dict[str, Any]],
        columns: list[str],
    ) -> int:
        """
        Insert ``rows`` into ``schema.table`` using the most efficient bulk
        mechanism for the backend.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        schema : str
            Target schema (or database.schema).
        table : str
            Target table name.
        rows : list[dict[str, Any]]
            Row dicts; keys must include every entry of ``columns``.
        columns : list[str]
            Column names defining the insert order.

        Returns
        -------
        int
            Number of rows inserted.
        """
        return _backend(self._provider_for_conn(conn)).bulk_insert(
            conn, schema, table, rows, columns
        )

    def is_truncation_error(self, exc: Exception) -> bool:
        """
        Return ``True`` if ``exc`` is a backend-recognized truncation error.

        Iterates all registered backends — the first one to claim the
        exception wins. Backends that do not have a truncation concept
        return ``False``. This method never raises.

        Parameters
        ----------
        exc : Exception
            The exception raised by a prior ``bulk_insert`` call.

        Returns
        -------
        bool
            ``True`` if the exception is a recoverable truncation error.
        """
        for provider in _REGISTRY:
            try:
                if _backend(provider).is_truncation_error(exc):
                    return True
            except Exception:  # noqa: BLE001 — backend module unavailable
                pass
        return False

    # ------------------------------------------------------------------
    # DDL export
    # ------------------------------------------------------------------

    def export_database_ddl(
        self,
        conn: Any,
        *,
        output_root: str,
        database: str | None = None,
        validate_only: bool = False,
        cleanup_stale: bool = True,
    ) -> dict[str, Any]:
        """Export one database into deterministic object files and build artifacts."""
        # Deferred import — see module-top note on the rey_lib.files cycle.
        from rey_lib.files import (
            cleanup_stale_files,
            export_build_manifest_path,
            export_build_sql_path,
            export_db_root,
            export_object_file_path,
            write_file,
        )

        provider = self._provider_for_conn(conn)
        backend = _backend(provider)

        if not hasattr(backend, "list_database_objects"):
            raise ConfigError(f"Provider '{provider}' is missing list_database_objects().")
        if not hasattr(backend, "get_object_ddl"):
            raise ConfigError(f"Provider '{provider}' is missing get_object_ddl().")

        objects_raw = backend.list_database_objects(conn, database=database)
        normalized = self._normalize_export_objects(
            provider=provider,
            conn=conn,
            database=database,
            objects_raw=objects_raw,
        )
        validation = self._validate_export_objects(normalized)
        if validation["errors"]:
            raise DatabaseError(
                "DDL export validation failed: " + "; ".join(validation["errors"])
            )

        ordered = self._build_order(normalized)
        resolved_database = normalized[0]["database"] if normalized else (
            database or self._fallback_database_name(conn)
        )

        db_root = export_db_root(output_root, provider, resolved_database)
        desired_files: set[str] = set()
        ordered_ddls: list[tuple[dict[str, Any], str, str]] = []

        for obj in ordered:
            if validate_only:
                continue
            ddl = backend.get_object_ddl(conn, obj)
            if not isinstance(ddl, str) or not ddl.strip():
                raise DatabaseError(
                    f"Provider '{provider}' returned empty DDL for {obj['key']}."
                )
            file_path = export_object_file_path(
                db_root,
                obj["schema"],
                obj["object_type"],
                obj["file_name"],
            )
            write_file(file_path, ddl.rstrip() + "\n", "TEXT")
            desired_files.add(str(file_path.resolve()))
            ordered_ddls.append((obj, ddl.rstrip(), str(file_path)))

        build_manifest = self._build_manifest(provider, resolved_database, ordered, db_root)

        build_manifest_path = export_build_manifest_path(db_root)
        build_sql_path = export_build_sql_path(db_root)

        if not validate_only:
            write_file(build_manifest_path, build_manifest, "JSON")
            write_file(build_sql_path, self._build_database_sql(db_root, ordered_ddls), "TEXT")
            desired_files.add(str(build_manifest_path.resolve()))
            desired_files.add(str(build_sql_path.resolve()))

            removed = []
            if cleanup_stale:
                removed = cleanup_stale_files(db_root, desired_files)
            else:
                removed = []
        else:
            removed = []

        return {
            "provider": provider,
            "database": resolved_database,
            "validate_only": validate_only,
            "object_count": len(normalized),
            "build_manifest": build_manifest,
            "removed_stale_files": removed,
        }

    def validate_ddl_export(
        self,
        conn: Any,
        *,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Validate export metadata and dependency graph without writing files."""
        provider = self._provider_for_conn(conn)
        backend = _backend(provider)
        if not hasattr(backend, "list_database_objects"):
            raise ConfigError(f"Provider '{provider}' is missing list_database_objects().")

        objects_raw = backend.list_database_objects(conn, database=database)
        normalized = self._normalize_export_objects(
            provider=provider,
            conn=conn,
            database=database,
            objects_raw=objects_raw,
        )
        validation = self._validate_export_objects(normalized)
        if not validation["errors"]:
            self._build_order(normalized)
        return {
            "provider": provider,
            "database": normalized[0]["database"] if normalized else (database or self._fallback_database_name(conn)),
            **validation,
        }

    def _normalize_export_objects(
        self,
        *,
        provider: str,
        conn: Any,
        database: str | None,
        objects_raw: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(objects_raw, list):
            raise DatabaseError(
                f"Provider '{provider}' list_database_objects() must return list[dict]."
            )

        fallback_database = database or self._fallback_database_name(conn)
        result: list[dict[str, Any]] = []
        for item in objects_raw:
            if not isinstance(item, dict):
                raise DatabaseError(
                    f"Provider '{provider}' list_database_objects() returned non-dict item."
                )

            object_type = _normalise_object_type(str(item.get("object_type", "")))
            schema = str(item.get("schema") or "public").strip() or "public"
            name = str(item.get("name") or "").strip()
            obj_database = str(item.get("database") or fallback_database).strip() or fallback_database
            if not name:
                raise DatabaseError(f"Provider '{provider}' returned object with empty name.")

            key = _object_key(object_type, schema, name)
            deps = [
                _normalise_dependency(dep, obj_database)
                for dep in (item.get("dependencies") or [])
            ]

            result.append(
                {
                    **item,
                    "provider": provider,
                    "database": obj_database,
                    "schema": schema,
                    "name": name,
                    "object_type": object_type,
                    "key": key,
                    "file_name": str(item.get("file_name") or f"{schema}.{_safe_name(name)}.sql"),
                    "dependencies": deps,
                }
            )
        return sorted(result, key=lambda o: (o["object_type"], o["schema"], o["name"], o["key"]))

    def _validate_export_objects(self, objects: list[dict[str, Any]]) -> dict[str, Any]:
        duplicates: list[str] = []
        invalid_references: list[str] = []
        missing_dependencies: list[str] = []
        errors: list[str] = []

        seen: set[str] = set()
        keys = {obj["key"] for obj in objects}

        for obj in objects:
            if obj["key"] in seen:
                duplicates.append(obj["key"])
            seen.add(obj["key"])
            for dep in obj["dependencies"]:
                dep_key = dep.get("key")
                if not dep_key:
                    invalid_references.append(f"{obj['key']} -> <invalid dependency>")
                    continue
                if dep_key not in keys:
                    missing_dependencies.append(f"{obj['key']} -> {dep_key}")

        if duplicates:
            errors.append(f"duplicate object definitions: {', '.join(sorted(set(duplicates)))}")
        if missing_dependencies:
            errors.append(f"missing dependencies: {', '.join(sorted(set(missing_dependencies)))}")
        if invalid_references:
            errors.append(f"invalid dependency references: {', '.join(sorted(set(invalid_references)))}")

        return {
            "errors": errors,
            "duplicates": sorted(set(duplicates)),
            "missing_dependencies": sorted(set(missing_dependencies)),
            "invalid_references": sorted(set(invalid_references)),
        }

    def _build_order(self, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nodes = {obj["key"]: obj for obj in objects}
        incoming: dict[str, int] = {k: 0 for k in nodes}
        outgoing: dict[str, list[str]] = {k: [] for k in nodes}

        for obj in objects:
            for dep in obj["dependencies"]:
                dep_key = dep.get("key")
                if dep_key in nodes:
                    outgoing[dep_key].append(obj["key"])
                    incoming[obj["key"]] += 1

        ordered: list[dict[str, Any]] = []
        ready = [k for k, count in incoming.items() if count == 0]
        ready.sort(key=lambda key: _sort_key(nodes[key]))

        while ready:
            key = ready.pop(0)
            ordered.append(nodes[key])
            for nxt in sorted(outgoing[key], key=lambda k: _sort_key(nodes[k])):
                incoming[nxt] -= 1
                if incoming[nxt] == 0:
                    ready.append(nxt)
                    ready.sort(key=lambda k: _sort_key(nodes[k]))

        if len(ordered) != len(objects):
            cycle = sorted(k for k, count in incoming.items() if count > 0)
            raise DatabaseError(
                "Circular dependencies detected: " + ", ".join(cycle)
            )

        return ordered

    def _build_manifest(
        self,
        provider: str,
        database: str,
        ordered: list[dict[str, Any]],
        db_root: str,
    ) -> dict[str, Any]:
        # Deferred import — see module-top note on the rey_lib.files cycle.
        from rey_lib.files import export_build_manifest_path, export_build_sql_path

        return {
            "version": 1,
            "provider": provider,
            "database": database,
            "build_order": _DEFAULT_BUILD_ORDER,
            "objects": [
                {
                    "key": obj["key"],
                    "object_type": obj["object_type"],
                    "schema": obj["schema"],
                    "name": obj["name"],
                    "file": f"{obj['schema']}/{obj['object_type']}/{obj['file_name']}",
                    "dependencies": [dep["key"] for dep in obj["dependencies"] if dep.get("key")],
                }
                for obj in ordered
            ],
            "paths": {
                "build_manifest": str(export_build_manifest_path(db_root).as_posix()),
                "build_database_sql": str(export_build_sql_path(db_root).as_posix()),
            },
        }

    def _build_database_sql(
        self,
        db_root: str,
        ordered_ddls: list[tuple[dict[str, Any], str, str]],
    ) -> str:
        # Deferred import — see module-top note on the rey_lib.files cycle.
        from rey_lib.files import export_relative_posix

        blocks: list[str] = []
        for obj, ddl, file_path in ordered_ddls:
            rel = export_relative_posix(file_path, db_root)
            blocks.append(
                "\n".join(
                    [
                        f"-- {obj['key']}",
                        f"-- source: {rel}",
                        ddl,
                    ]
                )
            )
        return "\n\n".join(blocks).rstrip() + "\n"

    def _fallback_database_name(self, conn: Any) -> str:
        candidate = (
            getattr(conn, "database", None)
            or getattr(getattr(conn, "info", None), "dbname", None)
            or getattr(getattr(conn, "info", None), "database", None)
        )
        return str(candidate).strip() if candidate else "database"

    # ------------------------------------------------------------------
    # Private — provider resolution
    # ------------------------------------------------------------------

    def _provider_for_cfg(self, db_cfg: Any) -> str:
        """
        Resolve provider name from a connection config.

        Honors ``db_cfg.provider`` when present (case-insensitive). Falls back
        to inferring from ``db_cfg.driver`` so older configs that pre-date the
        provider field keep working.

        Parameters
        ----------
        db_cfg : Any
            Connection config Namespace.

        Returns
        -------
        str
            Lowercase provider name.

        Raises
        ------
        ConfigError
            When neither ``provider`` nor a recognizable ``driver`` is set.
        """
        provider = (getattr(db_cfg, "provider", None) or "").strip().lower()

        if provider:
            return provider

        driver = (getattr(db_cfg, "driver", None) or "").lower()

        if "sql server" in driver:
            return "sqlserver"

        if "mysql" in driver:
            return "mysql"

        if "duckdb" in driver:
            return "duckdb"

        name = getattr(db_cfg, "name", "<unnamed>")

        raise ConfigError(
            f"DBAdapter: connection '{name}' has no 'provider' field and "
            "no recognizable 'driver'. Add 'provider' to the connection config."
        )

    def _provider_for_conn(self, conn: Any) -> str:
        """
        Resolve provider name from an already-open connection object.

        Checks for an explicit ``provider`` attribute first, then falls back
        to matching the connection's module name against ``_MODULE_PREFIXES``.

        Parameters
        ----------
        conn : Any
            An open backend connection.

        Returns
        -------
        str
            Lowercase provider name.

        Raises
        ------
        ConfigError
            When the provider cannot be determined.
        """
        explicit = getattr(conn, "provider", None)

        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip().lower()

        module = type(conn).__module__ or ""

        for prefix, provider in _MODULE_PREFIXES.items():
            if module.startswith(prefix):
                return provider

        raise ConfigError(
            f"DBAdapter: cannot determine provider for connection of type "
            f"{type(conn).__name__} (module={module!r}). "
            f"Add an entry to _MODULE_PREFIXES in db_adapter.py."
        )


def _normalise_object_type(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in _TYPE_ALIASES:
        return _TYPE_ALIASES[raw]
    singular = raw.rstrip("s")
    if singular in _TYPE_ALIASES:
        return _TYPE_ALIASES[singular]
    return "tables"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _object_key(object_type: str, schema: str, name: str) -> str:
    return f"{object_type}:{schema}.{name}"


def _normalise_dependency(dep: Any, database: str) -> dict[str, Any]:
    if isinstance(dep, str):
        raw = dep.strip()
        if ":" in raw and "." in raw:
            return {"key": raw, "database": database}
        return {"key": "", "database": database}

    if isinstance(dep, dict):
        dep_type = _normalise_object_type(str(dep.get("object_type") or "table"))
        dep_schema = str(dep.get("schema") or "public").strip() or "public"
        dep_name = str(dep.get("name") or "").strip()
        if dep_name:
            return {
                "key": _object_key(dep_type, dep_schema, dep_name),
                "database": str(dep.get("database") or database),
            }
    return {"key": "", "database": database}


def _sort_key(obj: dict[str, Any]) -> tuple[int, str, str, str]:
    obj_type = obj.get("object_type", "tables")
    try:
        idx = _DEFAULT_BUILD_ORDER.index(obj_type)
    except ValueError:
        idx = len(_DEFAULT_BUILD_ORDER)
    return (idx, str(obj.get("schema", "")), str(obj.get("name", "")), str(obj.get("key", "")))
