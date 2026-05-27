"""
Generic file transform and loading pipeline for delimited file ingestion.

Provides two independent pipeline stages driven entirely by YAML config:

transform_files — reads raw files from inbox_path, validates headers,
    applies list-based column transforms, writes clean output files
    to processing_path, and moves source files per configured movements.

load_files — reads transformed files from processing_path, bulk inserts all
    rows into a SQL Server landing table, and
    moves files per configured movements on success or failure.

On success  — commits and executes configured success movements.
On any error — rolls back, logs every row error, executes failure movements.

All configuration is driven by the YAML data source config — no table
names, schema names, column names, or folder paths are hardcoded here.
This module has no knowledge of any specific application, data model,
or business rule.

All DB calls go through DBAdapter. All file moves go through
file_utils. No raw pyodbc or os calls anywhere in this module.

Public API
----------
transform_files(ctx, data_source, transform_cfg)
    Find and transform all pending inbox files for one data source.
    Accepts one transform config or a list of candidate transforms.
    Returns total number of files successfully transformed.
load_files(ctx, conn, data_source, load_cfg)
    Find and load all pending files for one load configuration.
    Returns total rows loaded across all files processed.
    batch_id is read from ctx.batch_id — set by start_batch() before calling.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from rey_lib.logs.log_utils import get_logger, log_enter, log_exit
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError, ConfigError
from rey_lib.files.file_utils import (
    apply_file_movements,
    input_files,
    pattern_to_glob,
    get_reader,
    move_file,
    write_file,
    converted_output_path,
)
from rey_lib.profiling.file_profiler import infer_sql_type
from rey_lib.files.transformer import (
    transform_row,
    match_header,
    TransformError,
    parse_date_from_filename,
)

# Module-level DBAdapter instance. All DB calls in this module go through
# the adapter, which dispatches to the right backend implementation based
# on each connection config's `provider` field. The file pipeline never
# imports a backend driver directly — that knowledge lives in the adapter.
_db_adapter = DBAdapter()

__all__ = [
    "transform_files",
    "load_files",
    "load_files_to_callback",
    "run_transform",
    "run_load",
    "run_app_hooks",
]

_logger = get_logger(__name__)

# Fixed schema for the rejection table — created on first use via
# create_staging_table_if_not_exists. Table name comes from ctx.rejection.table.
# Types use the neutral vocabulary understood by all backends (see db_adapter).
_REJECTION_COLUMN_DEFS: list[tuple[str, str]] = [
    ("FileName",     "VARCHAR"),
    ("RowNum",       "INTEGER"),
    ("ColumnName",   "VARCHAR"),
    ("RawValue",     "TEXT"),
    ("ErrorMessage", "TEXT"),
    ("BatchID",      "INTEGER"),
    ("RejectedDT",   "TIMESTAMP"),
]

# Matches {ctx.attr} and {data_source.attr} tokens in LLM prompt templates.
_PROMPT_TOKEN_RE = re.compile(r"\{(ctx|data_source)\.([^}]+)\}")

# Keep callback and file-pipeline failures non-fatal without using broad catches.
_NON_FATAL_PIPELINE_ERRORS = (
    ConfigError,
    DatabaseError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
    TransformError,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transform_files(
    ctx: Any,
    data_source: Any,
    transform_cfg: Any,
    sql_dir: Optional[Path] = None,
) -> int:
    """
    Find and transform all pending inbox files for one data source.

    Scans inbox_path for files matching the configured file patterns, opens
    each file once, and reads non-blank lines until one matches any declared
    header signature. Only the matched transform config is then applied.
    Files with no matching header across all candidate transforms are moved
    to the failure destination.

    On success — writes output file to processing_path, moves source file.
    On failure — moves source file to rejected_path, logs all errors.

    Runtime values such as batch_id should be modeled as columns with
    inline ``transform.type: context`` or ``transform.type: constant``.

    File-level hook phases
    ----------------------
    Before processing each file:
      ``ctx.current_file_path`` and ``ctx.current_file_name`` are stamped,
      then any ``transform_hooks`` binding whose ``hook`` field is
      ``"hooks.pre_file_transform"`` fires. After the file is processed
      (whether the transform succeeded, was rejected for header mismatch,
      or otherwise failed), bindings at ``"hooks.post_file_transform"``
      fire. This lets per-file logging (e.g. a BatchStep row per file)
      reference the actual filename via ``source: ctx.current_file_name``.

    Parameters
    ----------
    ctx : Any
        Application context — ctx.batch_id must be set before calling.
    data_source : Any
        Namespace for one data_sources entry. Provides paths and
        max_files_per_run. May expose ``transform_hooks`` — a list of
        binding entries used for both data-source-level and file-level
        phases (filtered by each binding's ``hook`` field).
    transform_cfg : Any
        One transform Namespace or a list of candidate transform Namespaces.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` hook configs. Passed through
        to file-level hook dispatch. May be ``None`` when no sql_file
        hooks are declared.

    Returns
    -------
    int
        Total number of files successfully transformed.
    """
    transforms = _coerce_transform_cfgs(transform_cfg)
    transform_desc = ", ".join(
        f"{cfg.name} {cfg.version}" for cfg in transforms
    )

    log_enter(
        ctx,
        f"transform_files: {data_source.name} / {transform_desc}",
        _logger,
    )
    total = 0

    # Hook connection cache local to this transform_files call — covers
    # per-file pre/post_file_transform bindings. Closed in finally so a
    # crash mid-loop still cleans up.
    file_hook_conns: dict[str, Any] = {}
    bindings = getattr(data_source, "transform_hooks", None)

    try:
        moved = _run_file_movements_pipeline(data_source)
        if moved:
            _logger.info(
                "file_movements: moved %d file(s) into inbox for %s",
                moved,
                data_source.name,
            )

        inbox_dir = _resolve_path(data_source.paths, "inbox_path", ctx=ctx)

        glob_patterns = sorted(
            {
                pattern_to_glob(getattr(cfg, "file_pattern", "*.csv"))
                for cfg in transforms
            }
        )
        pending_map: dict[str, Path] = {}
        for glob_pattern in glob_patterns:
            for file_path in input_files(inbox_dir, glob_pattern):
                pending_map[str(file_path)] = file_path
        pending = sorted(pending_map.values())

        max_files = getattr(data_source, "max_files_per_run", None)
        if max_files is not None:
            pending = pending[:int(max_files)]

        _logger.info(
            "transform_files: %d file(s) pending in %s matching %s",
            len(pending), inbox_dir, glob_patterns,
        )

        for file_path in pending:
            # Stamp current-file attrs on ctx BEFORE pre_file_transform fires
            # so bindings can resolve `source: ctx.current_file_name` etc.
            # _setup_file_ctx will overwrite these inside the transform
            # pipeline; values are identical so the second write is a no-op.
            object.__setattr__(ctx, "current_file_path", str(file_path))
            object.__setattr__(ctx, "current_file_name", file_path.name)

            # Use a try/finally around the per-file body so that
            # post_file_transform always fires, even if matching fails or
            # the transform raises.
            try:
                _run_hook_bindings(
                    ctx,
                    data_source,
                    bindings,
                    "hooks.pre_file_transform",
                    file_hook_conns,
                    sql_dir,
                )

                matched_cfg, header_line = _match_transform(file_path, transforms)
                if matched_cfg is not None:
                    object.__setattr__(ctx, "transforms", matched_cfg)
                if matched_cfg is None or header_line is None:
                    _logger.error(
                        "No header match across %d transform(s) — file rejected: %s",
                        len(transforms),
                        file_path.name,
                    )
                    _reject_unmatched_file(data_source, transforms, file_path)
                    continue

                _logger.debug(
                    "Matched header for '%s' → %s %s",
                    file_path.name,
                    matched_cfg.name,
                    matched_cfg.version,
                )

                success = _transform_one_file(
                    ctx,
                    data_source,
                    matched_cfg,
                    file_path,
                    header_line=header_line,
                )
                if success:
                    total += 1

            finally:
                _run_hook_bindings(
                    ctx,
                    data_source,
                    bindings,
                    "hooks.post_file_transform",
                    file_hook_conns,
                    sql_dir,
                )

    finally:
        # Commit and close every connection opened by file-level hooks.
        for conn_name, conn in file_hook_conns.items():
            try:
                conn.commit()
                conn.close()
                _logger.debug("Closed file-hook connection '%s'", conn_name)
            except Exception:  # noqa: BLE001 — close must never raise
                pass
        log_exit(ctx, f"transform_files done: {total} file(s) transformed", _logger)

    return total


def _run_file_movements_pipeline(data_source: Any) -> int:
    """Run pre-transform file movement pipeline for one data source.

    If data_source has a file_movements block, files are moved (and optionally
    renamed) before inbox scanning begins.
    """
    file_movements = getattr(data_source, "file_movements", None)
    if file_movements is None:
        return 0

    try:
        return apply_file_movements(data_source.paths, file_movements)
    except ValueError as exc:
        _logger.error(
            "Invalid file_movements config for %s: %s",
            getattr(data_source, "name", "<unknown>"),
            exc,
        )
        return 0


def _coerce_transform_cfgs(transform_cfg: Any) -> list[Any]:
    """Return one or more transform configs as a plain list."""
    if transform_cfg is None:
        return []
    if isinstance(transform_cfg, (list, tuple)):
        return list(transform_cfg)
    return [transform_cfg]


def _match_transform(
    file_path: Path,
    transform_cfgs: list[Any],
) -> tuple[Optional[Any], Optional[str]]:
    """
    Scan a file until one non-blank line matches a configured header.

    Returns the matched transform config plus the exact header line read
    from the file. If no header matches, both return values are None.
    """
    if not transform_cfgs:
        return None, None

    cfg_dicts = [
        (cfg, _namespace_to_dict(cfg))
        for cfg in transform_cfgs
    ]
    encoding = getattr(transform_cfgs[0], "encoding", "utf-8-sig")

    try:
        with file_path.open(encoding=encoding, errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                for cfg, cfg_dict in cfg_dicts:
                    if match_header(stripped, cfg_dict):
                        return cfg, stripped
    except OSError as exc:
        _logger.error("Cannot read file '%s': %s", file_path.name, exc)

    return None, None


def _reject_unmatched_file(
    data_source: Any,
    transform_cfgs: list[Any],
    file_path: Path,
) -> None:
    """Move a file to the failure destination after no header matched."""
    if transform_cfgs:
        failure = getattr(getattr(transform_cfgs[0], "movements", None), "failure", None)
        if failure:
            _execute_movements(failure, file_path, data_source.paths)
            return

    rejected_dir = _resolve_path(data_source.paths, "rejected_path")
    move_file(file_path, rejected_dir)


def load_files(
    ctx: Any,
    conn: Any,
    data_source: Any,
    load_cfg: Any,
) -> int:
    """
    Find and load all pending files for one load configuration.

    Scans the configured source path for files matching the pickup_pattern,
    then loads each one independently. A failure on one file does not
    prevent processing of subsequent files.



    Parameters
    ----------
    ctx : Any

    conn : Any
        Open backend connection. Caller manages the connection lifecycle.
    data_source : Any
        Namespace for one data_sources entry from ctx. Provides paths and
        transforms list.
    load_cfg : Any
        Namespace for one loads entry. Provides source path key,
        pickup_pattern, version, load destination, and movements.

    Returns
    -------
    int
        Total number of rows successfully loaded across all files.
    """
    log_enter(ctx, f"load_files: {data_source.name} / {load_cfg.name}", _logger)
    total_rows = 0

    try:
        source_dir    = _resolve_path(data_source.paths, load_cfg.source, ctx=ctx)
        pattern       = _resolve_pattern(load_cfg.pickup_pattern, load_cfg.version, ctx=ctx)
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
            )
            total_rows += rows_loaded

    finally:
        log_exit(
            ctx,
            f"load_files done: {total_rows} total row(s) loaded",
            _logger,
        )

    return total_rows


def load_files_to_callback(
    ctx: Any,
    data_source: Any,
    load_cfg: Any,
    on_load_file: Callable[[Any, Any, Path, list[dict[str, str]]], int],
) -> int:
    """Load converted files by delegating persistence to a callback.

    This variant is for apps that do not use DBAdapter staging logic.
    It reads each converted CSV file, passes rows to on_load_file, and then
    executes configured movement rules on success/failure.

    Parameters
    ----------
    ctx : Any
        Application context used for logging.
    data_source : Any
        One data source config namespace.
    load_cfg : Any
        One load config namespace.
    on_load_file : Callable[[Any, Any, Path, list[dict[str, str]]], int]
        Callback that persists one file and returns inserted row count.

    Returns
    -------
    int
        Total rows loaded across all processed files.
    """
    log_enter(ctx, f"load_files_to_callback: {data_source.name} / {load_cfg.name}", _logger)
    total_rows = 0

    try:
        source_cfg = getattr(load_cfg, "source", None)
        source_name = getattr(source_cfg, "name", "")
        source_version = getattr(source_cfg, "version", "")
        pickup_pattern = getattr(source_cfg, "pickup_pattern", "")

        source_dir = _resolve_path(data_source.paths, "converted_path")
        pattern = _resolve_callback_pattern(
            pickup_pattern=pickup_pattern,
            data_source_name=getattr(data_source, "name", ""),
            source_name=source_name,
            source_version=source_version,
        )

        pending = input_files(source_dir, pattern)

        # Fallback for projects where pickup_pattern does not align with
        # transformed filename conventions.
        if not pending and source_version:
            pending = input_files(source_dir, f"*_{source_version}.csv")

        max_files = getattr(data_source, "max_files_per_run", None)
        if max_files is not None:
            pending = pending[:int(max_files)]

        _logger.info(
            "load_files_to_callback: %d file(s) pending in %s matching '%s'",
            len(pending), source_dir, pattern,
        )

        for file_path in pending:
            try:
                rows = list(
                    get_reader(
                        file_path,
                        file_type="CSV",
                        encoding="utf-8-sig",
                    )
                )
                rows_loaded = on_load_file(data_source, load_cfg, file_path, rows)
                if rows_loaded != len(rows):
                    _logger.warning(
                        "Row count mismatch for '%s': file had %d rows, callback inserted %d",
                        file_path.name,
                        len(rows),
                        rows_loaded,
                    )
                total_rows += rows_loaded
                _execute_movements(load_cfg.movements.success, file_path, data_source.paths, ctx=ctx)
                _logger.info(
                    "Loaded via callback: %s rows=%d",
                    file_path.name,
                    rows_loaded,
                )
            except _NON_FATAL_PIPELINE_ERRORS as exc:
                _logger.error(
                    "Callback load failed for '%s': %s",
                    file_path.name,
                    exc,
                    exc_info=True,
                )
                _execute_movements(load_cfg.movements.failure, file_path, data_source.paths, ctx=ctx)

    finally:
        log_exit(
            ctx,
            f"load_files_to_callback done: {total_rows} row(s) loaded",
            _logger,
        )

    return total_rows


def run_transform(ctx: Any, sql_dir: Optional[Path] = None) -> int:
    """
    Run the transform stage for every data source declared in ctx.

    Iterates ``ctx.data_sources``, calling :func:`transform_files` for each
    one. Hooks declared on the data source via ``transform_hooks:`` are
    dispatched at two phases:

    1. ``hooks.pre_transform``  — fires before transform_files. Output
       params that declare ``row_column`` are auto-injected as extra
       columns into every transformed row for this data source.
    2. ``transform_files``
    3. ``hooks.post_transform`` — fires after transform_files.

    The library does not interpret phase names — it filters bindings by
    the ``hook`` field declared on each entry. Add or rename phases by
    editing the YAML; the dispatcher is shape-agnostic.

    Parameters
    ----------
    ctx : Any
        Application context. Must have a ``data_sources`` iterable where
        each entry exposes a ``transforms`` attribute. Each data source
        may declare a ``transform_hooks`` list of binding entries
        (``{name, sql_config, hook}``); when absent no hooks fire.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` hook sql_configs.

    Returns
    -------
    int
        Total number of files successfully transformed across all sources.
    """
    total = 0

    for data_source in ctx.data_sources:
        object.__setattr__(ctx, "data_sources", data_source)
        bindings = getattr(data_source, "transform_hooks", None)
        hook_conns: dict[str, Any] = {}

        try:
            # Step 1: clear any previously injected row columns for this source.
            object.__setattr__(ctx, "_injected_row_columns", {})

            # Step 2: pre_transform bindings; collect row_column injections.
            row_cols = _run_hook_bindings(
                ctx,
                data_source,
                bindings,
                "hooks.pre_transform",
                hook_conns,
                sql_dir,
            )
            if row_cols:
                object.__setattr__(ctx, "_injected_row_columns", row_cols)
                _logger.debug(
                    "%s: injecting row columns from pre_transform: %s",
                    data_source.name,
                    list(row_cols.keys()),
                )

            # Step 3: transform files. sql_dir is threaded through so
            # per-file hook bindings (hooks.pre_file_transform /
            # hooks.post_file_transform) can resolve type: sql_file configs.
            count = transform_files(
                ctx,
                data_source,
                data_source.transforms,
                sql_dir=sql_dir,
            )
            total += count
            if count:
                _logger.info("%s: %d file(s) transformed", data_source.name, count)

            # Step 4: post_transform bindings.
            _run_hook_bindings(
                ctx,
                data_source,
                bindings,
                "hooks.post_transform",
                hook_conns,
                sql_dir,
            )

        finally:
            # Clear injected row columns so they don't bleed into the next source.
            object.__setattr__(ctx, "_injected_row_columns", {})
            for conn_name, conn in hook_conns.items():
                try:
                    conn.commit()
                    conn.close()
                    _logger.debug("Closed hook connection '%s'", conn_name)
                except Exception:  # noqa: BLE001
                    pass

    return total


def run_app_hooks(
    ctx: Any,
    phase: str,
    sql_dir: Optional[Path] = None,
) -> None:
    """
    Run every app-level (run-scoped) hook binding declared on ``ctx.app_hooks``
    whose ``hook`` field matches ``phase``.

    Intended for lifecycle bookends — call once at the very start of a CLI
    invocation with ``phase="hooks.pre_run"`` and once at the very end with
    ``phase="hooks.post_run"``. The library does not interpret phase names,
    so callers are free to define additional run-scoped phases (e.g.
    ``hooks.pre_sync``) by declaring matching bindings in YAML.

    Connections opened by bindings during this call are cached for the
    duration of the call and closed (with commit) before returning. Hook
    bindings declared on ``ctx.app_hooks`` follow the same shape as
    ``transform_hooks`` and ``load_hooks``: each entry has ``name``,
    ``sql_config``, and ``hook`` fields.

    Parameters
    ----------
    ctx : Any
        Application context. Reads ``ctx.app_hooks`` (absent is a no-op).
    phase : str
        Phase label to filter bindings by, e.g. ``"hooks.pre_run"``.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` sql_configs.

    Notes
    -----
    App-level bindings are not expected to drive row injection — the run
    has no current data source. If a binding declares ``row_column`` output
    params they are logged at debug level but otherwise ignored.
    """
    bindings = getattr(ctx, "app_hooks", None)
    if not bindings:
        return

    open_conns: dict[str, Any] = {}
    try:
        row_cols = _run_hook_bindings(
            ctx,
            None,            # no data source at run scope
            bindings,
            phase,
            open_conns,
            sql_dir,
        )
        if row_cols:
            _logger.debug(
                "app_hooks %s returned row_column values (ignored at run scope): %s",
                phase,
                list(row_cols.keys()),
            )
    finally:
        for conn_name, conn in open_conns.items():
            try:
                conn.commit()
                conn.close()
                _logger.debug("Closed app-hook connection '%s'", conn_name)
            except Exception:  # noqa: BLE001
                pass


def run_load(
    ctx: Any,
    sql_dir: Optional[Path] = None,
) -> int:
    """
    Run the load stage for every data source and load config declared in ctx.

    Iterates ``ctx.data_sources`` and each ``loads`` entry. For each load
    config the connection named by ``load_cfg.load.connection`` is resolved
    from ``ctx.db.connections`` and opened automatically — no connection
    management in the caller. Connections are reused when multiple load
    configs within the same data source share the same connection name,
    and are all closed after that data source's ``post_load_sql`` files
    run.

    All behaviour is driven entirely by YAML config. No app-specific
    schema knowledge lives here: the library just bulk-inserts the rows
    that the transform produced. Per-file idempotency, dedup, or reload
    semantics belong in the calling application (or in the destination
    schema, via constraints).

    YAML shape expected per loads entry::

        - name:           my_load
          version:        "v01"
          source:         converted_path
          pickup_pattern: "file_*_{version}.csv"
          load:
            connection:          <db_connection_name>
            destination_table:   <database>.<schema>.<table>
          movements:
            success: ...
            failure: ...

    Parameters
    ----------
    ctx : Any
        Application context. Must have:
        - ``ctx.data_sources`` — iterable of data source Namespaces
        - ``ctx.db.connections`` — list of named connection Namespaces

    sql_dir : Optional[Path]
        Base directory for resolving ``post_load_sql`` file names and
        ``type: sql_file`` hook configs. Pass ``None`` when no data
        source uses either.

    Returns
    -------
    int
        Total rows loaded across all data sources and load configs.

    Raises
    ------
    ConfigError
        If ``load.connection`` is missing or names an unknown connection.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    total = 0

    for data_source in ctx.data_sources:
        object.__setattr__(ctx, "data_sources", data_source)
        # Cache open connections by name so multiple load configs that share
        # a connection only open it once per data source. Hook bindings reuse
        # this cache so they may share the load connection or open their own.
        open_conns: dict[str, Any] = {}
        last_conn: Any = None

        try:
            for load_cfg in getattr(data_source, "loads", []):
                object.__setattr__(ctx, "loads", load_cfg)
                load_bindings = getattr(load_cfg, "load_hooks", None)

                # pre_load bindings — fire before this load entry's data move.
                _run_hook_bindings(
                    ctx,
                    data_source,
                    load_bindings,
                    "hooks.pre_load",
                    open_conns,
                    sql_dir,
                )

                conn_name = getattr(getattr(load_cfg, "load", None), "connection", None)
                if not conn_name:
                    raise ConfigError(
                        f"load.connection is not set for load '{load_cfg.name}' "
                        f"in data source '{data_source.name}'. "
                        "Add 'connection: <name>' under the load: section in YAML."
                    )

                if conn_name not in open_conns:
                    db_cfg = find_by_name(
                        getattr(getattr(ctx, "db", None), "connections", []),
                        conn_name,
                    )
                    if db_cfg is None:
                        raise ConfigError(
                            f"Connection '{conn_name}' not found in ctx.db.connections. "
                            "Check config/db/*.yaml for the connection definition."
                        )
                    open_conns[conn_name] = _db_adapter.get_connection(db_cfg)
                    _logger.debug("Opened connection '%s'", conn_name)

                conn = open_conns[conn_name]
                last_conn = conn
                rows = load_files(ctx, conn, data_source, load_cfg)
                total += rows

                # Write row count to ctx so post_load hooks (e.g. end_batch_step)
                # can stamp RecordCount on the BatchStep row.
                object.__setattr__(ctx, "step_record_count", rows)

                # post_load bindings — fire after this load entry completes.
                _run_hook_bindings(
                    ctx,
                    data_source,
                    load_bindings,
                    "hooks.post_load",
                    open_conns,
                    sql_dir,
                )

            # post_load_sql runs on the last connection used for this source
            # (backward-compat with existing post_load_sql YAML key).
            if last_conn is not None:
                _execute_post_load_sql(last_conn, data_source, sql_dir)

        finally:
            # Always close every connection opened for this data source.
            for conn_name, conn in open_conns.items():
                try:
                    conn.close()
                    _logger.debug("Closed connection '%s'", conn_name)
                except Exception:  # noqa: BLE001 — close must never raise
                    pass

    return total


def _execute_post_load_sql(conn: Any, data_source: Any, sql_dir: Optional[Path]) -> None:
    """Execute each SQL file listed in data_source.post_load_sql.

    Skips silently when ``post_load_sql`` is absent, empty, or ``sql_dir``
    is ``None``.  Raises ``ConfigError`` when a declared file does not exist.
    Each file is executed as a single statement — use semicolons within the
    file to separate multiple statements where the driver supports it.

    Parameters
    ----------
    conn : Any
        Open database connection — must support ``conn.execute(sql)``.
    data_source : Any
        Data source Namespace; may have a ``post_load_sql`` list attribute.
    sql_dir : Optional[Path]
        Base directory for resolving SQL file names.
    """
    sql_files = getattr(data_source, "post_load_sql", None) or []
    if not sql_files or sql_dir is None:
        return

    for sql_filename in sql_files:
        sql_path = sql_dir / sql_filename
        if not sql_path.exists():
            raise ConfigError(
                f"post_load_sql file not found: {sql_path} "
                f"(data_source='{data_source.name}')"
            )
        sql_text = sql_path.read_text(encoding="utf-8")
        conn.execute(sql_text)
        _logger.info("post_load_sql executed: %s", sql_filename)


# ---------------------------------------------------------------------------
# Private — sql_config hook execution
# ---------------------------------------------------------------------------

def _find_sql_config(ctx: Any, name: str) -> Any:
    """
    Find a named sql_config entry in ctx.sql_configs.

    Parameters
    ----------
    ctx : Any
        Application context.  Must have ``ctx.sql_configs`` list attribute.
    name : str
        Name of the sql_config to locate.

    Returns
    -------
    Any
        The matching sql_config Namespace.

    Raises
    ------
    ConfigError
        If ``ctx.sql_configs`` is absent or the name is not found.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    configs = getattr(ctx, "sql_configs", None)
    if configs is None:
        raise ConfigError(
            f"sql_config '{name}' referenced in hooks but ctx.sql_configs is not "
            "defined. Add a sql_configs section to your config YAML."
        )
    result = find_by_name(configs, name)
    if result is None:
        raise ConfigError(
            f"sql_config '{name}' not found in ctx.sql_configs. "
            "Check config/app/sql_configs.yaml."
        )
    return result


def _find_llm_config(ctx: Any, name: str) -> Any:
    """
    Find a named llm_config entry in ctx.llm_configs.

    Parameters
    ----------
    ctx : Any
        Application context. Must have ``ctx.llm_configs`` list attribute.
    name : str
        Name of the llm_config to locate.

    Returns
    -------
    Any
        The matching llm_config Namespace.

    Raises
    ------
    ConfigError
        If ``ctx.llm_configs`` is absent or the name is not found.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    configs = getattr(ctx, "llm_configs", None)
    if configs is None:
        raise ConfigError(
            f"llm_config '{name}' referenced in hooks but ctx.llm_configs is not "
            "defined. Add a llm_configs section to your config YAML."
        )
    result = find_by_name(configs, name)
    if result is None:
        raise ConfigError(
            f"llm_config '{name}' not found in ctx.llm_configs. "
            "Check config/app/llm_configs.yaml."
        )
    return result


def _render_prompt(template: str, ctx: Any, data_source: Any) -> str:
    """
    Render an LLM prompt template by substituting {ctx.attr} and
    {data_source.attr} tokens with live values.

    Unresolved tokens are left in place so the LLM still receives a
    readable prompt rather than silently losing context.

    Parameters
    ----------
    template : str
        Prompt template string with ``{ctx.attr}`` / ``{data_source.attr}``
        tokens.
    ctx : Any
        Application context — source for ``ctx.*`` tokens.
    data_source : Any
        Current data source Namespace, or ``None`` for run-scoped hooks.

    Returns
    -------
    str
        Rendered prompt text.
    """
    def _replace(m: re.Match) -> str:
        scope, attr = m.group(1), m.group(2)
        obj = ctx if scope == "ctx" else data_source
        if obj is None:
            return m.group(0)
        return str(getattr(obj, attr, m.group(0)))

    return _PROMPT_TOKEN_RE.sub(_replace, template)


def _execute_one_hook_llm(
    ctx: Any,
    data_source: Any,
    llm_cfg: Any,
) -> dict[str, Any]:
    """
    Execute one llm_config hook and return any row-column values.

    Renders the prompt template, calls the configured LLM, writes the
    response to ctx via any declared output_params, and returns any params
    that declare ``row_column`` so they can be injected into transformed rows.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.llm`` with at least one
        configured LLM instance.
    data_source : Any
        Current data source Namespace, or ``None`` for run-scoped hooks.
    llm_cfg : Any
        A single llm_config Namespace from ctx.llm_configs.

    Returns
    -------
    dict[str, Any]
        ``{column_name: value}`` for every output_param that declares
        ``row_column``. Empty dict when there are no such params.

    Raises
    ------
    ConfigError
        If ctx.llm is absent or the named LLM instance is not configured.
    """
    from rey_lib.llm.llm_utils import ask, default_llm  # local import — llm is optional dep

    llm_name      = getattr(llm_cfg, "llm", None) or default_llm(ctx)
    system_prompt = getattr(llm_cfg, "system_prompt", None)
    max_tokens    = int(getattr(llm_cfg, "max_tokens", 500))
    template      = getattr(llm_cfg, "prompt_template", "") or ""

    prompt = _render_prompt(template, ctx, data_source)

    _logger.debug("LLM hook '%s': calling '%s'", llm_cfg.name, llm_name)
    response = ask(ctx, prompt, llm=llm_name, max_tokens=max_tokens, system_prompt=system_prompt)
    _logger.info("LLM hook '%s' response (truncated): %.200s", llm_cfg.name, response)

    row_columns: dict[str, Any] = {}
    for param in (getattr(llm_cfg, "output_params", None) or []):
        ctx_var = getattr(param, "ctx_var", None)
        row_col = getattr(param, "row_column", None)
        if ctx_var:
            object.__setattr__(ctx, ctx_var, response)
            _logger.debug("LLM hook '%s': wrote ctx.%s", llm_cfg.name, ctx_var)
        if row_col:
            row_columns[row_col] = response

    return row_columns


def _resolve_hook_param_value(ctx: Any, data_source: Any, source: str) -> Any:
    """
    Resolve a sql_config param ``source`` value at runtime.

    Supports three formats:

    * ``ctx.<dotted_attr>``     — resolved by walking ctx attribute path
    * ``data_source.<attr>``    — resolved from data_source Namespace
    * anything else             — used as a literal string value

    Parameters
    ----------
    ctx : Any
        Application context.
    data_source : Any
        Current data source Namespace.
    source : str

        ``'data_source.name'`` or ``'2026'``.

    Returns
    -------
    Any
        Resolved value.  Returns empty string if a ctx/data_source path
        segment is missing rather than raising.
    """
    if source.startswith("ctx."):
        return _resolve_ctx_path(ctx, source[4:])
    if source.startswith("data_source."):
        attr = source[len("data_source."):]
        return getattr(data_source, attr, "")
    return source


def _execute_one_hook(
    ctx: Any,
    data_source: Any,
    sql_cfg: Any,
    open_conns: dict[str, Any],
    sql_dir: Optional[Path],
) -> dict[str, Any]:
    """
    Execute one sql_config hook and return any output-param row-column values.

    Resolves and opens the connection (caching by name in ``open_conns``),
    builds input param values, calls the procedure or SQL file, captures
    output params, stores them in ``ctx`` via ``ctx_var``, and returns any
    output params that have a ``row_column`` declared — these will be
    injected as extra columns into every transformed row.

    Parameters
    ----------
    ctx : Any
        Application context.
    data_source : Any
        Current data source Namespace.
    sql_cfg : Any
        A single sql_config Namespace (from ctx.sql_configs).
    open_conns : dict[str, Any]
        Shared connection cache — keyed by connection name.
        Connections are opened on first use and closed by the caller.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` configs.

    Returns
    -------
    dict[str, Any]
        ``{column_name: value}`` for every output_param that declares
        ``row_column``.  Empty dict when there are no such params.

    Raises
    ------
    ConfigError
        If the connection is not found or a required file is missing.
    DatabaseError
        If procedure or SQL execution fails.
    """
    from rey_lib.config.ctx import find_by_name  # local import

    conn_name = getattr(sql_cfg, "connection", None)
    if not conn_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is missing 'connection'. "
            "Add 'connection: <name>' to the sql_config entry."
        )

    # Resolve and cache the connection.
    if conn_name not in open_conns:
        db_cfg = find_by_name(
            getattr(getattr(ctx, "db", None), "connections", []),
            conn_name,
        )
        if db_cfg is None:
            raise ConfigError(
                f"Connection '{conn_name}' not found in ctx.db.connections "
                f"(referenced by sql_config '{sql_cfg.name}')."
            )
        open_conns[conn_name] = _db_adapter.get_connection(db_cfg)
        _logger.debug("Opened connection '%s' for hook '%s'", conn_name, sql_cfg.name)

    conn = open_conns[conn_name]
    hook_type = getattr(sql_cfg, "type", "procedure")
    row_columns: dict[str, Any] = {}

    if hook_type == "sql_file":
        _execute_one_hook_sql_file(conn, sql_cfg, sql_dir)

    else:
        # Default: type == "procedure"
        row_columns = _execute_one_hook_procedure(ctx, data_source, conn, sql_cfg)

    return row_columns


def _execute_one_hook_sql_file(
    conn: Any,
    sql_cfg: Any,
    sql_dir: Optional[Path],
) -> None:
    """
    Execute a sql_file hook: read the file and execute it on conn.

    Parameters
    ----------
    conn : Any
        Open database connection.
    sql_cfg : Any
        sql_config Namespace with ``file`` attribute.
    sql_dir : Optional[Path]
        Base directory for resolving the file name.

    Raises
    ------
    ConfigError
        If ``sql_dir`` is None or the file does not exist.
    """
    if sql_dir is None:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'sql_file' but no sql_dir "
            "was provided to the pipeline call. Pass sql_dir to run_load/run_transform."
        )
    file_name = getattr(sql_cfg, "file", None)
    if not file_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'sql_file' but 'file' is not set."
        )
    sql_path = sql_dir / file_name
    if not sql_path.exists():
        raise ConfigError(
            f"sql_config '{sql_cfg.name}': file not found: {sql_path}"
        )
    sql_text = sql_path.read_text(encoding="utf-8")
    conn.execute(sql_text)
    _logger.info("Hook sql_file executed: %s (config='%s')", file_name, sql_cfg.name)


def _execute_one_hook_procedure(
    ctx: Any,
    data_source: Any,
    conn: Any,
    sql_cfg: Any,
) -> dict[str, Any]:
    """
    Execute a procedure hook, capture output params, write to ctx.

    Parameters
    ----------
    ctx : Any
        Application context — output params are written here via ``ctx_var``.
    data_source : Any
        Current data source Namespace — used to resolve ``data_source.*``
        param sources.
    conn : Any
        Open SQL Server connection.
    sql_cfg : Any
        sql_config Namespace with ``proc``, ``params``, and optionally
        ``output_params`` attributes.

    Returns
    -------
    dict[str, Any]
        ``{row_column: value}`` for each output_param that declares
        ``row_column``.

    Raises
    ------
    ConfigError
        If ``proc`` is missing from the sql_config.
    DatabaseError
        If procedure execution fails.
    """
    proc_name = getattr(sql_cfg, "proc", None)
    if not proc_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'procedure' but 'proc' is not set."
        )

    # Resolve input params: [(param_name, value), ...]
    raw_params = getattr(sql_cfg, "params", None) or []
    named_inputs: list[tuple[str, Any]] = [
        (p.name, _resolve_hook_param_value(ctx, data_source, str(p.source)))
        for p in raw_params
    ]

    # Resolve output param specs: [(param_name, sql_type), ...]
    raw_outputs = getattr(sql_cfg, "output_params", None) or []
    output_specs: list[tuple[str, str]] = [
        (op.name, str(getattr(op, "sql_type", "NVARCHAR(MAX)")))
        for op in raw_outputs
    ]

    # Log input params (resolved values) BEFORE the proc fires so the call
    # can be reproduced from the log alone — useful when a proc misbehaves
    # in production and you need to replay it in SSMS.
    if named_inputs:
        inputs_repr = ", ".join(f"{name}={value!r}" for name, value in named_inputs)
    else:
        inputs_repr = "(none)"
    _logger.info(
        "Hook procedure invoking: %s (config='%s') inputs=[%s]",
        proc_name,
        sql_cfg.name,
        inputs_repr,
    )

    if output_specs:
        output_values = _db_adapter.call_proc_with_output(
            conn, proc_name, named_inputs, output_specs
        )
    else:
        _db_adapter.call_proc(conn, proc_name, [v for _n, v in named_inputs])
        output_values = {}


    if output_values:
        outputs_repr = ", ".join(f"{k}={v!r}" for k, v in output_values.items())
    else:
        outputs_repr = "(none)"
    _logger.info(
        "Hook procedure executed: %s (config='%s') outputs=[%s]",
        proc_name,
        sql_cfg.name,
        outputs_repr,
    )

    # Write each output param to ctx via ctx_var, collect row_column mappings.
    # Each ctx assignment is logged at INFO so the resolved value is visible
    # in the log file alongside the executed-procedure line.
    row_columns: dict[str, Any] = {}
    for out_cfg in raw_outputs:
        value = output_values.get(out_cfg.name)
        ctx_var = getattr(out_cfg, "ctx_var", None)
        if ctx_var:
            object.__setattr__(ctx, ctx_var, value)
            _logger.info(
                "  → ctx.%s = %r  (from %s output '%s')",
                ctx_var,
                value,
                sql_cfg.name,
                out_cfg.name,
            )
        row_col = getattr(out_cfg, "row_column", None)
        if row_col:
            row_columns[row_col] = value

    return row_columns


def _run_hook_bindings(
    ctx: Any,
    data_source: Any,
    bindings: Any,
    phase: str,
    open_conns: dict[str, Any],
    sql_dir: Optional[Path],
) -> dict[str, Any]:
    """
    Execute every hook binding in ``bindings`` whose ``hook`` field matches
    ``phase``.

    The library stays abstract: it does not interpret phase names, it only
    filters by string equality. Callers decide which phase labels exist
    (e.g. ``"hooks.pre_run"``, ``"hooks.pre_transform"``,
    ``"hooks.post_load"``) — bindings declare the phase they fire at via
    their ``hook`` field in YAML.

    Each binding is expected to expose:
      - ``name``        — descriptive label (used in error messages)
      - ``sql_config``  — name of an entry in ``ctx.sql_configs``
      - ``hook``        — phase label this binding fires at

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.sql_configs``.
    data_source : Any
        Current data source Namespace, or ``None`` for app-level bindings.
        Passed through to ``_resolve_hook_param_value`` so bindings can
        reference ``data_source.<attr>`` in their params.
    bindings : Any
        Iterable of binding Namespaces (from YAML), or ``None``/empty.
    phase : str
        Phase label to filter bindings by. Only bindings whose ``hook``
        equals this string are executed; ordering follows YAML declaration.
    open_conns : dict[str, Any]
        Connection cache shared across hooks for this scope. The caller
        owns lifecycle — opens via the cache, closes in a ``finally``.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` sql_configs.

    Returns
    -------
    dict[str, Any]
        Merged ``{row_column: value}`` from every binding's output params
        that declared ``row_column``. Empty dict when nothing matched.

    Raises
    ------
    ConfigError
        If a matching binding has no ``sql_config`` field, or the named
        sql_config is missing from ``ctx.sql_configs``.
    """
    row_columns: dict[str, Any] = {}
    if not bindings:
        return row_columns

    for binding in bindings:
        if getattr(binding, "hook", None) != phase:
            continue

        sql_cfg_name = getattr(binding, "sql_config", None)
        llm_cfg_name = getattr(binding, "llm_config", None)

        if sql_cfg_name:
            sql_cfg = _find_sql_config(ctx, sql_cfg_name)
            cols = _execute_one_hook(ctx, data_source, sql_cfg, open_conns, sql_dir)
        elif llm_cfg_name:
            llm_cfg = _find_llm_config(ctx, llm_cfg_name)
            cols = _execute_one_hook_llm(ctx, data_source, llm_cfg)
        else:
            label = getattr(binding, "name", "<unnamed>")
            raise ConfigError(
                f"Hook binding '{label}' (phase '{phase}') has neither "
                "'sql_config' nor 'llm_config'. Add one to the binding."
            )

        row_columns.update(cols)

    return row_columns


# ---------------------------------------------------------------------------
# Private — recovery from bulk-insert column-width failures
#
# The actual ALTER COLUMN statement is not in this module. The library
# only:
#   1. asks the DBAdapter whether the bulk-insert exception is a
#      truncation-class error (backend-specific check, lives in the
#      adapter),
#   2. computes a new SQL DataType string for each column whose observed
#      value length exceeds its declared length,
#   3. populates ctx.alter_table / alter_column / alter_data_type and
#      runs the configured ``alter_column_data_type`` sql_config — the
#      app decides which proc / SQL that is, via ctx.sql_configs.
# ---------------------------------------------------------------------------

# Buffer added to the observed max length when widening a string column,
# so a one-character overflow doesn't immediately trigger another resize.
_COLUMN_EXPAND_BUFFER = 10

# Pre-compiled regexes for the only two type families the library knows
# how to widen automatically. Any other declared type is skipped — the
# app must size those correctly up front.
_WIDENABLE_TYPE_PATTERNS = (
    ("NVARCHAR", re.compile(r"^NVARCHAR\((\d+)\)$")),
    ("VARCHAR",  re.compile(r"^VARCHAR\((\d+)\)$")),
)


def _alter_oversized_columns(
    ctx: Any,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    column_defs: list[tuple[str, str]],
) -> bool:
    """
note:: this function must call a configure stored procedure or sql file. no DDL should ever be contained on this file.

    Widen any columns whose values exceeded their declared length by
    running the configured ``alter_column_data_type`` sql_config once
    per affected column.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.sql_configs`` containing
        an entry named ``alter_column_data_type``.
    schema : str
        Target schema — may be ``database.schema`` for cross-db inserts.
    table : str
        Target table name.
    rows : list[dict[str, Any]]
        Rows that failed to insert — scanned for observed string lengths.
    column_defs : list[tuple[str, str]]
        Current ``(column_name, sql_type)`` pairs for the staging table.

    Returns
    -------
    bool
        ``True`` if at least one column was altered and the caller should
        retry the bulk insert. ``False`` if no column needed widening or
        the affected columns were of unsupported type families.

    Raises
    ------
    ConfigError
        When ctx has no ``alter_column_data_type`` sql_config configured.
    """
    sql_cfg      = _find_sql_config(ctx, "alter_column_data_type")
    fq_table     = f"{schema}.{table}"
    expanded_any = False
    open_conns: dict[str, Any] = {}

    try:
        for col_name, col_type in column_defs:
            new_type = _compute_expanded_type(
                col_type, _max_string_len(rows, col_name)
            )
            if new_type is None:
                continue

            # The sql_config's params point at these ctx attrs — set them
            # immediately before the call so each invocation widens the
            # right column.
            object.__setattr__(ctx, "alter_table",     fq_table)
            object.__setattr__(ctx, "alter_column",    col_name)
            object.__setattr__(ctx, "alter_data_type", new_type)

            _execute_one_hook(ctx, None, sql_cfg, open_conns, None)
            _logger.info(
                "Altered column '%s.%s' to %s", fq_table, col_name, new_type,
            )
            expanded_any = True
    finally:
        for conn in open_conns.values():
            try:
                conn.commit()
                conn.close()
            except Exception:  # noqa: BLE001 — close must never raise
                pass

    return expanded_any


def _compute_expanded_type(
    current_type: str,
    max_observed: int,
) -> Optional[str]:
    """
    Return the new DataType string for a column that overflowed, or
    ``None`` when no expansion is needed or possible.

    Recognizes NVARCHAR(n) and VARCHAR(n) only. Any other declared type
    is the app's responsibility to size correctly up front — rey_lib
    will not invent type conversions.
    """
    if max_observed <= 0:
        return None
    upper = (current_type or "").upper().strip()
    for prefix, pattern in _WIDENABLE_TYPE_PATTERNS:
        m = pattern.match(upper)
        if not m:
            continue
        current_len = int(m.group(1))
        if max_observed <= current_len:
            return None
        return f"{prefix}({max_observed + _COLUMN_EXPAND_BUFFER})"
    return None


def _max_string_len(rows: list[dict[str, Any]], col_name: str) -> int:
    """
    Maximum string length of ``col_name`` across all rows. None values and
    missing keys count as length 0.
    """
    return max(
        (len(str(row.get(col_name, "") or "")) for row in rows),
        default=0,
    )


# ---------------------------------------------------------------------------
# Private — transform orchestration
# ---------------------------------------------------------------------------

def _write_rejections(
	ctx: Any,
	file_path: Path,
	errors: list[tuple[int, str, Any, Exception]],
) -> None:
	"""Write transform row errors to the configured rejection table.

	Reads ctx.rejection.connection and ctx.rejection.table. If either is
	absent the call is a no-op — rejection logging is optional. Failures
	are logged as warnings and never re-raised so they cannot mask the
	original transform failure.

	Parameters
	----------
	ctx : Any
	    Application context. Must carry ctx.rejection if rejection logging
	    is desired.
	file_path : Path
	    Source file that produced the errors — name is stored per row.
	errors : list[tuple[int, str, Any, Exception]]
	    4-tuples of (row_num, column, raw_value, exception) from transform.
	"""
	rejection_cfg = getattr(ctx, "rejection", None)
	if rejection_cfg is None:
		return

	conn_name  = getattr(rejection_cfg, "connection", None)
	table_name = getattr(rejection_cfg, "table", None)
	if not conn_name or not table_name:
		_logger.warning(
			"ctx.rejection is configured but missing connection or table — "
			"skipping rejection write for %s",
			file_path.name,
		)
		return

	from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

	sql_cfgs = getattr(ctx, "sql_configs", None)
	conn_cfg = find_by_name(sql_cfgs, conn_name) if sql_cfgs else None
	if conn_cfg is None:
		_logger.warning(
			"Rejection connection '%s' not found in sql_configs — "
			"skipping rejection write for %s",
			conn_name,
			file_path.name,
		)
		return

	schema, table = _parse_destination(table_name)
	batch_id      = getattr(ctx, "batch_id", None)
	rejected_dt   = datetime.now()

	rows = [
		{
			"FileName":     file_path.name,
			"RowNum":       row_num,
			"ColumnName":   col,
			"RawValue":     str(raw_value) if raw_value is not None else None,
			"ErrorMessage": str(err),
			"BatchID":      batch_id,
			"RejectedDT":   rejected_dt,
		}
		for row_num, col, raw_value, err in errors
	]

	columns = [col for col, _ in _REJECTION_COLUMN_DEFS]
	try:
		with _db_adapter.get_connection(conn_cfg) as conn:
			_db_adapter.create_staging_table_if_not_exists(
				conn, schema, table, _REJECTION_COLUMN_DEFS
			)
			_db_adapter.bulk_insert(conn, schema, table, rows, columns)
			conn.commit()
		_logger.info(
			"Wrote %d rejection row(s) for '%s' → %s.%s",
			len(rows),
			file_path.name,
			schema,
			table,
		)
	except Exception as exc:  # noqa: BLE001 — rejection write must never mask transform error
		_logger.warning(
			"Failed to write rejection rows for '%s': %s",
			file_path.name,
			exc,
			exc_info=True,
		)


def _transform_one_file(
	ctx: Any,
	data_source: Any,
	transform_cfg: Any,
	file_path: Path,
	header_line: Optional[str] = None,
) -> bool:
	log_enter(ctx, f"_transform_one_file: {file_path.name}", _logger)

	try:
		if header_line is None and not _validate_header(file_path, transform_cfg):
			_logger.error("Header mismatch — file rejected: %s", file_path.name)
			_execute_movements(
				transform_cfg.movements.failure, file_path, data_source.paths, ctx=ctx
			)
			log_exit(
				ctx,
				f"_transform_one_file rejected (header): {file_path.name}",
				_logger,
			)
			return False

		_setup_file_ctx(ctx, file_path, data_source.paths)

		file_name_date = parse_date_from_filename(file_path.name, _namespace_to_dict(transform_cfg))
		object.__setattr__(ctx, "file_name_date", file_name_date)
		_stamp_date_parts(ctx, "file_name_date", file_name_date)
		object.__setattr__(ctx, "transform_version", getattr(transform_cfg, "version", ""))
		object.__setattr__(ctx, "file_checksum", _hash_file(file_path))

		rows, errors = _read_and_transform(
			file_path,
			transform_cfg,
			header_line=header_line,
			ctx=ctx,
		)

		if errors:
			for row_num, col, raw_value, err in errors:
				_logger.error(
					"Transform error — file=%s row=%d col=%s value=%r: %s",
					file_path.name,
					row_num,
					col,
					raw_value,
					err,
				)

			_write_rejections(ctx, file_path, errors)
			_execute_movements(
				transform_cfg.movements.failure, file_path, data_source.paths, ctx=ctx
			)
			log_exit(
				ctx,
				f"_transform_one_file rejected (errors): {file_path.name}",
				_logger,
			)
			return False

		if not rows:
			_logger.warning("No rows produced from file: %s", file_path.name)
			_execute_movements(
				transform_cfg.movements.failure, file_path, data_source.paths, ctx=ctx
			)
			log_exit(
				ctx,
				f"_transform_one_file rejected (empty): {file_path.name}",
				_logger,
			)
			return False

		output_path = _build_output_path(data_source.paths, transform_cfg, file_path, ctx=ctx)
		write_file(
			output_path,
			rows,
			file_type="CSV",
			state_ctx=ctx,
			app=getattr(ctx, "app_name", "") if ctx is not None else "",
			pipeline=getattr(ctx, "pipeline_name", None) if ctx is not None else None,
			reason="transformed",
		)

		# Write row count to ctx so post_file_transform hooks (e.g. end_batch_step)
		# can stamp RecordCount on the BatchStep row.
		object.__setattr__(ctx, "step_record_count", len(rows))

		_logger.info(
			"Transformed: %s → %s  rows=%d",
			file_path.name, output_path.name, len(rows),
		)

		_execute_movements(
			transform_cfg.movements.success, file_path, data_source.paths, ctx=ctx
		)
		log_exit(ctx, f"_transform_one_file done: {file_path.name}", _logger)
		return True

	except _NON_FATAL_PIPELINE_ERRORS as exc:
		_logger.error(
			"Unexpected error transforming '%s': %s",
			file_path.name, exc, exc_info=True,
		)
		_execute_movements(
			transform_cfg.movements.failure, file_path, data_source.paths, ctx=ctx
		)
		log_exit(ctx, f"_transform_one_file failed: {file_path.name}", _logger)
		return False
    
def _build_output_path(
    paths: Any,
    transform_cfg: Any,
    source_file: Path,
    ctx: Any = None,
) -> Path:
    """
    Build the output file path from the transform output config.

    Substitutes {base_file_name} and {version} tokens into the filename
    pattern defined in output.file.name. Output is written to the
    directory named by output.output_dest.

    Token substitutions:
        {base_file_name} — stem of the source file e.g. 'tran_20260331'
        {version}        — transform version from output.version e.g. 'v01'

    Parameters
    ----------
    paths : Any
        Paths Namespace from the data source config.
    transform_cfg : Any
        Transform Namespace providing output.output_dest, output.version,
        and output.file.name.
    source_file : Path
        Source file — stem used for {base_file_name} substitution.

    Returns
    -------
    Path
        Full path for the output file.

    Raises
    ------
    ConfigError
        If output.output_dest or output.file.name is missing.
    """
    output_path_key = getattr(transform_cfg.output, "output_dest", None)
    if output_path_key is None:
        raise ConfigError(
            f"Transform '{transform_cfg.name}' {transform_cfg.version} "
            f"is missing output.output_dest — cannot determine where to write output files."
        )

    file_name_pattern = getattr(transform_cfg.output.file, "name", None)
    if file_name_pattern is None:
        raise ConfigError(
            f"Transform '{transform_cfg.name}' {transform_cfg.version} "
            f"is missing output.file.name — cannot determine output filename pattern."
        )

    output_dir = _resolve_path(paths, output_path_key, ctx=ctx)
    version    = getattr(transform_cfg.output, "version", getattr(transform_cfg, "version", ""))

    file_name_pattern = resolve_ctx_tokens(file_name_pattern, ctx)
    filename = file_name_pattern.format(
        base_file_name=source_file.stem,
        version=version,
    )

    return output_dir / filename


# ---------------------------------------------------------------------------
# Private — load orchestration
# ---------------------------------------------------------------------------





def _load_one_file(
    ctx: Any,
    conn: Any,
    file_path: Path,
    transform_cfg: Any,
    load_cfg: Any,
    paths: Any,
    schema: str,
    table: str,
) -> int:
    """
    Load one file into the landing table.

    Validates the header, reads and transforms all rows, bulk inserts,
    then executes the configured file movements. Full rollback on any
    error — every row error is logged before rollback.


    Parameters
    ----------
    ctx : Any

    conn : Any
        Open backend connection.
    file_path : Path
        Full path of the file to load.
    transform_cfg : Any
        Transform Namespace — provides header, list-based columns, file_type,
        and encoding.
    load_cfg : Any
        Load Namespace — provides movements.
    paths : Any
        Paths Namespace from the data source config.
    schema : str
        Target schema — may be 'database.schema' for cross-db inserts.
    table : str
        Target table name.

    Returns
    -------
    int
        Number of rows loaded, or 0 on failure.
    """
    log_enter(ctx, f"_load_one_file: {file_path.name}", _logger)

    try:
        # Validate header before reading any rows.
        expected_columns = _db_adapter.get_table_columns(conn, schema, table)
        encoding = getattr(transform_cfg, "encoding", "utf-8-sig")

        if not _validate_load_header(file_path, expected_columns, encoding):
            _logger.error("Header mismatch — file rejected: %s", file_path.name)
            _execute_movements(load_cfg.movements.failure, file_path, paths, ctx=ctx)
            log_exit(ctx, f"_load_one_file rejected (header): {file_path.name}", _logger)
            return 0

        rows = list(
            get_reader(
                file_path,
                file_type="CSV",
                encoding=getattr(transform_cfg, "encoding", "utf-8-sig"),
            )
        )

        if not rows:
            _logger.warning("No rows produced from file: %s", file_path.name)
            _execute_movements(load_cfg.movements.failure, file_path, paths, ctx=ctx)
            log_exit(ctx, f"_load_one_file rejected (empty): {file_path.name}", _logger)
            return 0

        columns     = list(rows[0].keys())
        column_defs = _build_column_defs(transform_cfg, columns, rows)

        _db_adapter.create_staging_table_if_not_exists(
            conn, schema, table, column_defs
        )

        try:
            _db_adapter.bulk_insert(conn, schema, table, rows, columns)
            conn.commit()
        except DatabaseError as bulk_exc:
            conn.rollback()
            column_types = dict(column_defs)

            if not _db_adapter.is_truncation_error(bulk_exc):
                raise
            if not _alter_oversized_columns(
                ctx, schema, table, rows, column_defs,
            ):
                raise
            _logger.info(
                "Retrying bulk insert after column alterations: %s",
                file_path.name,
            )
            _db_adapter.bulk_insert(conn, schema, table, rows, columns)
            conn.commit()

        _logger.info(
            "Loaded: %s → %s.%s  rows=%d",
            file_path.name, schema, table, len(rows),
        )

        _execute_movements(load_cfg.movements.success, file_path, paths, ctx=ctx)
        log_exit(ctx, f"_load_one_file done: {file_path.name}", _logger)
        return len(rows)

    except DatabaseError as exc:
        conn.rollback()
        _logger.error(
            "Database error loading '%s' — rolled back: %s",
            file_path.name, exc,
        )
        _execute_movements(load_cfg.movements.failure, file_path, paths, ctx=ctx)
        log_exit(ctx, f"_load_one_file failed: {file_path.name}", _logger)
        return 0


# ---------------------------------------------------------------------------
# Private — column helpers
# ---------------------------------------------------------------------------

def _build_column_defs(
    transform_cfg: Any,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """
    Build a column definition list for staging table creation.

    All types use the neutral vocabulary understood by every backend via
    db_adapter (VARCHAR(n), INTEGER, DECIMAL(p,s), DATE). Backend-specific
    mapping happens in sqlserver_utils / duckdb_utils, not here.

    Parameters
    ----------
    transform_cfg : Any
        Transform Namespace providing list-based columns with inline transform
        entries.
    columns : list[str]
        Ordered list of output column names.
    rows : list[dict[str, Any]]
        Transformed rows — used to compute max varchar lengths.

    Returns
    -------
    list[tuple[str, str]]
        Ordered list of (column_name, sql_type) tuples.
    """
    transform_map = _transform_map(transform_cfg)

    # Compute max observed length per column for varchar sizing.
    max_lengths: dict[str, int] = {}
    for row in rows:
        for col, val in row.items():
            length = len(str(val)) if val is not None else 0
            if col not in max_lengths or length > max_lengths[col]:
                max_lengths[col] = length

    _TRANSFORM_TYPE_MAP: dict[str, str] = {
        "date":       "DATE",
        "datetime":   "DATETIME2",
        "time":       "TIME",
        "regex_date": "DATE",
        "numeric":    "DECIMAL(18, 6)",
    }

    col_defs: list[tuple[str, str]] = []
    for col in columns:
        transform      = transform_map.get(col, {})
        transform_type = transform.get("type", "") if transform else ""
        cast_to        = transform.get("cast_to", "") if transform else ""

        if transform_type in _TRANSFORM_TYPE_MAP:
            sql_type = _TRANSFORM_TYPE_MAP[transform_type]
        elif transform_type == "regex_extract" and cast_to in ("float", "double"):
            sql_type = "DECIMAL(18, 6)"
        elif transform_type == "regex_extract" and cast_to in ("int", "integer"):
            sql_type = "INTEGER"
        else:
            col_values = [
                str(row.get(col, "") or "").strip()
                for row in rows
                if str(row.get(col, "") or "").strip()
            ]
            inferred = infer_sql_type(col_values) if col_values else None
            if inferred:
                sql_type = inferred
            else:
                observed = max_lengths.get(col, 0)
                size     = max(observed + 10, 20)
                sql_type = f"VARCHAR({size})"

        col_defs.append((col, sql_type))

    return col_defs


# ---------------------------------------------------------------------------
# Private — header validation
# ---------------------------------------------------------------------------
def _validate_load_header(
	file_path: Path,
	expected_columns: list[str],
	encoding: str = "utf-8-sig",
) -> bool:
	"""
	Validate converted-file header against destination table columns.
	"""
	try:
		with file_path.open(encoding=encoding, errors="replace") as fh:
			for line in fh:
				actual_header = line.strip()

				if not actual_header:
					continue

				actual_columns = actual_header.split(",")

				if actual_columns == expected_columns:
					return True

				_logger.error(
					"Load header validation failed for '%s'\n"
					"Expected table columns:\n%s\n\n"
					"Actual file columns:\n%s",
					file_path.name,
					",".join(expected_columns),
					actual_header,
				)
				return False

	except OSError as exc:
		_logger.error("Cannot read file '%s': %s", file_path.name, exc)

	return False


def _transform_map(transform_cfg: Any) -> dict[str, dict[str, Any]]:
    """Return output column -> inline transform config for native columns."""
    transform_map: dict[str, dict[str, Any]] = {}

    for col_cfg in _namespace_to_plain(getattr(transform_cfg, "columns", None)) or []:
        if not isinstance(col_cfg, dict):
            continue
        name = str(col_cfg.get("name", ""))
        transform = col_cfg.get("transform") or {}
        if name and isinstance(transform, dict) and transform:
            transform_map[name] = transform

    return transform_map


def _validate_header(file_path: Path, transform_cfg: Any) -> bool:
	"""
	Read the first non-blank line of a file and validate it against the
	expected header defined in transform_cfg.
	"""
	encoding = getattr(transform_cfg, "encoding", "utf-8-sig")

	try:
		with file_path.open(encoding=encoding, errors="replace") as fh:
			for line in fh:
				stripped = line.strip()

				if stripped:
					cfg_dict = _namespace_to_dict(transform_cfg)

					matched = match_header(stripped, cfg_dict)

					if not matched:

						expected = cfg_dict.get("header")

						_logger.error(
							"Header validation failed for '%s'\n"
							"Expected:\n%s\n\n"
							"Actual:\n%s",
							file_path.name,
							expected,
							stripped,
						)

					return matched

	except OSError as exc:
		_logger.error("Cannot read file '%s': %s", file_path.name, exc)

	return False

# ---------------------------------------------------------------------------
# Private — row reading and transformation
# ---------------------------------------------------------------------------

def _read_and_transform(
    file_path: Path,
    transform_cfg: Any,
    header_line: Optional[str] = None,
    ctx: Any = None,
) -> tuple[list[dict[str, Any]], list[tuple[int, str, str]]]:
    """
    Read all rows from a file and apply list-based column transforms.

    Collects all row errors without stopping — returns both the clean
    rows and the full error list so the caller can decide what to do.

    Parameters
    ----------
    file_path : Path
        File to read.
    transform_cfg : Any
        Transform Namespace — provides columns, file_type, encoding.
    header_line : Optional[str]
        Exact header line to locate before reading rows.
    ctx : Any
        Application context passed through to transform_row.

    Returns
    -------
    tuple[list[dict], list[tuple[int, str, str]]]
        (rows, errors) where errors are (row_num, column_name, message).
    """
    file_type = getattr(transform_cfg, "file_type", "CSV")
    encoding  = getattr(transform_cfg, "encoding",  "utf-8-sig")
    delimiter = getattr(transform_cfg, "delimiter", ",")

    cfg_dict             = _normalized_transform_config(transform_cfg, ctx=ctx)
    # Resolve env-var keys for any encrypt transforms — done once per file.
    cfg_dict["secrets"]  = _build_secrets(cfg_dict)

    injected: dict[str, Any] = getattr(ctx, "_injected_row_columns", None) or {}

    rows:   list[dict[str, Any]]       = []
    errors: list[tuple[int, str, str]] = []

    for row_num, raw_row in enumerate(
        get_reader(
            file_path,
            file_type=file_type,
            encoding=encoding,
            header_line=header_line,
            delimiter=delimiter,
        ),
        start=1,
    ):
        if ctx is not None:
            object.__setattr__(ctx, "row_num", row_num)
        try:
            out_row = transform_row(raw_row, cfg_dict, row_num=row_num, ctx=ctx)
            if out_row is None:
                continue
            if injected:
                out_row.update(injected)
            rows.append(out_row)
        except TransformError as exc:
            raw_value = ""

            if getattr(exc, "column", None):
                raw_value = raw_row.get(exc.column, "")

            errors.append((row_num, getattr(exc, "column", ""), raw_value, str(exc)))
            

    return rows, errors


# ---------------------------------------------------------------------------
# Private — file movements
# ---------------------------------------------------------------------------

def _execute_movements(
    movements: Any,
    file_path: Path,
    paths: Any,
    ctx: Any = None,
) -> None:
    """
    Execute a list of file movement instructions from the YAML config.

    Each movement entry specifies an action (move or delete) and the
    source and destination path keys resolved from the paths Namespace.

    Supports:
        - move:   from/to path keys
        - delete: from path key

    Movement errors are logged but never raise — a movement failure
    must not mask the original pipeline error.

    Parameters
    ----------
    movements : Any
        List of movement instruction Namespaces from transform/load cfg.
    file_path : Path
        The file to move or delete.
    paths : Any
        Paths Namespace from the data source config.
    """
    if not movements:
        return

    for instruction in movements:
        move   = getattr(instruction, "move",   None)
        delete = getattr(instruction, "delete", None)

        if move is not None:
            from_key = getattr(move, "from", None)
            src_path = _resolve_movement_source_path(paths, from_key, file_path)
            dest_dir = _resolve_path(paths, move.to, ctx=ctx)
            try:
                if not src_path.exists():
                    _logger.debug("Movement skipped — source missing: %s", src_path)
                    continue
                move_file(
                    src_path,
                    dest_dir,
                    state_ctx=ctx,
                    app=getattr(ctx, "app_name", "") if ctx is not None else "",
                    pipeline=getattr(ctx, "pipeline_name", None) if ctx is not None else None,
                    reason=str(move.to),
                )
                _logger.debug("Moved: %s → %s", src_path.name, dest_dir)
            except OSError as exc:
                _logger.error(
                    "Movement failed — could not move '%s' to '%s': %s",
                    src_path.name, dest_dir, exc,
                )

        elif delete is not None:
            from_key = getattr(delete, "from", None)
            src_path = _resolve_movement_source_path(paths, from_key, file_path)
            try:
                src_path.unlink(missing_ok=True)
                _logger.debug("Deleted: %s", src_path.name)
            except OSError as exc:
                _logger.error(
                    "Movement failed — could not delete '%s': %s",
                    src_path.name, exc,
                )


def _resolve_movement_source_path(
    paths: Any,
    from_key: Optional[str],
    file_path: Path,
) -> Path:
    """
    Resolve the physical source path for a movement instruction.

    For load-stage movements, the active file may be a converted output
    (for example, 'name_v01.csv') while the source file in processing has
    the unversioned name ('name.csv'). This resolver first tries the exact
    filename, then falls back to the unversioned variant when the filename
    ends with a version suffix.

    Parameters
    ----------
    paths : Any
        Paths Namespace from the data source config.
    from_key : Optional[str]
        Optional source path key in the movement config.
    file_path : Path
        Current pipeline file path.

    Returns
    -------
    Path
        Best candidate source path for the movement operation.
    """
    if not from_key:
        return file_path

    base_dir = _resolve_path(paths, from_key)
    exact = base_dir / file_path.name
    if exact.exists():
        return exact

    unversioned_stem = re.sub(r"_v\d+$", "", file_path.stem)
    if unversioned_stem != file_path.stem:
        unversioned = base_dir / f"{unversioned_stem}{file_path.suffix}"
        if unversioned.exists():
            return unversioned

    return exact


def _resolve_callback_pattern(
    pickup_pattern: str,
    data_source_name: str,
    source_name: str,
    source_version: str,
) -> str:
    """Resolve callback-load pickup pattern from load.source config.

    Supports replacement tokens: {data_source}, {name}, {version}.
    """
    if not pickup_pattern:
        return f"*_{source_version}.csv" if source_version else "*.csv"

    return (
        pickup_pattern
        .replace("{data_source}", data_source_name)
        .replace("{name}", source_name)
        .replace("{version}", source_version)
    )


# ---------------------------------------------------------------------------
# Private — config helpers
# ---------------------------------------------------------------------------

def resolve_ctx_tokens(value: str, ctx: Any) -> str:
    """Replace ``{ctx.attr}`` tokens in ``value`` with the live ctx attribute.

    Only tokens of the form ``{ctx.something}`` are substituted — all other
    tokens (``{version}``, ``{base_file_name}``, etc.) are left untouched
    so existing format logic continues to work unchanged.

    Returns ``value`` unchanged when ``ctx`` is ``None`` or no ``{ctx.``
    tokens are present.

    Parameters
    ----------
    value : str
        String that may contain ``{ctx.attr}`` tokens.
    ctx : Any
        Application context. Missing attributes resolve to empty string.

    Returns
    -------
    str
        String with all ``{ctx.attr}`` tokens replaced.
    """
    if ctx is None or "{ctx." not in value:
        return value

    def _replace(m: re.Match) -> str:
        return str(getattr(ctx, m.group(1), "") or "")

    return re.sub(r"\{ctx\.([^}]+)\}", _replace, value)


def _resolve_path(paths: Any, key: str, ctx: Any = None) -> Path:
    """
    Resolve a named path from the paths Namespace.

    Applies ``{ctx.attr}`` token substitution to the path value before
    converting to a Path — all other tokens are left unchanged.

    Parameters
    ----------
    paths : Any
        Paths Namespace from the data source config.
    key : str
        Attribute name — e.g. 'inbox_path', 'processing_path'.
    ctx : Any, optional
        Application context for ``{ctx.attr}`` substitution.

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
    return Path(resolve_ctx_tokens(str(value), ctx))


def _resolve_pattern(pickup_pattern: str, version: str, ctx: Any = None) -> str:
    """
    Substitute ``{version}`` and ``{ctx.attr}`` tokens in a pickup_pattern.

    Parameters
    ----------
    pickup_pattern : str
        Pattern from load config.
    version : str
        Version string to substitute for ``{version}``.
    ctx : Any, optional
        Application context for ``{ctx.attr}`` substitution.

    Returns
    -------
    str
        Resolved glob pattern.
    """
    resolved = pickup_pattern.replace("{version}", version)
    return resolve_ctx_tokens(resolved, ctx)


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
    'Advantage_SCH.transaction'           → ('Advantage_SCH', 'transaction')
    'NaviStage.Advantage_SCH.transaction' → ('NaviStage.Advantage_SCH', 'transaction')

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


def _hash_file(file_path: Path, algorithm: str = "sha256") -> str:
    """Return a hex digest of the file content using the specified algorithm."""
    import hashlib
    h = hashlib.new(algorithm)
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp_date_parts(ctx: Any, prefix: str, d: Optional[date]) -> None:
    """Stamp ``{prefix}_yyyy``, ``_mm``, ``_dd``, ``_yyyymm``, ``_yyyymmdd`` on ctx."""
    if d is None:
        for suffix in ("yyyy", "mm", "dd", "yyyymm", "yyyymmdd"):
            object.__setattr__(ctx, f"{prefix}_{suffix}", None)
        return
    object.__setattr__(ctx, f"{prefix}_yyyy",     d.strftime("%Y"))
    object.__setattr__(ctx, f"{prefix}_mm",       d.strftime("%m"))
    object.__setattr__(ctx, f"{prefix}_dd",       d.strftime("%d"))
    object.__setattr__(ctx, f"{prefix}_yyyymm",   d.strftime("%Y%m"))
    object.__setattr__(ctx, f"{prefix}_yyyymmdd", d.strftime("%Y%m%d"))


def _setup_file_ctx(ctx: Any, file_path: Path, paths: Any) -> None:
    """
    Stamp per-file attributes on ctx before transform begins.

    Sets file metadata (name, stem, extension, size, created/modified dates
    and their parts) and one attribute per paths key so context transforms
    can resolve them via ``ctx.*`` references.

    Parameters
    ----------
    ctx : Any
        Application context.
    file_path : Path
        Full path of the file currently being processed.
    paths : Any
        Paths Namespace from the data source config.
    """
    stat = file_path.stat()

    object.__setattr__(ctx, "current_file_name", file_path.name)
    object.__setattr__(ctx, "current_file_path", str(file_path))
    object.__setattr__(ctx, "incoming_file_name", file_path.name)
    object.__setattr__(ctx, "incoming_file_path", str(file_path))
    object.__setattr__(ctx, "base_file_name", file_path.stem)
    object.__setattr__(ctx, "file_name",         file_path.name)
    object.__setattr__(ctx, "file_stem",         file_path.stem)
    object.__setattr__(ctx, "file_extension",    file_path.suffix)
    object.__setattr__(ctx, "file_size_bytes",   stat.st_size)

    created_date  = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).date()
    modified_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date()

    object.__setattr__(ctx, "file_created_date",  created_date)
    object.__setattr__(ctx, "file_modified_date", modified_date)

    _stamp_date_parts(ctx, "file_created_date",  created_date)
    _stamp_date_parts(ctx, "file_modified_date", modified_date)

    if paths is not None:
        for key, val in _namespace_to_dict(paths).items():
            path_str = str(Path(str(val)) / file_path.name)
            if "\\" in path_str:
                path_str = path_str.replace("\\\\", "\\")
            object.__setattr__(ctx, key, path_str)


def _resolve_ctx_path(ctx: Any, dotted_path: str) -> Any:
    """
    Walk a dot-separated path on ctx and return the value.

    Returns empty string if any segment is not found rather than
    raising — missing ctx values produce blank staging columns, not
    pipeline failures.

    Parameters
    ----------
    ctx : Any
        Application context Namespace.
    dotted_path : str


    Returns
    -------
    Any
        Resolved value, or empty string if not found.
    """
    current = ctx
    for part in dotted_path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return ""
    return current if current is not None else ""


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


def _normalized_transform_config(transform_cfg: Any, ctx: Any = None) -> dict[str, Any]:
    """
    Return the row-transform config in the internal list-based column shape.

    Loader YAML uses one native shape: ``columns`` must be a list where each
    item defines the output name, source, and optional inline transform.
    Legacy mapping-style ``columns``, ``field_transforms``, and ``constants``
    are intentionally rejected so stale YAML is converted instead of silently
    creating two competing config styles.
    """
    cfg_dict = _namespace_to_plain(transform_cfg)
    if cfg_dict.get("field_transforms") is not None:
        raise ConfigError(
            "Legacy transform field_transforms is not supported. "
            "Move each transform under columns[].transform."
        )
    if cfg_dict.get("constants") is not None:
        raise ConfigError(
            "Legacy transform constants is not supported. "
            "Represent constants as columns with transform.type: constant."
        )
    cfg_dict["columns"] = _normalized_columns(cfg_dict.get("columns"))
    return cfg_dict


def _namespace_to_plain(value: Any) -> Any:
    """
    Recursively convert Namespace-like values to plain Python containers.

    The config loader returns Namespace objects for mappings. The transformer
    expects normal dict/list structures, especially for nested transform rules.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _namespace_to_plain(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_namespace_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_namespace_to_plain(item) for item in value)
    if hasattr(value, "items"):
        return {key: _namespace_to_plain(val) for key, val in value.items()}
    return value


def _normalized_columns(columns_cfg: Any) -> list[dict[str, Any]]:
    """Validate and return native list-based column definitions."""
    columns = _namespace_to_plain(columns_cfg)

    if isinstance(columns, list):
        normalized: list[dict[str, Any]] = []
        for col_cfg in columns:
            if not isinstance(col_cfg, dict):
                raise ConfigError("Each transform column entry must be a mapping.")
            if not str(col_cfg.get("name", "")).strip():
                raise ConfigError("Each transform column entry requires name.")
            normalized.append(dict(col_cfg))
        return normalized

    if isinstance(columns, dict):
        raise ConfigError(
            "Legacy mapping-style transform columns is not supported. "
            "Use a list of {name, source, transform} entries."
        )

    raise ConfigError("Transform columns must be a list of column definitions.")

def _build_secrets(cfg_dict: dict[str, Any]) -> dict[str, str]:
    """
    Resolve env-var values for all encrypt transforms in this file config.

    Scans the columns list for entries with ``transform.type: encrypt`` and
    resolves their ``key_env`` names from the current environment. Each
    unique env-var name is resolved once per file.

    Parameters
    ----------
    cfg_dict : dict
        Already-converted config dict for this transform.

    Returns
    -------
    dict[str, str]
        Mapping of env-var name → key value for every encrypt transform found.
        Empty dict when no encrypt transforms are configured.
    """
    import os  # stdlib — imported here to keep the top-level import section clean

    secrets: dict[str, str] = {}

    for col_cfg in cfg_dict.get("columns") or []:
        if not isinstance(col_cfg, dict):
            continue
        tfm = col_cfg.get("transform") or {}
        if tfm.get("type") != "encrypt":
            continue
        key_env = tfm.get("key_env", "")
        if key_env and key_env not in secrets:
            value = os.environ.get(key_env, "")
            secrets[key_env] = value if value else key_env

    return secrets
