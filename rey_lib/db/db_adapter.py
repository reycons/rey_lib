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
from typing import Any, Optional

from rey_lib.errors.error_utils import ConfigError

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
}

# Maps connection object module prefix → provider name.
# Used by _provider_for_conn when conn.provider is not set.
# Add one entry here per new backend.
_MODULE_PREFIXES: dict[str, str] = {
	"pyodbc":          "sqlserver",
	"duckdb":          "duckdb",
	"mysql.connector": "mysql",
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
