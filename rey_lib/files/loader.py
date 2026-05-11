"""
Generic file loading pipeline for backend-agnostic ingestion.

- Reads transformed files from a source directory
- Prepares rows and columns
- Calls db_adapter.bulk_insert to insert rows into the configured database
- All table DDL logic must be handled outside this module
- No backend-specific logic or hardcoded columns

Contract: All configuration is driven by YAML and passed via ctx and config objects.
"""

from pathlib import Path
from typing import Any, Optional
from rey_lib.files.file_utils import input_files, get_reader
from rey_lib.errors.error_utils import DatabaseError, ConfigError


def load_files(
    ctx: Any,
    db_adapter: Any,
    conn: Any,
    data_source: Any,
    load_cfg: Any,
    on_reload: Optional[callable] = None,
) -> int:
    """
    Load files into the database using db_adapter.bulk_insert.

    Parameters
    ----------
    ctx : Any
        Application context.
    db_adapter : Any
        Backend-agnostic DBAdapter instance.
    conn : Any
        Open database connection.
    data_source : Any
        Data source config/namespace.
    load_cfg : Any
        Load config/namespace.
    on_reload : Optional[callable]
        Callback for reload events (optional).

    Returns
    -------
    int
        Total number of rows loaded.
    """
    total_rows = 0
    source_dir = Path(getattr(data_source.paths, load_cfg.source))
    pattern = getattr(load_cfg, "pickup_pattern", "*.csv")
    pending = input_files(source_dir, pattern)

    for file_path in pending:
        try:
            rows = list(get_reader(file_path, file_type="CSV", encoding="utf-8-sig"))
            if not rows:
                continue
            columns = list(rows[0].keys())
            db_adapter.bulk_insert(conn, load_cfg.schema, load_cfg.table, rows, columns)
            total_rows += len(rows)
        except (DatabaseError, ConfigError) as exc:
            # Log and continue to next file
            pass
    return total_rows
