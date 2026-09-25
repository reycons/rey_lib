"""The file pipeline: transforming files, and routing them as it goes.

Two things live here, and LOADING IS NO LONGER ONE OF THEM.

transform_files
    Read raw files from inbox_path, validate headers, apply the configured
    column transforms, write clean output to processing_path, and move each
    source file per its configured movements.
load_files_to_callback
    Hand every pending file's rows to a caller that does its own thing with
    them. It names no destination, which is why it is a file feeder and not a
    load.

**Where the load went.** ``rey_lib/load/`` -- the operation between two data
objects, which is not a file concern and was only ever here because the first
thing ever loaded was a file. ``load_files``, ``load_one``, ``run_load`` and
``load_file_to_table`` are imported from there now, and nothing in this
package imports that one.

What this module still owns for the load, and hands over by name: the
movements a file is routed by, the paths and patterns a definition resolves,
and the step-failure record. They are public rather than underscored because a
name two packages use is not private.

On success  — commits and executes configured success movements.
On any error — rolls back, logs every row error, executes failure movements.

All configuration is driven by the YAML data source config — no table names,
schema names, column names, or folder paths are hardcoded here. This module
has no knowledge of any specific application, data model, or business rule.

All DB calls go through DBAdapter. All file moves go through file_utils. No
raw pyodbc or os calls anywhere in this module.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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
from rey_lib.files.data_file import registered_formats
from rey_lib.files.file_utils import (
    apply_file_movements,
    input_files,
    pattern_to_glob,
    get_reader,
    move_file,
    copy_file,
    write_file,
)
from rey_lib.data.column_transform import ColumnTransform
from rey_lib.data.errors import TransformError
from rey_lib.files.transformer import (
    keyed_record,
    match_header,
    parse_date_from_filename,
)

# Module-level DBAdapter instance. All DB calls in this module go through
# the adapter, which dispatches to the right backend implementation based
# on each connection config's `provider` field. The file pipeline never
# imports a backend driver directly — that knowledge lives in the adapter.
_db_adapter = DBAdapter()

__all__ = [
    # The file pipeline itself.
    "transform_files",
    "transform_one",
    "run_transform",
    "validate_one",
    "load_files_to_callback",
    "supported_file_types",
    # What the load operation calls back into. Public because two packages
    # use them, which is the definition of not private.
    "execute_movements",
    "log_loader_step_failure",
    "namespace_to_plain",
    "resolve_ctx_path",
    "resolve_ctx_tokens",
    "resolve_path",
    "resolve_pattern",
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

        inbox_dir = resolve_path(data_source.paths, "inbox_path", ctx=ctx)

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


def log_loader_step_failure(
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
            execute_movements(failure, file_path, data_source.paths)
            return

    rejected_path = getattr(data_source.paths, "rejected_path", None)
    if not rejected_path:
        _logger.info(
            "No rejected_path configured — leaving unmatched file in place: %s",
            file_path.name,
        )
        return

    rejected_dir = resolve_path(data_source.paths, "rejected_path")
    move_file(file_path, rejected_dir)














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

        source_dir = resolve_path(data_source.paths, "converted_path")
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
                execute_movements(load_cfg.movements.success, file_path, data_source.paths,
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
                execute_movements(load_cfg.movements.failure, file_path, data_source.paths,
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










# ---------------------------------------------------------------------------
# Private — sql_config hook execution
# ---------------------------------------------------------------------------

















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
            execute_movements(
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
                execute_movements(movements.success, file_path, data_source.paths,
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
            execute_movements(
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
            execute_movements(
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

        execute_movements(
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
        log_loader_step_failure(
            ctx,
            run_log, exc,
            failed_step_id=getattr(transform_cfg, "name", "transform"),
            failed_step_name=getattr(transform_cfg, "name", "transform"),
            related_path=str(file_path),
        )
        execute_movements(
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

    output_dir = resolve_path(paths, output_path_key, ctx=ctx)
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













# ---------------------------------------------------------------------------
# Private — column helpers
# ---------------------------------------------------------------------------





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
        Application context, held by the transform object it builds.

    Returns
    -------
    tuple[list[dict], list[tuple[int, str, str]]]
        (rows, errors) where errors are (row_num, column_name, message).
    """
    file_type = getattr(transform_cfg, "file_type", "CSV")
    encoding  = getattr(transform_cfg, "encoding",  "utf-8-sig")
    delimiter = getattr(transform_cfg, "delimiter", ",")

    cfg_dict = _normalized_transform_config(transform_cfg, ctx=ctx)
    # Resolve env-var keys for any encrypt transforms — done once per file.
    secrets = build_secrets(cfg_dict)

    # ONE TRANSFORM OBJECT FOR THIS FILE, built before the rows are read. The
    # declaration, the context and the secrets are its dependencies and are
    # settled here; what it is given per row is a record and nothing else.
    #
    # Secrets are handed to it rather than left in `cfg_dict` for it to find:
    # they are resolved from the environment by this function and are not part
    # of the declaration, whoever wrote that declaration.
    transform = ColumnTransform(cfg_dict, context=ctx, secrets=secrets)

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
            # KEYED AT THE FILE BOUNDARY. A positional or fixed-width row is
            # a list or a line, and the transform names its sources by key --
            # so where a field sits in THIS format is answered here, by the
            # module that reads files, and never inside the transform.
            out_row = transform.transform_record(keyed_record(raw_row, cfg_dict))
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

def execute_movements(
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
            dest_dir = resolve_path(paths, move.to, ctx=ctx)
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

    base_dir = resolve_path(paths, from_key)
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


def resolve_path(paths: Any, key: str, ctx: Any = None) -> Path:
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


def resolve_pattern(pickup_pattern: str, version: str, ctx: Any = None) -> str:
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


def resolve_ctx_path(ctx: Any, dotted_path: str) -> Any:
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
    cfg_dict = namespace_to_plain(transform_cfg)
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


def namespace_to_plain(value: Any) -> Any:
    """
    Recursively convert Namespace-like values to plain Python containers.

    The config loader returns Namespace objects for mappings. The transformer
    expects normal dict/list structures, especially for nested transform rules.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: namespace_to_plain(val) for key, val in value.items()}
    if isinstance(value, list):
        return [namespace_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(namespace_to_plain(item) for item in value)
    if hasattr(value, "items"):
        return {key: namespace_to_plain(val) for key, val in value.items()}
    return value


def _normalized_columns(columns_cfg: Any) -> list[dict[str, Any]]:
    """Validate and return native list-based column definitions."""
    columns = namespace_to_plain(columns_cfg)

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

def build_secrets(cfg_dict: dict[str, Any]) -> dict[str, str]:
    """
    Resolve env-var values for all encrypt transforms in this declaration.

    **PUBLIC, because it is the trusted half of applying an `encrypt` rule.**
    A declaration NAMES an environment variable; this reads it. Any caller
    building a transform object has to resolve secrets the same way, or an
    ad-hoc load would silently have none -- so there is one resolver rather
    than one per construction site.

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
