"""
Generic file loading pipeline for delimited file ingestion.

Reads transformed output files from a configured source path, applies
column mapping and field transforms defined in a data source YAML config,
injects runtime constants, and bulk inserts all rows into a SQL Server
landing table via sqlserver_utils.

On success  — commits and executes configured success movements.
On any error — rolls back, logs every row error, executes failure movements.

All configuration is driven by the YAML data source config — no table
names, schema names, column names, or folder paths are hardcoded here.
This module has no knowledge of any specific application, data model,
or business rule.

All DB calls go through sqlserver_utils. All file moves go through
file_utils. No raw pyodbc or os calls anywhere in this module.

Public API
----------
load_files(ctx, conn, data_source, load_cfg)
    Find and load all pending files for one load configuration.
    Returns total rows loaded across all files processed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from rey_lib.db import sqlserver_utils
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.files.file_utils import input_files, get_reader, move_file
from rey_lib.files.transformer import transform_row, match_header, TransformError
from rey_lib.logs.log_utils import log_enter, log_exit

__all__ = ["load_files"]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_files(
    ctx: Any,
    conn: Any,
    data_source: Any,
    load_cfg: Any,
    batch_id: Optional[int] = None,
    on_reload: Optional[callable] = None,
) -> int:
    """
    Find and load all pending files for one load configuration.

    Scans the configured source path for files matching the pickup_pattern,
    then loads each one independently. A failure on one file does not
    prevent processing of subsequent files.

    Parameters
    ----------
    ctx : Any
        Application context — used for logging and chunk_size.
    conn : pyodbc.Connection
        Open SQL Server connection. Caller manages the connection lifecycle.
    data_source : Any
        Namespace for one data_sources entry from ctx. Provides paths,
        transforms list, and constants.
    load_cfg : Any
        Namespace for one loads entry. Provides source path key,
        pickup_pattern, version, load destination, and movements.
    batch_id : Optional[int]
        BatchID of the active batch. Stamped into every staging row and
        passed to expand_column_if_truncated for logging.
    on_reload : Optional[callable]
        Callback invoked when a file is found already in staging.
        Signature: (file_path, original_batch_id, new_batch_id) -> None
        Use this to log BatchStep records in the calling application.
        Errors in the callback are logged and suppressed.

    Returns
    -------
    int
        Total number of rows successfully loaded across all files.
    """
    log_enter(ctx, f"load_files: {data_source.name} / {load_cfg.name}", _logger)
    total_rows = 0

    try:
        source_dir    = _resolve_path(data_source.paths, load_cfg.source)
        pattern       = _resolve_pattern(load_cfg.pickup_pattern, load_cfg.version)
        pending       = input_files(source_dir, pattern)
        max_files     = getattr(data_source, "max_files_per_run", None)
        if max_files is not None:
            pending   = pending[:int(max_files)]
            _logger.info(
                "max_files_per_run=%d applied — %d file(s) eligible this run",
                max_files, len(pending),
            )

        transform_cfg = _find_transform(
            data_source.transforms,
            load_cfg.name,
            load_cfg.version,
        )
        schema, table = _parse_destination(load_cfg.load.destination_table)

        _logger.info(
            "load_files: %d file(s) pending in %s matching '%s'",
            len(pending), source_dir, pattern,
        )

        for file_path in pending:
            rows_loaded = _load_one_file(
                ctx, conn, file_path, transform_cfg,
                load_cfg, data_source.paths, schema, table,
                batch_id, on_reload,
            )
            total_rows += rows_loaded

    finally:
        log_exit(
            ctx,
            f"load_files done: {total_rows} total row(s) loaded",
            _logger,
        )

    return total_rows


# ---------------------------------------------------------------------------
# Private — file orchestration
# ---------------------------------------------------------------------------
def _check_file_in_staging(
    conn: Any,
    schema: str,
    table: str,
    file_id_col: str,
    batch_id_col: str,
    file_path: Path,
) -> Optional[int]:
    """
    Check whether rows for this file already exist in the staging table.

    Returns the BatchID that loaded them if found, None otherwise.
    Used at the start of each file load to detect restart conditions.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open SQL Server connection.
    schema : str
        Target schema — may be 'database.schema'.
    table : str
        Target table name.
    file_id_col : str
        Column name that holds the incoming file path.
    batch_id_col : str
        Column name that holds the BatchID.
    file_path : Path
        File being checked.

    Returns
    -------
    Optional[int]
        Original BatchID if rows exist, None if the file is not in staging.
    """
    sql = (
        f"SELECT TOP 1 [{batch_id_col}] "
        f"FROM {schema}.{table} "
        f"WHERE [{file_id_col}] = ?"
    )
    cursor = conn.cursor()
    try:
        cursor.execute(sql, [str(file_path)])
        row = cursor.fetchone()
        if row is None:
            return None
        val = row[0]
        return int(val) if val is not None else None
    except Exception as exc:
        _logger.warning(
            "Could not check staging for '%s': %s — assuming not present.",
            file_path.name, exc,
        )
        return None
    finally:
        cursor.close()


def _delete_staging_rows(
    conn: Any,
    schema: str,
    table: str,
    file_id_col: str,
    file_path: Path,
) -> None:
    """
    Delete all staging rows for a specific file.

    Called before reloading a file whose rows are already in staging.
    Commits immediately — the delete must be durable before the reload
    begins so a subsequent failure does not leave duplicate rows.

    Parameters
    ----------
    conn : pyodbc.Connection
        Open SQL Server connection.
    schema : str
        Target schema — may be 'database.schema'.
    table : str
        Target table name.
    file_id_col : str
        Column name that holds the incoming file path.
    file_path : Path
        File whose staging rows should be deleted.

    Raises
    ------
    DatabaseError
        If the DELETE fails.
    """
    sql = (
        f"DELETE FROM {schema}.{table} "
        f"WHERE [{file_id_col}] = ?"
    )
    cursor = conn.cursor()
    try:
        cursor.execute(sql, [str(file_path)])
        deleted = cursor.rowcount
        conn.commit()
        _logger.info(
            "Deleted %d staging row(s) for '%s' before reload.",
            deleted, file_path.name,
        )
    except Exception as exc:
        conn.rollback()
        raise DatabaseError(
            f"Failed to delete staging rows for '{file_path.name}': {exc}"
        ) from exc
    finally:
        cursor.close()


def _invoke_on_reload(
    callback: Optional[callable],
    file_path: Path,
    original_batch_id: Optional[int],
    new_batch_id: Optional[int],
) -> None:
    """
    Invoke the on_reload callback safely.

    Called when a file is found already in staging. The callback is
    responsible for logging BatchStep records on both the original and
    new batch. Errors are logged and suppressed — a callback failure
    must never abort the reload.

    Parameters
    ----------
    callback : Optional[callable]
        Caller-supplied callback, or None.
    file_path : Path
        File being reloaded.
    original_batch_id : Optional[int]
        BatchID that originally loaded the file.
    new_batch_id : Optional[int]
        BatchID of the current reload run.
    """
    if callback is None:
        return
    try:
        callback(file_path, original_batch_id, new_batch_id)
    except Exception as exc:
        _logger.error(
            "on_reload callback failed for '%s': %s",
            file_path.name, exc, exc_info=True,
        )
        
def _load_one_file(
    ctx: Any,
    conn: Any,
    file_path: Path,
    transform_cfg: Any,
    load_cfg: Any,
    paths: Any,
    schema: str,
    table: str,
    batch_id: Optional[int] = None,
    on_reload: Optional[callable] = None,
) -> int:
    """
    Load one file into the landing table.

    Before loading, checks whether rows for this file already exist in
    the staging table. If they do — indicating a previous run committed
    but the file move failed — the original batch is noted, staging rows
    are deleted, and the file is reloaded under a new batch. The on_reload
    callback is invoked so the caller can log BatchStep records.

    Validates the header, reads and transforms all rows, bulk inserts,
    then executes the configured file movements. Full rollback on any
    error — every row error is logged before rollback.

    Parameters
    ----------
    ctx : Any
        Application context.
    conn : pyodbc.Connection
        Open SQL Server connection.
    file_path : Path
        Full path of the file to load.
    transform_cfg : Any
        Transform Namespace — provides header, columns, field_transforms,
        constants, file_type, encoding.
    load_cfg : Any
        Load Namespace — provides movements, file_id_column, batch_id_column.
    paths : Any
        Paths Namespace from the data source config.
    schema : str
        Target schema — may be 'database.schema' for cross-db inserts.
    table : str
        Target table name.
    batch_id : Optional[int]
        BatchID of the active batch. Stamped into every staging row and
        passed to expand_column_if_truncated for logging.
    on_reload : Optional[callable]
        Callback invoked when a file is found already in staging.
        Signature: (file_path, original_batch_id, new_batch_id) -> None
        Errors in the callback are logged and suppressed.

    Returns
    -------
    int
        Number of rows loaded, or 0 on failure.
    """
    log_enter(ctx, f"_load_one_file: {file_path.name}", _logger)

    try:
        # Resolve restart-safety column names from load config.
        file_id_col   = getattr(load_cfg.load, "file_id_column",  "incoming_file_name")
        batch_id_col  = getattr(load_cfg.load, "batch_id_column",  "BatchID")

        # Check whether this file is already in staging — indicates a
        # previous run committed the insert but failed to move the file.
        original_batch_id = _check_file_in_staging(
            conn, schema, table, file_id_col, batch_id_col, file_path
        )

        if original_batch_id is not None:
            _logger.warning(
                "File '%s' already in staging (BatchID=%s) — deleting and reloading.",
                file_path.name, original_batch_id,
            )

            # Notify the caller so it can log BatchStep records on both batches.
            _invoke_on_reload(on_reload, file_path, original_batch_id, batch_id)

            # Delete the stale staging rows before reloading.
            _delete_staging_rows(conn, schema, table, file_id_col, file_path)

        # Validate header before reading any rows.
        if not _validate_header(file_path, transform_cfg):
            _logger.error("Header mismatch — file rejected: %s", file_path.name)
            _execute_movements(load_cfg.movements.failure, file_path, paths)
            log_exit(ctx, f"_load_one_file rejected (header): {file_path.name}", _logger)
            return 0

        # Build runtime constants — inject batch_id for staging row stamping.
        constants = _build_constants(transform_cfg.constants, file_path, paths)
        if batch_id is not None:
            constants[batch_id_col] = str(batch_id)

        rows, errors = _read_and_transform(file_path, transform_cfg, constants)

        if errors:
            for row_num, col, err in errors:
                _logger.error(
                    "Transform error — file=%s row=%d col=%s: %s",
                    file_path.name, row_num, col, err,
                )
            _execute_movements(load_cfg.movements.failure, file_path, paths)
            log_exit(ctx, f"_load_one_file rejected (transform errors): {file_path.name}", _logger)
            return 0

        if not rows:
            _logger.warning("No rows produced from file: %s", file_path.name)
            _execute_movements(load_cfg.movements.failure, file_path, paths)
            log_exit(ctx, f"_load_one_file rejected (empty): {file_path.name}", _logger)
            return 0

        columns     = list(rows[0].keys())
        column_defs = _build_column_defs(transform_cfg, columns, rows)

        sqlserver_utils.create_staging_table_if_not_exists(
            conn, schema, table, column_defs
        )

        try:
            sqlserver_utils.bulk_insert(conn, schema, table, rows, columns)
            conn.commit()
        except DatabaseError as bulk_exc:
            conn.rollback()
            expanded = sqlserver_utils.expand_column_if_truncated(
                conn, schema, table, bulk_exc, rows, column_defs, batch_id
            )
            if expanded:
                _logger.info(
                    "Retrying bulk insert after column expansion: %s",
                    file_path.name,
                )
                sqlserver_utils.bulk_insert(conn, schema, table, rows, columns)
                conn.commit()
            else:
                raise

        _logger.info(
            "Loaded: %s → %s.%s  rows=%d",
            file_path.name, schema, table, len(rows),
        )

        _execute_movements(load_cfg.movements.success, file_path, paths)
        log_exit(ctx, f"_load_one_file done: {file_path.name}", _logger)
        return len(rows)

    except DatabaseError as exc:
        conn.rollback()
        _logger.error(
            "Database error loading '%s' — rolled back: %s",
            file_path.name, exc,
        )
        _execute_movements(load_cfg.movements.failure, file_path, paths)
        log_exit(ctx, f"_load_one_file failed: {file_path.name}", _logger)
        return 0
    

# ---------------------------------------------------------------------------
# Private — columns helpers
# ---------------------------------------------------------------------------
def _build_column_defs(
    transform_cfg: Any,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """
    Build a column definition list for staging table creation.

    Maps each output column to an appropriate SQL Server type based on
    its field_transform type. Varchar columns are sized to the maximum
    observed value length in the current batch plus a 10-character buffer.

    Parameters
    ----------
    transform_cfg : Any
        Transform Namespace providing field_transforms.
    columns : list[str]
        Ordered list of output column names.
    rows : list[dict[str, Any]]
        Transformed rows — used to compute max varchar lengths.

    Returns
    -------
    list[tuple[str, str]]
        Ordered list of (column_name, sql_type) tuples.
    """
    field_transforms = _namespace_to_dict(
        getattr(transform_cfg, "field_transforms", None)
    )

    # Compute max observed length per column for varchar sizing.
    max_lengths: dict[str, int] = {}
    for row in rows:
        for col, val in row.items():
            length = len(str(val)) if val is not None else 0
            if col not in max_lengths or length > max_lengths[col]:
                max_lengths[col] = length

    # Map transform type → SQL Server type.
    _TRANSFORM_TYPE_MAP: dict[str, str] = {
        "date":       "DATE",
        "regex_date": "DATE",
        "numeric":    "DECIMAL(18, 6)",
    }

    col_defs: list[tuple[str, str]] = []
    for col in columns:
        transform    = field_transforms.get(col, {})
        transform_type = transform.get("type", "") if transform else ""
        cast_to        = transform.get("cast_to", "") if transform else ""

        if transform_type in _TRANSFORM_TYPE_MAP:
            sql_type = _TRANSFORM_TYPE_MAP[transform_type]

        elif transform_type == "regex_extract" and cast_to in ("float", "double"):
            sql_type = "DECIMAL(18, 6)"

        elif transform_type == "regex_extract" and cast_to in ("int", "integer"):
            sql_type = "INT"

        else:
            # No transform or unknown — size varchar from observed data.
            observed = max_lengths.get(col, 0)
            size     = max(observed + 10, 20)   # minimum 20 chars
            sql_type = f"NVARCHAR({size})"

        col_defs.append((col, sql_type))

    return col_defs

# ---------------------------------------------------------------------------
# Private — header validation
# ---------------------------------------------------------------------------
def _validate_header(file_path: Path, transform_cfg: Any) -> bool:
    """
    Read the first non-blank line of a file and validate it against the
    expected header defined in transform_cfg.

    Parameters
    ----------
    file_path : Path
        File to validate.
    transform_cfg : Any
        Transform Namespace providing the expected header string and encoding.

    Returns
    -------
    bool
        True if the header matches, False otherwise.
    """
    encoding = getattr(transform_cfg, "encoding", "utf-8-sig")
    try:
        with file_path.open(encoding=encoding, errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    cfg_dict = _namespace_to_dict(transform_cfg)
                    return match_header(stripped, cfg_dict)
    except OSError as exc:
        _logger.error("Cannot read file '%s': %s", file_path.name, exc)
    return False


# ---------------------------------------------------------------------------
# Private — row reading and transformation
# ---------------------------------------------------------------------------

def _read_and_transform(
    file_path: Path,
    transform_cfg: Any,
    constants: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[tuple[int, str, str]]]:
    """
    Read all rows from a file and apply column mapping, transforms,
    and constant injection via the generic transformer.

    Collects all row errors without stopping — returns both the clean
    rows and the full error list so the caller can decide what to do.

    Parameters
    ----------
    file_path : Path
        File to read.
    transform_cfg : Any
        Transform Namespace — provides columns, field_transforms, constants,
        file_type, encoding.
    constants : dict[str, Any]
        Runtime constants already resolved by _build_constants().

    Returns
    -------
    tuple[list[dict], list[tuple[int, str, str]]]
        (rows, errors) where errors are (row_num, column_name, message).
    """
    file_type = getattr(transform_cfg, "file_type", "CSV")
    encoding  = getattr(transform_cfg, "encoding",  "utf-8-sig")

    # Build a plain dict config for the transformer — inject resolved constants.
    cfg_dict              = _namespace_to_dict(transform_cfg)
    cfg_dict["constants"] = constants

    rows:   list[dict[str, Any]]        = []
    errors: list[tuple[int, str, str]]  = []

    for row_num, raw_row in enumerate(
        get_reader(file_path, file_type=file_type, encoding=encoding),
        start=1,
    ):
        try:
            out_row = transform_row(raw_row, cfg_dict)
            if out_row is None:
                # Row discarded by row_filter — not an error.
                continue
            rows.append(out_row)
        except TransformError as exc:
            errors.append((row_num, "", str(exc)))

    return rows, errors


# ---------------------------------------------------------------------------
# Private — file movements
# ---------------------------------------------------------------------------

def _execute_movements(
    movements: Any,
    file_path: Path,
    paths: Any,
) -> None:
    """
    Execute a list of file movement instructions from the YAML config.

    Each movement entry specifies an action (move or delete) and the
    source and destination path keys. Path keys are resolved from the
    data source paths Namespace.

    Supports:
        - move:  from/to path keys
        - delete: from path key

    Movement errors are logged but never raise — a movement failure
    must not mask the original load error.

    Parameters
    ----------
    movements : Any
        List of movement instruction Namespaces from load_cfg.movements.
    file_path : Path
        The file to move or delete.
    paths : Any
        Paths Namespace from the data source config.
    """
    if not movements:
        return

    for instruction in movements:
        # Each instruction is a Namespace with one key: 'move' or 'delete'.
        move   = getattr(instruction, "move",   None)
        delete = getattr(instruction, "delete", None)

        if move is not None:
            dest_dir = _resolve_path(paths, move.to)
            try:
                move_file(file_path, dest_dir)
                _logger.debug(
                    "Moved: %s → %s", file_path.name, dest_dir
                )
            except OSError as exc:
                _logger.error(
                    "Movement failed — could not move '%s' to '%s': %s",
                    file_path.name, dest_dir, exc,
                )

        elif delete is not None:
            try:
                file_path.unlink(missing_ok=True)
                _logger.debug("Deleted: %s", file_path.name)
            except OSError as exc:
                _logger.error(
                    "Movement failed — could not delete '%s': %s",
                    file_path.name, exc,
                )


# ---------------------------------------------------------------------------
# Private — config helpers
# ---------------------------------------------------------------------------

def _resolve_path(paths: Any, key: str) -> Path:
    """
    Resolve a named path from the paths Namespace.

    Parameters
    ----------
    paths : Any
        Paths Namespace from the data source config.
    key : str
        Attribute name — e.g. 'inbox_path', 'processing_path'.

    Returns
    -------
    Path
        Resolved Path object.

    Raises
    ------
    ValueError
        If the key is not found in the paths Namespace.
    """
    value = getattr(paths, key, None)
    if value is None:
        raise ValueError(
            f"Path key '{key}' not found in data source paths config."
        )
    return Path(value)


def _resolve_pattern(pickup_pattern: str, version: str) -> str:
    """
    Substitute {version} token in a pickup_pattern string.

    Parameters
    ----------
    pickup_pattern : str
        Pattern from load config — may contain {version} token.
    version : str
        Version string to substitute.

    Returns
    -------
    str
        Resolved glob pattern.
    """
    return pickup_pattern.replace("{version}", version)


def _find_transform(
    transforms: list[Any],
    name: str,
    version: str,
) -> Any:
    """
    Find a transform config by name and version.

    Parameters
    ----------
    transforms : list[Any]
        List of transform Namespace objects from data_source.transforms.
    name : str
        Transform name to match.
    version : str
        Transform version to match.

    Returns
    -------
    Any
        Matching transform Namespace.

    Raises
    ------
    ValueError
        If no matching transform is found.
    """
    for t in transforms:
        if (
            getattr(t, "name",    None) == name
            and getattr(t, "version", None) == version
        ):
            return t
    raise ValueError(
        f"No transform found with name='{name}' version='{version}'."
    )


def _parse_destination(destination_table: str) -> tuple[str, str]:
    """
    Parse a destination_table string into (schema, table).

    Handles 2-part (schema.table) and 3-part (database.schema.table)
    names. For 3-part names the database and schema are combined into
    the schema argument so bulk_insert produces valid cross-db SQL.

    Examples
    --------
    'Advantage_SCH.transaction'             → ('Advantage_SCH', 'transaction')
    'NaviControl.Advantage_SCH.transaction' → ('NaviControl.Advantage_SCH', 'transaction')

    Parameters
    ----------
    destination_table : str
        Table reference from the load config.

    Returns
    -------
    tuple[str, str]
        (schema, table)

    Raises
    ------
    ValueError
        If the string does not contain at least one dot.
    """
    parts = destination_table.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"destination_table '{destination_table}' must be at least "
            f"'schema.table' — got {len(parts)} part(s)."
        )
    table  = parts[-1]
    schema = ".".join(parts[:-1])
    return schema, table


def _build_constants(
    constants_cfg: Any,
    file_path: Path,
    paths: Any,
) -> dict[str, Any]:
    """
    Build runtime constant values for one file.

    Resolves placeholder tokens in constant config values using the
    actual file paths computed at load time. Placeholders are substituted
    using the path keys defined in the data source paths config.

    Parameters
    ----------
    constants_cfg : Any
        Constants Namespace from the transform config.
    file_path : Path
        Full path of the file currently being loaded.
    paths : Any
        Paths Namespace from the data source config.

    Returns
    -------
    dict[str, Any]
        Resolved constant values keyed by DB column name.
    """
    # Build substitution map from all named paths in config.
    substitutions: dict[str, str] = {}
    if paths is not None:
        for key in _namespace_to_dict(paths).keys():
            path_val = _resolve_path(paths, key)
            substitutions[key] = str(path_val / file_path.name)

    # Always provide the bare file path as a fallback substitution.
    substitutions["incoming_file_name"] = str(file_path)

    result: dict[str, Any] = {}
    for col, template in _namespace_to_dict(constants_cfg).items():
        value = str(template)
        for token, resolved in substitutions.items():
            value = value.replace(f"{{{token}}}", resolved)
        result[col] = value

    return result


def _namespace_to_dict(ns: Any) -> dict[str, Any]:
    """
    Convert a Namespace object to a plain dict.

    Returns an empty dict when ns is None — allows callers to treat
    missing optional config sections uniformly.

    Parameters
    ----------
    ns : Any
        Namespace object, plain dict, or None.

    Returns
    -------
    dict[str, Any]
        Plain dict of the Namespace contents, or empty dict if None.
    """
    if ns is None:
        return {}
    if isinstance(ns, dict):
        return ns
    return {k: v for k, v in ns.items()}