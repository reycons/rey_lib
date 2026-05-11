"""
Backend-agnostic DB adapter for pipeline use.
Selects the correct backend (SQL Server, MySQL, etc.) based on the connection config.
"""

from typing import Any

from rey_lib.db import sqlserver_utils
from rey_lib.db import duckdb_utils
# from rey_lib.db import mysql_utils  # Uncomment if/when MySQL support is added

class DBAdapter:
    """
    Database adapter that dispatches to the correct backend implementation
    based on the provider in the connection config.
    """
    def get_connection(self, db_cfg: Any) -> Any:
        provider = getattr(db_cfg, "provider", "").lower()
        if provider == "sqlserver":
            return sqlserver_utils.get_connection(db_cfg)
        elif provider == "duckdb":
            return duckdb_utils.get_connection()
        # elif provider == "mysql":
        #     return mysql_utils.get_connection(db_cfg)
        else:
            raise NotImplementedError(f"Unsupported DB provider: {provider}")


    def bulk_insert(self, conn, schema, table, rows, columns):
        provider = self._get_provider_from_conn(conn)
        if provider == "sqlserver":
            return sqlserver_utils.bulk_insert(conn, schema, table, rows, columns)
        elif provider == "duckdb":
            # DuckDB: use executemany for bulk insert
            if not rows:
                return 0
            placeholders = ", ".join(["?"] * len(columns))
            col_names = ", ".join(f'"{col}"' for col in columns)
            sql = f'INSERT INTO {schema}.{table} ({col_names}) VALUES ({placeholders})'
            values = [tuple(row[col] for col in columns) for row in rows]
            conn.executemany(sql, values)
            return len(rows)
        # elif provider == "mysql":
        #     return mysql_utils.bulk_insert(conn, schema, table, rows, columns)
        else:
            raise NotImplementedError(f"Unsupported DB provider: {provider}")

    def expand_column_if_truncated(self, conn, schema, table, exc, rows, column_defs, logger):
        provider = self._get_provider_from_conn(conn)
        if provider == "sqlserver":
            return sqlserver_utils.expand_column_if_truncated(conn, schema, table, exc, rows, column_defs, logger)
        elif provider == "duckdb":
            # DuckDB: no-op, as columns auto-expand (no silent truncation)
            return False
        # elif provider == "mysql":
        #     return mysql_utils.expand_column_if_truncated(conn, schema, table, exc, rows, column_defs, logger)
        else:
            raise NotImplementedError(f"Unsupported DB provider: {provider}")

    def call_proc_with_output(self, conn, proc_name, named_inputs, output_specs):
        provider = self._get_provider_from_conn(conn)
        if provider == "sqlserver":
            return sqlserver_utils.call_proc_with_output(conn, proc_name, named_inputs, output_specs)
        elif provider == "duckdb":
            # DuckDB: no stored procs, simulate with SQL file or function
            # Not implemented; raise for now
            raise NotImplementedError("DuckDB does not support stored procedures with output params.")
        # elif provider == "mysql":
        #     return mysql_utils.call_proc_with_output(conn, proc_name, named_inputs, output_specs)
        else:
            raise NotImplementedError(f"Unsupported DB provider: {provider}")

    def call_proc(self, conn, proc_name, inputs):
        provider = self._get_provider_from_conn(conn)
        if provider == "sqlserver":
            return sqlserver_utils.call_proc(conn, proc_name, inputs)
        elif provider == "duckdb":
            # DuckDB: no stored procs, simulate with SQL file or function
            raise NotImplementedError("DuckDB does not support stored procedures.")
        # elif provider == "mysql":
        #     return mysql_utils.call_proc(conn, proc_name, inputs)
        else:
            raise NotImplementedError(f"Unsupported DB provider: {provider}")

    def _get_provider_from_conn(self, conn) -> str:
        provider = getattr(conn, "provider", None)
        if provider:
            return provider.lower()
        # SQL Server detection
        if conn.__class__.__module__.startswith("pyodbc"):
            return "sqlserver"
        # DuckDB detection
        if conn.__class__.__module__.startswith("duckdb"):
            return "duckdb"
        # elif ... (add MySQL detection if needed)
        raise NotImplementedError("Cannot determine DB provider from connection object.")

# Usage:
# db_adapter = DBAdapter()
# conn = db_adapter.get_connection(db_cfg)
# db_adapter.bulk_insert(conn, ...)
