"""
Backend-agnostic database adapter.

DBAdapter is the single point through which all DB work flows in rey_lib's
file pipeline and any app code that opts in. Each method dispatches to a
backend-specific implementation based on the connection config's ``provider``
field. The file pipeline (and any caller) never imports a backend driver
directly — that knowledge lives here.

Supported providers
-------------------
sqlserver   — wraps ``rey_lib.db.sqlserver_utils``
duckdb      — wraps ``rey_lib.db.duckdb_utils``

Provider resolution
-------------------
``get_connection`` inspects ``db_cfg.provider`` (case-insensitive). When that
field is absent, the adapter falls back to inferring the provider from the
``driver`` string — any driver containing ``"sql server"`` resolves to
``sqlserver``. Other backends must declare ``provider`` explicitly.

All other methods (``call_proc``, ``bulk_insert``, etc.) detect the provider
from the open connection object via ``_provider_for_conn``.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.errors.error_utils import ConfigError

__all__ = ["DBAdapter"]


# ---------------------------------------------------------------------------
# Lazy backend-module accessors
#
# Backend drivers are imported on first use rather than at module load. This
# lets a deployment install only the driver it needs (e.g. pyodbc for a
# SQL-Server-only client) without pulling in duckdb just because it's listed
# as a supported provider. Python caches imports in sys.modules, so the
# overhead after the first call is negligible.
# ---------------------------------------------------------------------------

def _sqlserver_utils() -> Any:
    """Return ``rey_lib.db.sqlserver_utils``, importing on first use."""
    from rey_lib.db import sqlserver_utils  # noqa: WPS433 — intentional lazy import
    return sqlserver_utils


def _duckdb_utils() -> Any:
    """Return ``rey_lib.db.duckdb_utils``, importing on first use."""
    from rey_lib.db import duckdb_utils  # noqa: WPS433 — intentional lazy import
    return duckdb_utils


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
            Connection config Namespace. Should expose a ``provider`` field
            (``"sqlserver"`` / ``"duckdb"``). Falls back to inferring from
            ``driver`` for backwards compatibility with older SQL Server
            configs that pre-date the provider field.

        Returns
        -------
        Any
            An open backend connection (e.g. ``pyodbc.Connection``).

        Raises
        ------
        ConfigError
            If the provider is missing/unknown and cannot be inferred.
        """
        provider = self._provider_for_cfg(db_cfg)
        if provider == "sqlserver":
            return _sqlserver_utils().get_connection(db_cfg)
        if provider == "duckdb":
            return _duckdb_utils().get_connection()
        raise ConfigError(f"DBAdapter: unsupported provider '{provider}'.")

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
        provider = self._provider_for_conn(conn)
        if provider == "sqlserver":
            return _sqlserver_utils().call_proc(conn, proc_name, params)
        if provider == "duckdb":
            raise NotImplementedError(
                "DBAdapter.call_proc: DuckDB has no stored procedures."
            )
        raise ConfigError(f"DBAdapter: unsupported provider '{provider}'.")

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
        provider = self._provider_for_conn(conn)
        if provider == "sqlserver":
            return _sqlserver_utils().call_proc_with_output(
                conn, proc_name, named_inputs, output_specs
            )
        if provider == "duckdb":
            raise NotImplementedError(
                "DBAdapter.call_proc_with_output: DuckDB has no stored procedures."
            )
        raise ConfigError(f"DBAdapter: unsupported provider '{provider}'.")

    # ------------------------------------------------------------------
    # Staging / bulk insert
    # ------------------------------------------------------------------

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
            ``(column_name, sql_type)`` pairs describing the staging table.

        Returns
        -------
        bool
            ``True`` if the table was created on this call; ``False`` if it
            already existed.

        Raises
        ------
        NotImplementedError
            If the connection's provider has no staging-table support yet.
        """
        provider = self._provider_for_conn(conn)
        if provider == "sqlserver":
            return _sqlserver_utils().create_staging_table_if_not_exists(
                conn, schema, table, column_defs
            )
        raise NotImplementedError(
            f"DBAdapter.create_staging_table_if_not_exists: provider "
            f"'{provider}' not yet supported."
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
        provider = self._provider_for_conn(conn)
        if provider == "sqlserver":
            return _sqlserver_utils().bulk_insert(conn, schema, table, rows, columns)
        if provider == "duckdb":
            # DuckDB has no native bulk-insert helper today — use executemany.
            if not rows:
                return 0
            placeholders = ", ".join(["?"] * len(columns))
            col_list = ", ".join(f'"{c}"' for c in columns)
            sql = (
                f'INSERT INTO "{schema}"."{table}" ({col_list}) '
                f'VALUES ({placeholders})'
            )
            values = [tuple(row[c] for c in columns) for row in rows]
            conn.executemany(sql, values)
            return len(rows)
        raise ConfigError(f"DBAdapter: unsupported provider '{provider}'.")

    def expand_column_if_truncated(
        self,
        conn: Any,
        schema: str,
        table: str,
        exc: Exception,
        rows: list[dict[str, Any]],
        column_defs: list[tuple[str, str]],
        batch_id: Optional[int] = None,
    ) -> bool:
        """
        On a bulk-insert truncation error, widen the offending column and
        signal whether a retry is appropriate.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        schema : str
            Target schema (or database.schema).
        table : str
            Target table name.
        exc : Exception
            The original bulk-insert exception to introspect.
        rows : list[dict[str, Any]]
            Rows that failed to insert (used to compute the needed length).
        column_defs : list[tuple[str, str]]
            Column definitions for the staging table.
        batch_id : Optional[int]
            Batch identifier stamped on widening operations for audit.

        Returns
        -------
        bool
            ``True`` if a column was widened and the caller should retry the
            bulk insert; ``False`` if the exception was not a truncation error
            this adapter can handle.
        """
        provider = self._provider_for_conn(conn)
        if provider == "sqlserver":
            return _sqlserver_utils().expand_column_if_truncated(
                conn, schema, table, exc, rows, column_defs, batch_id
            )
        if provider == "duckdb":
            # DuckDB columns auto-expand — never a truncation error to handle.
            return False
        raise ConfigError(f"DBAdapter: unsupported provider '{provider}'.")

    # ------------------------------------------------------------------
    # Private — provider resolution
    # ------------------------------------------------------------------

    def _provider_for_cfg(self, db_cfg: Any) -> str:
        """
        Resolve provider name from a connection config.

        Honors ``db_cfg.provider`` when present (case-insensitive). Falls back
        to inferring from ``db_cfg.driver`` so older configs that pre-date the
        provider field keep working: any driver string containing
        ``"sql server"`` resolves to ``sqlserver``.

        Parameters
        ----------
        db_cfg : Any
            Connection config Namespace.

        Returns
        -------
        str
            Lowercase provider name (e.g. ``"sqlserver"``, ``"duckdb"``).

        Raises
        ------
        ConfigError
            When neither ``provider`` nor a recognizable ``driver`` is set.
        """
        provider = (getattr(db_cfg, "provider", None) or "").strip().lower()
        if provider:
            return provider
        # Backwards-compat inference from driver string.
        driver = (getattr(db_cfg, "driver", None) or "").lower()
        if "sql server" in driver:
            return "sqlserver"
        name = getattr(db_cfg, "name", "<unnamed>")
        raise ConfigError(
            f"DBAdapter: connection '{name}' has no 'provider' field and "
            "no recognizable 'driver'. Add 'provider: sqlserver' (or the "
            "appropriate backend name) to the connection config."
        )

    def _provider_for_conn(self, conn: Any) -> str:
        """
        Resolve provider name from an already-open connection object.

        Checks for an explicit ``provider`` attribute first (callers may
        attach one for clarity), then falls back to inspecting the
        connection's module name.

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
        if explicit:
            return str(explicit).lower()
        module = type(conn).__module__ or ""
        if module.startswith("pyodbc"):
            return "sqlserver"
        if module.startswith("duckdb"):
            return "duckdb"
        raise ConfigError(
            f"DBAdapter: cannot determine provider for connection of type "
            f"{type(conn).__name__} (module={module!r})."
        )
