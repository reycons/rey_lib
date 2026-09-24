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
transform_files(ctx, run_log, data_source, transform_cfg)
    Find and transform all pending inbox files for one data source.
    Accepts one transform config or a list of candidate transforms.
    Returns total number of files successfully transformed.
load_files(ctx, run_log, conn, data_source, load_cfg)
    Find and load all pending files for one load configuration.
    Returns total rows loaded across all files processed.
    batch_id is read from ctx.batch_id — set by start_batch() before calling.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from rey_lib.db.connection import shared_connection
from rey_lib.db.procedure_map import execute_procedure_call, execute_sql_text
from rey_lib.logs.log_utils import (
    get_logger,
    log_artifact_reference,
    log_enter,
    log_error,
    log_exit,
    log_input_discovered,
    log_row_count,
    log_step_failure,
    log_validation_result,
)
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import (
    ConfigError,
    DatabaseError,
    build_safe_error_payload,
)
from rey_lib.files import file_utils
from rey_lib.files.data_file import DataFileStructureError
from rey_lib.files.data_file import data_file_for as _data_file_for
from rey_lib.files.data_file import registered_formats
from rey_lib.files.configured_load import ConfiguredLoad as _ConfiguredLoad
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_loader import DataLoader as _DataLoader
from rey_lib.files.data_loader import adapter_destination
from rey_lib.files.data_transform import IdentityTransform
from rey_lib.files.file_utils import (
    apply_file_movements,
    input_files,
    pattern_to_glob,
    get_reader,
    move_file,
    copy_file,
    write_file,
    converted_output_path,
)
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
    "transform_one",
    "load_one",
    "validate_one",
    "supported_file_types",
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
    ctx: Any, run_log,
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
                key = str(file_path)
                if key not in pending_map:
                    pending_map[key] = file_path
                    log_input_discovered(run_log,
                        input_name=file_path.name,
                        path=str(file_path),
                        pattern=glob_pattern,
                        source_config=f"{data_source.name}.transforms",
                        exists=True,
                        safe_to_preview=True,
                        size_bytes=file_path.stat().st_size if file_path.exists() else None,
                    )
        pending = sorted(pending_map.values())

        max_files = getattr(data_source, "max_files_per_run", None)
        if max_files is not None:
            pending = pending[:int(max_files)]

        _logger.info(
            "transform_files: %d file(s) pending in %s matching %s",
            len(pending), inbox_dir, glob_patterns,
        )

        for file_path in pending:
            if transform_one(ctx, run_log, data_source, file_path):
                total += 1

    finally:
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


def _log_loader_step_failure(
    ctx: Any, run_log,
    exc: BaseException,
    *,
    failed_step_id: str,
    failed_step_name: str,
    related_path: str = "",
) -> str:
    """Log canonical ERROR evidence and STEP_FAILURE reference for loader work."""
    error_payload = build_safe_error_payload(
        exc,
        message=str(exc),
        failed_step_id=failed_step_id,
        failed_step_name=failed_step_name,
        related_path=related_path,
    )
    error_record = log_error(run_log, **error_payload)
    error_id = str(error_record.get("error_id") or "")
    # The text, not the payload -- see build_error_record_payload.
    error_message = str(error_record.get("message") or str(exc))
    return log_step_failure(run_log,
        failed_step_id=failed_step_id,
        failed_step_name=failed_step_name,
        message=error_message,
        failure_record_id=error_id,
        error_id=error_id,
        related_path=related_path,
    )


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
    """Move a file to the failure destination after no header matched.

    The file is only moved when a destination is configured — failure
    movements, or a ``rejected_path`` on the data source. When neither is
    defined the original file is left in place (logged only) so a read-only
    pickup folder is never swept.
    """
    if transform_cfgs:
        failure = getattr(getattr(transform_cfgs[0], "movements", None), "failure", None)
        if failure:
            _execute_movements(failure, file_path, data_source.paths)
            return

    rejected_path = getattr(data_source.paths, "rejected_path", None)
    if not rejected_path:
        _logger.info(
            "No rejected_path configured — leaving unmatched file in place: %s",
            file_path.name,
        )
        return

    rejected_dir = _resolve_path(data_source.paths, "rejected_path")
    move_file(file_path, rejected_dir)


def load_files(
    ctx: Any, run_log,
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
        total_rows = _configured_load(ctx, run_log, data_source, load_cfg).load(
            conn, run_log
        )

    finally:
        log_exit(
            ctx,
            f"load_files done: {total_rows} total row(s) loaded",
            _logger,
        )

    return total_rows


def _route_file(
    ctx: Any,
    run_log: Any,
    movements: Any,
    outcome: str,
    file_path: Path,
    paths: Any,
) -> None:
    """Move a file according to this load's policy, where it has one.

    **A DIRECT load has no movement policy, and that is not the same as an
    empty one.** Nothing was picked up from an inbox, so there is nowhere to
    move the file to and no archive it belongs in -- the caller named a path
    and expects it left alone.

    ``movements is None`` therefore means "do not route", and is why the
    direct path needs no manufactured ``load_cfg`` carrying empty lists.

    Args:
        outcome: ``"success"`` or ``"failure"`` -- which movement list to run.
    """
    if movements is None:
        return

    # An EMPTY list still goes through. A configured load declaring no moves
    # for this outcome has a policy that moves nothing, which is not the same
    # as having no policy -- and keeping the call means a configured load
    # behaves exactly as it did before movements became a resolved value.
    _execute_movements(
        getattr(movements, outcome, []), file_path, paths,
        ctx=ctx, run_log=run_log,
    )


def _build_identity_transform(transform_cfg: Any) -> IdentityTransform:
    """Build the transform for one load definition.

    **The single place an IdentityTransform is constructed.** Both callers
    are ``ConfiguredLoad`` construction sites, so what they produce cannot
    drift.

    ``transform_cfg`` may be None -- a DIRECT load has no configuration at
    all. Every read below is a ``getattr``, so absent configuration yields no
    transform map and no declared columns, which is what that load means.
    Passing a manufactured config object instead would be a stand-in for a
    definition that does not exist.

    Reads the configured columns, which is where an unreadable ``columns:``
    shape is refused rather than treated as absent.
    """
    return IdentityTransform(
        _transform_map(transform_cfg),
        columns=_configured_columns(transform_cfg),
    )


def _destination_identity(
    destination_table: str,
    connection: str,
) -> DatabaseObjectIdentity:
    """Resolve one configured destination into the data object it names.

    **THE TARGET DATA OBJECT.** Past this point the destination is an object
    rather than a path through strings, and nothing downstream re-parses it.

    The parts are kept APART, which is the difference from
    ``_parse_destination``: that one joins a three-part name into the single
    schema argument the adapter takes, and it still does, for the callers that
    hand strings straight to the adapter. An identity carries no rendered
    reference, so the join happens where the adapter is actually called --
    ``data_loader.adapter_destination``.

        'schema.table'             -> catalog '',         schema 'schema'
        'database.schema.table'    -> catalog 'database',  schema 'schema'

    Args:
        destination_table: ``schema.table``, or ``database.schema.table``.
        connection: The CONFIGURED CONNECTION NAME this object lives on.
            Required, and not defaulted: an identity without one is a partial
            endpoint, which is what passing objects exists to stop. Every
            construction site has it -- a direct load is given it, and a
            configured load reads ``load.connection``.

    Returns:
        The identity. The connection is named, never resolved here: opening it
        belongs to whoever runs the load.

    Raises:
        ValueError: When the destination names no schema, or when no
            connection is given.
    """
    name = str(destination_table or "")
    parts = name.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"destination_table '{name}' must be at least 'schema.table' — "
            f"got {len(parts)} part(s)."
        )
    if not str(connection or "").strip():
        raise ValueError(
            f"destination '{name}' needs the configured connection it lives "
            f"on; a database object identity without one is incomplete."
        )
    return DatabaseObjectIdentity(
        connection=str(connection).strip(),
        catalog=".".join(parts[:-2]),
        schema=parts[-2],
        name=parts[-1],
    )


def _build_data_loader(
    ctx: Any,
    create_declared: bool,
) -> _DataLoader:
    """Build the loader for one load's policy.

    **The single place a DataLoader is constructed on this path**, so the
    definition-level build and the single-file build cannot differ.

    **No destination.** Which object is written to is the target data object's
    and arrives per call, so this builds POLICY only -- what the load may do,
    which is not a property of the object it writes to.

    The widening callback is bound here because widening is CONFIGURED
    behaviour -- a declared routine, never inline DDL -- and its parameters
    travel through ``ctx``. This is the boundary that still holds ``ctx``; the
    loader never learns how widening is configured. The destination reaches it
    per call now, rather than being closed over, for the same reason it is no
    longer held.
    """
    return _DataLoader(
        adapter=_db_adapter,
        create_destination=create_declared,
        widen_columns=(
            lambda _conn, target, records, defs: _alter_oversized_columns(
                ctx, *adapter_destination(target), records, defs,
            )
        ),
    )


def _configured_load(
    ctx: Any,
    run_log: Any,
    data_source: Any,
    load_cfg: Any,
    explicit_files: Optional[list[Path]] = None,
) -> _ConfiguredLoad:
    """Resolve one load definition into the object that runs it.

    **This is the construction boundary.** Everything configuration has to say
    is read here -- where files come from, what they are called, which
    transform describes them, which table they go to, whether it may be
    created -- and nothing below re-reads it.

    ``ctx`` is consumed here for resolution. It still travels into the
    per-file step, which needs it for run logging and file movements; those
    are not configuration.
    """
    transform_cfg = _find_transform(
        data_source.transforms, load_cfg.name, load_cfg.version,
    )
    # THE TARGET DATA OBJECT, resolved once where configuration is read. The
    # connection it names comes from the same load block that has always
    # named it, so nothing downstream reads that name again.
    target = _destination_identity(
        load_cfg.load.destination_table,
        getattr(getattr(load_cfg, "load", None), "connection", "") or "",
    )
    create_declared = _create_destination_declared(load_cfg)
    source_config = f"{data_source.name}.{load_cfg.name}"

    def _record_discovered(file_path: Path) -> None:
        log_input_discovered(run_log,
            input_name=file_path.name,
            path=str(file_path),
            pattern=_resolve_pattern(load_cfg.pickup_pattern, load_cfg.version,
                                     ctx=ctx),
            source_config=source_config,
            exists=True,
            safe_to_preview=True,
            size_bytes=file_path.stat().st_size if file_path.exists() else None,
        )

    def _load_one(source: Any, transform: Any, target: Any,
                  **context: Any) -> int:
        # The definition's own source, transform and target arrive here; this
        # step wraps them in movements and evidence and builds nothing of its
        # own.
        return _load_one_file(
            source, transform, target,
            ctx=ctx,
            transform_cfg=transform_cfg,
            load_cfg=load_cfg,
            paths=data_source.paths,
            **context,
        )

    return _ConfiguredLoad(
        source_dir=_resolve_path(data_source.paths, load_cfg.source, ctx=ctx),
        pattern=_resolve_pattern(load_cfg.pickup_pattern, load_cfg.version,
                                 ctx=ctx),
        load_one_file=_load_one,
        transform=_build_identity_transform(transform_cfg),
        loader=_build_data_loader(ctx, create_declared),
        target=target,
        file_type=getattr(transform_cfg, "file_type", "CSV"),
        encoding=getattr(transform_cfg, "encoding", "utf-8-sig"),
        max_files=getattr(data_source, "max_files_per_run", None),
        name=source_config,
        on_discovered=_record_discovered,
        explicit_files=explicit_files,
    )


def load_files_to_callback(
    ctx: Any,
    run_log: Any,
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
                # CSV IS CORRECT HERE, and deliberately not the configured
                # type. This reads converted_path -- the TRANSFORM'S OWN
                # OUTPUT -- and _transform_one_file writes that as CSV
                # unconditionally. The format is this pipeline's own choice,
                # not the source's, so there is nothing for a config to
                # declare. The load stage reads a configured source and does
                # honour the configured type; this does not read one.
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
                _execute_movements(load_cfg.movements.success, file_path, data_source.paths,
                                   ctx=ctx, run_log=run_log)
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
                _execute_movements(load_cfg.movements.failure, file_path, data_source.paths,
                                   ctx=ctx, run_log=run_log)

    finally:
        log_exit(
            ctx,
            f"load_files_to_callback done: {total_rows} row(s) loaded",
            _logger,
        )

    return total_rows


def run_transform(ctx: Any, run_log, sql_dir: Optional[Path] = None) -> int:
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
        # A data source with no transforms (e.g. an analysis-only source that
        # shares the same config tree) has nothing to transform — skip it so
        # the transform stage can coexist with non-loader data sources.
        if not getattr(data_source, "transforms", None):
            _logger.debug(
                "transform: skipping '%s' — no transforms declared.",
                getattr(data_source, "name", "?"),
            )
            continue
        object.__setattr__(ctx, "data_sources", data_source)
        count = transform_files(ctx, run_log, data_source, data_source.transforms, sql_dir=sql_dir)
        total += count
        if count:
            _logger.info("%s: %d file(s) transformed", data_source.name, count)

    return total


# ---------------------------------------------------------------------------
# Public per-file APIs (single file, no discovery, no hooks)
#
# These operate on exactly one already-resolved file using already-loaded
# config. They never scan the inbox, glob, or call the batch runners, and they
# execute no transform_hooks / load_hooks. Used by the rey_loader single-file
# workflow (etl_operation / validate) against ``ctx.current_file``.
# ---------------------------------------------------------------------------

def validate_one(file_path: Path, transform_cfg: Any) -> bool:
    """Validate one file's header against its transform config."""
    return _validate_header(file_path, transform_cfg)


def transform_one(ctx: Any, run_log, data_source: Any, file_path: Path) -> bool:
    """Transform exactly one file. No discovery, no hooks.

    Matches ``file_path`` against the data source's candidate transforms,
    rejects it (per movement config) when no header matches, and otherwise runs
    the existing single-file transform. Returns True on success.
    """
    transforms = _coerce_transform_cfgs(data_source.transforms)
    matched_cfg, header_line = _match_transform(file_path, transforms)
    if matched_cfg is None or header_line is None:
        _logger.error(
            "No header match across %d transform(s) — file rejected: %s",
            len(transforms), file_path.name,
        )
        log_validation_result(run_log,
            validation_name="transform_header",
            status="failed",
            message=f"No header match across {len(transforms)} transform(s)",
            path=str(file_path),
            data_source=getattr(data_source, "name", ""),
        )
        _reject_unmatched_file(data_source, transforms, file_path)
        return False
    object.__setattr__(ctx, "transforms", matched_cfg)
    return _transform_one_file(ctx, run_log, data_source, matched_cfg, file_path,
                               header_line=header_line)


def load_file_to_table(
    ctx: Any,
    run_log: Any,
    file_path: Path,
    destination: str,
    connection: str,
    *,
    create_destination: bool = False,
    file_type: str = "",
    encoding: str = "utf-8-sig",
) -> int:
    """Load one named file into one named table. No configuration at all.

    The direct answer to "load this file into this table over this
    connection", which for a long time could not be given without first
    authoring a ``data_sources:`` block.

    **It builds the same object graph a configured feed uses** -- a
    ``DataFile`` for the format, an ``IdentityTransform``, a ``DataLoader``
    for the destination -- and runs the same pipeline. It is a different way
    of CONSTRUCTING the graph, not a second implementation of it.

    What a direct load does not have, and does not pretend to:

    - **no configured columns**, so the schema is inferred from the records;
    - **no movements**, because nothing was picked up from an inbox and the
      caller's file is left where they put it;
    - **no transform**, because there is no conversion step ahead of it.

    Args:
        ctx: Application context, for logging and any configured widening.
        run_log: The run's evidence recorder.
        file_path: The file to load.
        destination: ``schema.table``, or ``database.schema.table`` where the
            backend qualifies that way.
        connection: The CONFIGURED CONNECTION NAME the destination lives on.
            A name, not a handle: the target is built from it and the handle
            is opened from the target, so one value says where the object is
            and nothing reads it twice. Required -- an identity without a
            connection is a partial endpoint.
        create_destination: Whether an absent table may be created from the
            file. False means it must already exist.
        file_type: Declared format. Empty infers it from the suffix, which
            REFUSES a suffix naming no known format -- which is why this
            parameter exists.
        encoding: How to decode the file. A library parameter with the
            estate's default; no CLI exposes it.

    Returns:
        Rows loaded.
    """
    target = _destination_identity(destination, connection)
    load_name = ".".join(
        part for part in (target.catalog, target.schema, target.name) if part
    )

    def _load_one(source: Any, transform: Any, target_: Any,
                  **context: Any) -> int:
        # movements=None: a direct load has no movement policy, which is not
        # the same as one that moves nothing.
        return _load_one_file(
            source, transform, target_,
            ctx=ctx, movements=None, load_name=load_name, **context,
        )

    return _ConfiguredLoad(
        load_one_file=_load_one,
        target=target,
        explicit_files=[Path(file_path)],
        # None, not a stand-in config object. The builder reads a transform
        # config with getattr, so absent configuration produces no transform
        # map and no declared columns -- which is exactly what a direct load
        # means. Going through the builder rather than around it is what
        # leaves ONE construction site for an IdentityTransform.
        transform=_build_identity_transform(None),
        loader=_build_data_loader(ctx, create_destination),
        file_type=file_type,
        encoding=encoding,
        name=f"direct:{load_name}",
    ).load(run_log)


def load_one(ctx: Any, run_log, data_source: Any, load_cfg: Any, file_path: Path) -> int:
    """Load exactly one file into its destination table. No discovery, no hooks.

    Resolves the load connection, destination schema/table, and matching
    transform from config, then runs the existing single-file load. Returns the
    number of rows loaded. Fails closed on a missing/unknown connection.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    conn_name = getattr(getattr(load_cfg, "load", None), "connection", None)
    if not conn_name:
        raise ConfigError(
            f"load.connection is not set for load '{getattr(load_cfg, 'name', '?')}' "
            f"in data source '{getattr(data_source, 'name', '?')}'."
        )
    # The connection name is CHECKED here and resolved nowhere here: the
    # target carries it, and the per-file step opens one from that. Resolving
    # a handle to pass down would be a second reading of the same name.
    #
    # Through a ConfiguredLoad like every other caller, so this is the same
    # object graph a discovered load runs -- one file selected explicitly
    # rather than found by pattern. Calling the per-file step directly would
    # be the second construction path this step exists to remove.
    return _configured_load(
        ctx, run_log, data_source, load_cfg, explicit_files=[file_path],
    ).load(run_log)


def run_load(
    ctx: Any, run_log,
    sql_dir: Optional[Path] = None,
) -> int:
    """
    Run the load stage for every data source and load config declared in ctx.

    Iterates ``ctx.data_sources`` and each ``loads`` entry. For each load
    config the connection named by ``load_cfg.load.connection`` is resolved
    from the shared connections and reused — no connection
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
        - ``ctx.connections`` — the configured connections, which the runtime
          connection owner resolves objects from by name

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

                conn_name = getattr(getattr(load_cfg, "load", None), "connection", None)
                if not conn_name:
                    raise ConfigError(
                        f"load.connection is not set for load '{load_cfg.name}' "
                        f"in data source '{data_source.name}'. "
                        "Add 'connection: <name>' under the load: section in YAML."
                    )

                if conn_name not in open_conns:
                    open_conns[conn_name] = shared_connection(ctx, conn_name).handle()
                    _logger.debug("Using shared connection '%s'", conn_name)

                conn = open_conns[conn_name]
                last_conn = conn
                rows = load_files(ctx, run_log, conn, data_source, load_cfg)
                total += rows

            # post_load_sql runs on the last connection used for this source
            # (backward-compat with existing post_load_sql YAML key).
            if last_conn is not None:
                _execute_post_load_sql(ctx, run_log, last_conn, data_source, sql_dir)

        finally:
            # Nothing is closed here. These are shared Connections held by every
            # other consumer of the same name; closing one at the end of a data
            # source would take the handle from all of them. Their lifetime
            # belongs to runtime shutdown.
            open_conns.clear()

    return total


def _execute_post_load_sql(ctx: Any, run_log, conn: Any, data_source: Any, sql_dir: Optional[Path]) -> None:
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
        execute_sql_text(
            ctx,
            run_log, conn,
            sql_text,
            sql_path=str(sql_path),
            sql_label=sql_filename,
            operation="post_load_sql",
            safe_to_preview=True,
            data_source=getattr(data_source, "name", ""),
        )
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
    AIUnavailableError
        If this runtime has no AI configured.
    AISelectionError
        If the named profile is not one this runtime offers.
    """
    # Local import: the AI runtime is optional, and a loader hook that names no
    # AI must not make importing this module depend on one.
    from rey_lib.ai import (  # noqa: PLC0415
        AIInstruction,
        AIInstructionKind,
        AIRequest,
        AIRequestOptions,
    )
    from rey_lib.ai.errors import AIUnavailableError  # noqa: PLC0415

    ai = getattr(ctx, "shared_ai", None)
    if ai is None:
        raise AIUnavailableError(
            f"LLM hook '{llm_cfg.name}' needs AI, and this runtime has none "
            "configured."
        )

    llm_name      = getattr(llm_cfg, "llm", None) or ""
    system_prompt = getattr(llm_cfg, "system_prompt", None)
    max_tokens    = int(getattr(llm_cfg, "max_tokens", 500))
    template      = getattr(llm_cfg, "prompt_template", "") or ""

    prompt = _render_prompt(template, ctx, data_source)

    _logger.debug("LLM hook '%s': calling profile '%s'", llm_cfg.name, llm_name)
    response = ai.execute(
        AIRequest.prompt(
            prompt,
            profile_id=llm_name,
            instruction=(
                AIInstruction(kind=AIInstructionKind.RAW, text=str(system_prompt))
                if system_prompt else None
            ),
            options=AIRequestOptions(max_tokens=max_tokens),
        ),
    ).text
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
        open_conns[conn_name] = shared_connection(ctx, conn_name).handle()
        _logger.debug("Using shared connection '%s' for hook '%s'",
                      conn_name, sql_cfg.name)

    conn = open_conns[conn_name]
    hook_type = getattr(sql_cfg, "type", "procedure")
    row_columns: dict[str, Any] = {}

    if hook_type == "sql_file":
        _execute_one_hook_sql_file(ctx, run_log, conn, sql_cfg, sql_dir)

    else:
        # Default: type == "procedure"
        row_columns = _execute_one_hook_procedure(ctx, run_log, data_source, conn, sql_cfg)

    return row_columns


def _execute_one_hook_sql_file(
    ctx: Any, run_log,
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
    execute_sql_text(
        ctx,
        run_log, conn,
        sql_text,
        sql_path=str(sql_path),
        sql_label=str(sql_cfg.name),
        operation="hook_sql_file",
        safe_to_preview=True,
    )
    _logger.info("Hook sql_file executed: %s (config='%s')", file_name, sql_cfg.name)


def _execute_one_hook_procedure(
    ctx: Any, run_log,
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

    output_values = execute_procedure_call(
        ctx,
        run_log, conn,
        proc_name,
        named_inputs,
        output_specs,
        sql_label=str(sql_cfg.name),
        operation="hook_procedure",
        safe_to_preview=False,
    )


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
        # Committed but not closed: the commit is this work's transaction, the
        # connection is shared and outlives it.
        for conn in open_conns.values():
            try:
                conn.commit()
            except Exception:  # noqa: BLE001 — commit must never mask the error above
                pass
        open_conns.clear()

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
		with _db_adapter.get_connection(conn_cfg, ctx=ctx) as conn:
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
	ctx: Any, run_log,
	data_source: Any,
	transform_cfg: Any,
	file_path: Path,
	header_line: Optional[str] = None,
) -> bool:
    log_enter(ctx, f"_transform_one_file: {file_path.name}", _logger)

    try:
        if header_line is None and not _validate_header(file_path, transform_cfg):
            _logger.error("Header mismatch — file rejected: %s", file_path.name)
            log_validation_result(run_log,
                validation_name="transform_header",
                status="failed",
                message="Header mismatch",
                path=str(file_path),
                transform_name=getattr(transform_cfg, "name", ""),
                transform_version=getattr(transform_cfg, "version", ""),
            )
            _execute_movements(
                transform_cfg.movements.failure, file_path, data_source.paths,
                ctx=ctx, run_log=run_log,
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

        # Prepare Files (no columns): identify and canonical-rename only. Copy the
        # matched file byte-for-byte to its canonical name — never parse, transform,
        # or re-serialise rows. Transforms WITH columns fall through to the normal
        # row transformation below.
        if not getattr(transform_cfg, "columns", None):
            output_path = _build_output_path(
                data_source.paths, transform_cfg, file_path, ctx=ctx
            )
            copy_file(
                file_path,
                output_path.parent,
                dest_name=output_path.name,
                state_ctx=ctx,
                run_log=run_log,
                app=getattr(ctx, "app_name", "") if ctx is not None else "",
                pipeline=getattr(ctx, "pipeline_name", None) if ctx is not None else None,
                reason="prepared",
            )
            object.__setattr__(ctx, "step_record_count", 0)
            log_row_count(run_log,
                count_name="transformed_rows",
                count=0,
                subject=file_path.name,
                path=str(output_path),
            )
            _logger.info(
                "Prepared (byte copy): %s → %s", file_path.name, output_path.name
            )
            log_artifact_reference(run_log, str(output_path), role="prepared",
                artifact_group="output_files",
                artifact_type="prepared_file", source_path=str(file_path),
                viewer_type="file", safe_to_preview=True,
            )
            movements = getattr(transform_cfg, "movements", None)
            if movements is not None and getattr(movements, "success", None):
                _execute_movements(movements.success, file_path, data_source.paths,
                                   ctx=ctx, run_log=run_log)
            log_exit(ctx, f"_transform_one_file prepared: {file_path.name}", _logger)
            return True

        rows, errors = _read_and_transform(
            file_path,
            transform_cfg,
            header_line=header_line,
            ctx=ctx,
        )

        if errors:
            log_validation_result(run_log,
                validation_name="transform_rows",
                status="failed",
                message=f"{len(errors)} transform error(s)",
                path=str(file_path),
                error_count=len(errors),
            )
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
                transform_cfg.movements.failure, file_path, data_source.paths,
                ctx=ctx, run_log=run_log,
            )
            log_exit(
                ctx,
                f"_transform_one_file rejected (errors): {file_path.name}",
                _logger,
            )
            return False

        if not rows:
            _logger.warning("No rows produced from file: %s", file_path.name)
            log_validation_result(run_log,
                validation_name="transform_rows",
                status="failed",
                message="No rows produced",
                path=str(file_path),
            )
            _execute_movements(
                transform_cfg.movements.failure, file_path, data_source.paths,
                ctx=ctx, run_log=run_log,
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
            run_log=run_log,
            app=getattr(ctx, "app_name", "") if ctx is not None else "",
            pipeline=getattr(ctx, "pipeline_name", None) if ctx is not None else None,
            reason="transformed",
        )

        # Write row count to ctx so post_file_transform hooks (e.g. end_batch_step)
        # can stamp RecordCount on the BatchStep row.
        object.__setattr__(ctx, "step_record_count", len(rows))
        log_row_count(run_log,
            count_name="transformed_rows",
            count=len(rows),
            subject=file_path.name,
            path=str(output_path),
        )

        _logger.info(
            "Transformed: %s → %s  rows=%d",
            file_path.name, output_path.name, len(rows),
        )
        log_artifact_reference(run_log, str(output_path), role="transformed",
            artifact_group="output_files",
            artifact_type="transformed_file", source_path=str(file_path),
            viewer_type="file", safe_to_preview=True,
        )

        _execute_movements(
            transform_cfg.movements.success, file_path, data_source.paths,
                ctx=ctx, run_log=run_log,
        )
        log_exit(ctx, f"_transform_one_file done: {file_path.name}", _logger)
        return True

    except _NON_FATAL_PIPELINE_ERRORS as exc:
        _logger.error(
            "Unexpected error transforming '%s': %s",
            file_path.name, exc, exc_info=True,
        )
        _log_loader_step_failure(
            ctx,
            run_log, exc,
            failed_step_id=getattr(transform_cfg, "name", "transform"),
            failed_step_name=getattr(transform_cfg, "name", "transform"),
            related_path=str(file_path),
        )
        _execute_movements(
            transform_cfg.movements.failure, file_path, data_source.paths,
                ctx=ctx, run_log=run_log,
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





def _reject_structure(
    ctx: Any,
    run_log: Any,
    structure_exc: DataFileStructureError,
    file_path: Any,
    schema: str,
    table: str,
    movements: Any,
    paths: Any,
) -> int:
    """Refuse one file for a structural fault, recording why.

    SHARED BY BOTH EXECUTION PATHS, so a file refused without being
    materialized produces the same evidence as one refused after being read.
    The validation NAME comes from the error, because only the check that ran
    knows what it was -- ``load_header`` and ``configured_columns`` both
    arrive here.

    Returns:
        0. A file fault is not a run fault.
    """
    _logger.error("%s — file rejected: %s", structure_exc, file_path.name)
    log_validation_result(run_log,
        validation_name=structure_exc.validation_name,
        status="failed",
        message=str(structure_exc),
        path=str(file_path),
        schema=schema,
        table=table,
    )
    _route_file(ctx, run_log, movements, "failure", file_path, paths)
    log_exit(ctx, f"_load_one_file rejected (structure): "
                  f"{file_path.name}", _logger)
    return 0


def _reject_empty(
    ctx: Any,
    run_log: Any,
    file_path: Any,
    schema: str,
    table: str,
    movements: Any,
    paths: Any,
) -> int:
    """Refuse one file that produced no rows.

    SHARED BY BOTH EXECUTION PATHS. The materialised path knows the source
    was empty because reading it produced nothing; the non-row path knows
    because the engine reported inserting nothing. Different evidence for the
    same fact, and the same refusal.

    Returns:
        0.
    """
    _logger.warning("No rows produced from file: %s", file_path.name)
    log_validation_result(run_log,
        validation_name="load_rows",
        status="failed",
        message="No rows produced",
        path=str(file_path),
        schema=schema,
        table=table,
    )
    _route_file(ctx, run_log, movements, "failure", file_path, paths)
    log_exit(ctx, f"_load_one_file rejected (empty): {file_path.name}", _logger)
    return 0


def _non_row_execution_possible(
    source: Any,
    transform: Any,
    loader: Any,
    conn: Any,
    exists: bool,
) -> bool:
    """Whether this load can be executed without materializing its records.

    **CAN THE EXECUTION ENVIRONMENT USE IT**, which is deliberately not "does
    the target's provider read the source". The latter is true of a file and
    false of database-to-database, where the source is already a relation and
    nothing reads a file at all.

    Four questions, and every one of them has to answer yes:

    1. **The transform offers an equivalent non-row form.** ``None`` means
       row-by-row only, which is every transform until it says otherwise.
       Asked of the transform because equivalence is its claim to make.
    2. **The source DECLARES its structure.** A header states every column in
       order before a row is read, so the file can be validated and its
       column order established without materializing it. A source that only
       exhibits its structure -- keys on each record, positions in each line
       -- cannot, and has nothing to offer here. This is a source primitive,
       not a format test: no name is checked.
    3. **The destination already exists.** Creating one needs a schema, and a
       schema is derived from records this path will not have. An absent
       destination is not a refusal of the load, only of this path.
    4. **The provider implements it.** Resolved from the provider module by
       ``supports_provider_capability``, so nothing declares support twice
       and a provider that lacks the function simply answers no.

    Returns:
        True when all four hold. False is never a failure -- it means the
        materialised path runs, which is the guaranteed one.
    """
    if transform is None or transform.execution_form() is None:
        return False
    if not getattr(source, "declares_structure", False):
        return False
    if not exists:
        return False
    return bool(
        loader.adapter.supports_provider_capability(conn, "insert_from_path")
    )


def _load_one_file(
    source: Any,
    transform: Any,
    target: Any,
    *,
    ctx: Any,
    run_log: Any,
    transform_cfg: Any = None,
    load_cfg: Any = None,
    paths: Any = None,
    loader: Any = None,
    movements: Any = None,
    load_name: str = "",
) -> int:
    """Transfer one source into one target, through one transform.

    **THE TRANSFER BOUNDARY.** Three domain inputs, and they are the whole
    contract:

        source     what is being read     a DataFile
        transform  what the records are   a DataTransform
        target     where they go          a DatabaseObjectIdentity

    Everything else is context, evidence or policy, and is keyword-only
    precisely so it cannot be mistaken for part of the contract.

    WHAT IS NOT HERE, and must not come back
    ----------------------------------------
    **No connection.** An open connection is not generic execution context --
    it is the runtime form of ONE KIND of data object. A file-to-file transfer
    has no meaningful connection, so a boundary that demanded one would not be
    symmetric however its parameters were spelled. It is resolved BELOW, from
    ``target.connection``, where a database is first actually needed.

    **No path, schema or table.** Those are the endpoints, and the endpoints
    are the objects. A second representation travelling alongside would mean
    the flattening was never removed, only joined.

    **Not a batch.** One source, one target. Selecting many files is
    ``ConfiguredLoad``'s, and a many-to-one feeder is a different abstraction
    that would need its own object.

    Args:
        source: The data object being read. Its own type settles how.
        transform: What the produced records contain.
        target: The data object being written, naming its configured
            connection rather than holding one.
        ctx: Application context, for logging and configured widening.
        run_log: The run's evidence recorder.
        transform_cfg: Declared columns, where a definition declares them.
        load_cfg: The definition, for its movements and create policy.
        paths: Where routed files go.
        loader: The destination mechanic.
        movements: Resolved movement policy. None means do not route, which
            is not the same as routing nowhere.
        load_name: What this load is called in evidence.

    Returns:
        Rows loaded, or 0 when this file was rejected. A file fault is 0; a
        run-level fault raises.
    """
    file_path = source.path
    # The evidence form of the destination, rendered once. Logs and run-log
    # rows have always named the object the way the adapter does; keeping that
    # means the identity gaining parts changes nothing anyone reads.
    schema, table = adapter_destination(target)
    # Named before the try so the handlers can ask whether there is anything
    # to roll back -- the connection is resolved inside, and a failure before
    # that point has opened nothing.
    conn: Any = None
    log_enter(ctx, f"_load_one_file: {file_path.name}", _logger)

    try:
        # RESOLVED, not re-read. A configured load supplies its definition's
        # movements; a DIRECT load has none, because nothing was picked up
        # from an inbox and there is nowhere to move it to. None means "do
        # not route", which is different from "route nowhere".
        if movements is None and load_cfg is not None:
            movements = getattr(load_cfg, "movements", None)
        if not load_name:
            load_name = str(getattr(load_cfg, "name", "") or "load")

        # ONE CONSTRUCTION PATH. Handed the definition's own objects where a
        # ConfiguredLoad is driving; building them only when called directly.
        # Re-reading the configuration here when it has already been read
        # would be a second graph that looks identical and can drift.
        if loader is None:
            loader = _build_data_loader(
                ctx, _create_destination_declared(load_cfg),
            )

        # THE CONNECTION IS DERIVED FROM THE TARGET, and this is the first
        # point a database is actually needed. It is resolved here rather than
        # handed in so that `target.connection` is the one source of truth --
        # nothing reads a configured connection name once the target exists,
        # so there is no second answer for a guard to check.
        conn = shared_connection(ctx, target.connection).handle()

        # Asked of the LOADER, which owns the policy, rather than re-read
        # from configuration. A direct load declares it as an argument and
        # has no config to read; reading config here would have refused its
        # own --create.
        create_declared = loader.create_destination

        # EXISTENCE ASKED ONCE, and its answer serves both decisions: how to
        # validate the file, and whether the destination must be created.
        # None means absent; [] would mean a table with no columns.
        expected_columns = loader.destination_columns(conn, target)
        exists = expected_columns is not None

        if not exists and not create_declared:
            # A misconfiguration, NOT a bad file -- so it raises rather than
            # running movements.failure. The file is fine and moving it to
            # rejected_path would strand a good file for a fault it did not
            # cause; every later file would fail identically anyway.
            raise ConfigError(
                f"load '{load_name}' requires destination "
                f"{schema}.{table}, and it does not exist. Set "
                f"create_destination_table: true under that load's 'load:' "
                f"block to have the loader create it."
            )

        # NO FORMAT BRANCH, and no format name either. Every source reaching
        # here is a data object; a format with no DataFile is refused
        # upstream, where the object would have been built. The
        # XLSX/DELIMITED_NO_HEADER exclusion that used to stand here is gone
        # -- one is migrated, the other is converted to CSV upstream.
        #
        # The file decides WHEN its shape is checked, because that depends on
        # where its column names live: a header is one line and is checked
        # before the rows, record keys only after, and a headerless file
        # states its width nowhere but in its rows. This step knows none of
        # that.
        if transform is None:
            transform = _build_identity_transform(transform_cfg)

        if _non_row_execution_possible(source, transform, loader, conn, exists):
            # THE SAME TWO CHECKS, from the structure the file declares
            # instead of from records. Neither is skipped and neither is
            # reimplemented: `validate` is the half of `read_validated` that
            # reads no rows, and the configured-columns rule is asked over
            # ordered names by both paths.
            try:
                source.validate(expected_columns)
                columns = transform.columns_for_names(source.source_structure())
            except DataFileStructureError as structure_exc:
                return _reject_structure(
                    ctx, run_log, structure_exc, file_path, schema, table,
                    movements, paths,
                )

            inserted = loader.load_from_path(conn, target, file_path, columns)
            if not inserted:
                # The source held no rows. Reported by the engine rather than
                # counted here, and refused exactly as an empty read is.
                return _reject_empty(
                    ctx, run_log, file_path, schema, table, movements, paths,
                )

            _logger.info(
                "Loaded: %s → %s.%s  rows=%d  (non-row execution)",
                file_path.name, schema, table, inserted,
            )
            log_row_count(run_log,
                count_name="loaded_rows",
                count=inserted,
                subject=file_path.name,
                path=str(file_path),
                schema=schema,
                table=table,
            )
            _route_file(ctx, run_log, movements, "success", file_path, paths)
            log_exit(ctx, f"_load_one_file done: {file_path.name}", _logger)
            return inserted

        try:
            # ALWAYS VALIDATED, but against different things.
            #
            #   a destination  -> does this file match the table?
            #   None (absent)  -> is this file coherent with ITSELF?
            #
            # The second is what the create path never had. An absent
            # destination used to mean no check at all, so an inconsistent
            # file had its table created from the first record and then
            # failed inside the insert -- a database error for what is a
            # file defect, raised after DDL.
            rows = source.read_validated(expected_columns)
        except DataFileStructureError as structure_exc:
            return _reject_structure(
                ctx, run_log, structure_exc, file_path, schema, table,
                movements, paths,
            )

        if not rows:
            return _reject_empty(
                ctx, run_log, file_path, schema, table, movements, paths,
            )

        # The transform says what the produced records contain. On the load
        # path it is IDENTITY -- the transform stage ran earlier and wrote its
        # output to this file -- but it is explicit rather than absent, so the
        # pipeline is one shape whether or not a transform is configured.
        rows        = transform.transform(rows)
        column_defs = transform.logical_schema(rows)
        columns     = [name for name, _sql_type in column_defs]

        # The destination half: the create policy, the insert and the
        # truncation retry. Told what existence check already found, so the
        # destination is inspected once per file rather than once per
        # decision.
        loader.load(conn, target, rows, column_defs, expected_columns)

        _logger.info(
            "Loaded: %s → %s.%s  rows=%d",
            file_path.name, schema, table, len(rows),
        )
        log_row_count(run_log,
            count_name="loaded_rows",
            count=len(rows),
            subject=file_path.name,
            path=str(file_path),
            schema=schema,
            table=table,
        )

        _route_file(ctx, run_log, movements, "success", file_path, paths)
        log_exit(ctx, f"_load_one_file done: {file_path.name}", _logger)
        return len(rows)

    except DatabaseError as exc:
        # Only if one was opened. The connection is resolved inside the try
        # now, so a failure before that point has nothing to undo -- and
        # calling rollback on nothing would replace a real database error
        # with an AttributeError.
        #
        # THE SAME HAZARD, ONE LEVEL DOWN: a provider whose statements are
        # already atomic has no transaction open to undo, and answers the
        # call by raising -- DuckDB says "cannot rollback - no transaction is
        # active". Undoing nothing is exactly what was wanted there, so the
        # refusal is not a failure and must not be allowed to replace the
        # error being reported. The original exception is what the run log
        # and the operator need.
        if conn is not None:
            try:
                conn.rollback()
            except Exception as rollback_exc:   # noqa: BLE001 -- see above
                _logger.debug(
                    "Nothing to roll back for '%s': %s",
                    file_path.name, rollback_exc,
                )
        _logger.error(
            "Database error loading '%s' — rolled back: %s",
            file_path.name, exc,
        )
        _log_loader_step_failure(
            ctx,
            run_log, exc,
            failed_step_id=load_name,
            failed_step_name=load_name,
            related_path=str(file_path),
        )
        _route_file(ctx, run_log, movements, "failure", file_path, paths)
        log_exit(ctx, f"_load_one_file failed: {file_path.name}", _logger)
        return 0


# ---------------------------------------------------------------------------
# Private — column helpers
# ---------------------------------------------------------------------------

def _configured_columns(transform_cfg: Any) -> Optional[list[str]]:
    """Return the declared output columns in order, or None when none are.

    **Three answers, and the third is a refusal.**

    ``None``
        No ``columns:`` block. A load that declares no schema -- the direct
        single-file path is one -- and the records decide their own shape.
    ``[...]``
        The current LIST shape. These are the columns, in this order.
    *raises*
        A ``columns:`` block in any other shape. A config that declares a
        schema the loader cannot read is WRONG, and says so.

    **Absent and unreadable are different answers and must not collapse.**
    They did: the older mapping shape iterated to plain strings, every entry
    was skipped as "not a dict", and a feed carrying 22 declared columns
    behaved exactly like one declaring none. Two feeds drifted that way
    unnoticed.

    Raises:
        ConfigError: When ``columns:`` is present in an unsupported shape.
    """
    declared = _namespace_to_plain(getattr(transform_cfg, "columns", None))

    if declared is None:
        return None

    if not isinstance(declared, list):
        raise ConfigError(
            f"transform '{getattr(transform_cfg, 'name', '?')}' declares "
            f"columns: as a {type(declared).__name__}, which is not the "
            "supported shape. Each output column is one list entry: "
            "- name: <column>, with an optional source: and transform:."
        )

    names: list[str] = []
    for position, col_cfg in enumerate(declared, start=1):
        if not isinstance(col_cfg, dict) or not col_cfg.get("name"):
            raise ConfigError(
                f"transform '{getattr(transform_cfg, 'name', '?')}' has a "
                f"columns: entry at position {position} that is not a "
                f"mapping with a name: -- found {col_cfg!r}."
            )
        names.append(str(col_cfg["name"]))

    return names


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
    run_log: Any = None,
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
                    run_log=run_log,
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


#: Formats still read by the pre-DataFile path, and why each is still here.
#:
#: NOT a list of formats this module understands -- it is a list of formats
#: that cannot be migrated yet because neither has a working structural check
#: to preserve:
#:
#:   XLSX                 _validate_load_header reads a ZIP container as
#:                        comma-delimited text, so a valid workbook never
#:                        matches its own destination.
#:   DELIMITED_NO_HEADER  the delimited reader always takes the first line as
#:                        a header, so a headerless file loses its first row.
#:
#: Both carry defect rows. **This set goes away when they are fixed** and the
#: subtypes join the hierarchy; it is the one format distinction left in this
#: module and it is deliberately not a behavioural branch about how to read.
def supported_file_types() -> list[str]:
    """Every format a load accepts today, sorted.

    **The registry, and nothing beside it.** This used to be the registry
    UNION a deny-list of formats the pre-DataFile path still read. That path
    is gone: only a data object crosses the load boundary, so a format with no
    DataFile is not loadable and must not be offered as if it were.

    XLSX is the format that left. It is not lost -- it is CONVERTED upstream,
    by ``convert_workbook_to_csv`` and file_operator's excel conversion, and
    the CSV that produces loads like any other. What changed is that the
    loader stopped pretending to read a workbook it could not structurally
    check.

    **Here rather than in ``data_file``, and that is a dependency fact, not a
    preference.** ``file_loader`` reaches into ``data_file``; the reverse
    direction is empty and was emptied deliberately. An accessor there would
    create the first edge the wrong way.

    Returns:
        The accepted tokens, uppercase and sorted.
    """
    return sorted(registered_formats())


def _create_destination_declared(load_cfg: Any) -> bool:
    """Whether this load declares that its destination may be created.

    ``loads[].load.create_destination_table``, registered beside ``connection``
    and ``destination_table`` in rey_loader's configuration reference.

    **Absent means false**, which is what the loader already did: an absent
    destination returned no columns, every shape check failed against them and
    the file was rejected before creation was ever reached. So a config that
    says nothing keeps behaving exactly as it does today, and creating a table
    is something a data source has to ask for.

    A value that is not a boolean is REFUSED rather than read for its
    truthiness. This governs whether the loader issues DDL, and ``"false"`` as
    a quoted string is truthy in Python -- silently creating a table for a
    config that spelled out the opposite.

    Args:
        load_cfg: The load Namespace.

    Returns:
        Whether creation is declared.

    Raises:
        ConfigError: If the key is present and is not a boolean.
    """
    declared = getattr(getattr(load_cfg, "load", None),
                       "create_destination_table", None)
    if declared is None:
        return False
    if not isinstance(declared, bool):
        raise ConfigError(
            f"load '{getattr(load_cfg, 'name', '?')}' declares "
            f"create_destination_table: {declared!r}, which is not true or "
            "false."
        )
    return declared


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
