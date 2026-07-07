"""
Logging configuration and helpers.

All logging setup is centralised here. No module outside log_utils.py
may call logging.basicConfig(), add handlers, or configure formatters directly.

Log level defaults to INFO unless ctx.log_level specifies otherwise. The active
level is written to ctx.log_level after setup_logging() runs so all modules can
read it from ctx.

Log messages use ctx.log_depth to indent output, reflecting the call
hierarchy. log_enter() increments the depth on function entry; log_exit()
decrements it on exit.

Public API
----------
setup_logging(ctx, operation)   Configure logging for the application.
get_logger(name)                Return a named logger for use in modules.
log_enter(ctx, msg, logger)     Log function entry and increment ctx.log_depth.
log_exit(ctx, msg, logger)      Log function exit and decrement ctx.log_depth.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.logs.jsonl_handler import JsonlHandler

__all__ = [
    "setup_logging",
    "add_jsonl_handler",
    "get_logger",
    "log_file_metadata",
    "log_enter",
    "log_exit",
    "format_jsonl_records",
    "project_run_log",
    "read_jsonl_records",
    "read_run_log_sections",
    "resolve_run_identity",
    "open_run_log",
    "log_run_record",
    "log_run_start",
    "log_step_start",
    "log_step_end",
    "log_run_complete",
    "log_run_summary",
    "log_artifact_reference",
    "log_artifact_manifest",
    "EXECUTION_RECORD_TYPES",
    "RUN_RESULT_RECORD_TYPES",
]


def resolve_run_identity(ctx: Any) -> None:
    """
    Ensure the runtime context carries the standard run identity fields
    (SGC_Rey_Run_ID_Standard), created once, before logging starts.

    Sets three fields on ``ctx`` when absent and leaves existing values untouched so
    the identity is stable for the whole execution:

    - ``run_id``         : UUID string — the authoritative execution identity.
    - ``run_timestamp``  : ``YYYYMMDD_HHMMSS`` — human-readable, filename-safe,
      time-sortable; used for artifact filenames and operator display.
    - ``run_started_at`` : ISO-8601 start time with timezone offset — the full
      timestamp preserved separately from the filename-safe id.

    The timestamp is taken from local system time made timezone-aware, so the offset
    is recorded even when no runtime timezone is configured. Identity (``run_id``)
    and display (``run_timestamp``) are intentionally separate.

    Parameters
    ----------
    ctx : Any
        Application context, mutated in place.

    Returns
    -------
    None
    """
    if not getattr(ctx, "run_id", None):
        ctx.run_id = str(uuid.uuid4())
    if not getattr(ctx, "run_timestamp", None):
        started = datetime.now().astimezone()
        ctx.run_timestamp = started.strftime("%Y%m%d_%H%M%S")
        ctx.run_started_at = started.isoformat()


# ---------------------------------------------------------------------------
# Append-only typed run log (SGC_Rey_Workflow_Pipeline_Automatic_Control_Batch_Logging)
# ---------------------------------------------------------------------------

# Record schema version stamped on every run-log record.
_RUN_RECORD_SCHEMA_VERSION = 1

# The run log projects into three top-level groups
# (SGC_Rey_Log_Writer_Run_View_Groups): execution (what happened), files (what files
# were involved), and results (what is known after execution/review). File movement
# (FILE_OPERATION) is execution history, not artifact inventory, so it stays in the
# execution group. All groups are reconstructed from typed records in the same
# append-only file; the file is never rewritten.
EXECUTION_RECORD_TYPES = frozenset({
    "RUN_START", "STEP_START", "STEP_END", "INFO", "WARNING", "ERROR",
    "FILE_OPERATION", "RUN_COMPLETE",
})
RUN_RESULT_RECORD_TYPES = frozenset({
    "RUN_SUMMARY", "EMAIL_SUMMARY",
    "LLM_ANALYSIS_PACKAGE", "LLM_ANALYSIS_RESULT",
    "MANUAL_REVIEW", "POST_MORTEM",
})
# Files-group record types and their subgroup. Only created/generated run-owned
# files are artifacts; consumed inputs and run config/definition files are their
# own subgroups.
FILES_RECORD_SUBGROUP = {
    "INPUT_FILE_REFERENCE": "input_files",
    "CONFIG_FILE_REFERENCE": "config_files",
    "CONFIG_FILE_MANIFEST": "config_files",
    "ARTIFACT_REFERENCE": "artifacts",
    "ARTIFACT_MANIFEST": "artifacts",
}


def _record_group(record_type: str) -> str:
    """Map a record type to its top-level run-log group (execution/files/results)."""
    if record_type in FILES_RECORD_SUBGROUP:
        return "files"
    if record_type in RUN_RESULT_RECORD_TYPES:
        return "results"
    return "execution"


def open_run_log(ctx: Any) -> Path:
    """
    Establish and return the append-only run-log path for this execution.

    The run log is a run-created artifact named ``run_log.<run_timestamp>.jsonl``
    (SGC_Rey_Run_ID_Standard) beside the configured log directory. The path is
    resolved once and cached on ``ctx.run_log_path``; run identity is established
    first so ``run_id``/``run_timestamp`` exist before the first record. The logging
    layer names and writes its own run log (it cannot depend on files/file_utils).

    Parameters
    ----------
    ctx : Any
        Application context. Must have ``log_file`` set (by setup_logging) so the
        run-log directory is known; execution should not proceed without a durable
        log path.

    Returns
    -------
    Path
        The append-only run-log path.

    Raises
    ------
    ValueError
        If no durable log directory is available (fail closed).
    """
    existing = getattr(ctx, "run_log_path", None)
    if existing:
        return Path(existing)

    resolve_run_identity(ctx)
    log_file = getattr(ctx, "log_file", None)
    if not log_file:
        raise ValueError(
            "Cannot open run log: no durable log path (ctx.log_file). Configure "
            "logging before starting a run."
        )
    path = Path(log_file).parent / f"run_log.{ctx.run_timestamp}.jsonl"
    ctx.run_log_path = str(path)
    return path


def log_run_record(ctx: Any, record_type: str, *, message: str = "", **fields: Any) -> None:
    """
    Append one typed record to the append-only run log.

    Every record carries ``run_id`` (SGC_Rey_Run_ID_Standard) plus the record type,
    its logical group (execution vs run-result), a UTC timestamp, the owning app,
    and the workflow/pipeline name when known. This is the single, centralized entry
    point for run-log records — runners and the logging layer emit through here
    rather than writing the run log directly. Append failures are recorded to the
    standard logger and never mask execution.

    Parameters
    ----------
    ctx : Any
        Application context.
    record_type : str
        A record type (e.g. ``"RUN_START"``, ``"STEP_END"``, ``"RUN_SUMMARY"``).
    message : str
        Optional human-readable message.
    **fields : Any
        Additional typed fields merged into the record.

    Returns
    -------
    None
    """
    try:
        path = open_run_log(ctx)
        record: dict[str, Any] = {
            "record_type": record_type,
            "record_group": _record_group(record_type),
            "run_id": ctx.run_id,
            "run_timestamp": ctx.run_timestamp,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_schema_version": _RUN_RECORD_SCHEMA_VERSION,
        }
        subgroup = FILES_RECORD_SUBGROUP.get(record_type)
        if subgroup:
            record["record_subgroup"] = subgroup
        app = (getattr(ctx, "owner_app_name", None) or getattr(ctx, "app_name", None)
               or getattr(ctx, "name", None))
        if app:
            record["app"] = str(app)
        for key in ("workflow_name", "pipeline_name"):
            value = getattr(ctx, key, None)
            if value:
                record[key] = str(value)
        if message:
            record["message"] = message
        record.update(fields)

        # Route the durable append through the primitive I/O layer so the run-log
        # writer shares one low-level append with file_utils without either
        # foundational module importing the other (SGC_Rey_Lib_Primitive_File_IO_Layer).
        # Imported lazily because the rey_lib.files package eagerly loads file_utils,
        # which imports this logging layer — a module-level import would form a cycle.
        from rey_lib.files import primitive_file_io

        primitive_file_io.append_jsonl(path, record)
    except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
        logging.getLogger(__name__).warning(
            "run log: could not append %s record: %s", record_type, exc
        )


def log_run_start(ctx: Any, **fields: Any) -> None:
    """Append a RUN_START execution record marking the start of the run."""
    log_run_record(ctx, "RUN_START", **fields)


def log_step_start(ctx: Any, step_name: str, step_sequence: int,
                   step_type: str = "", **fields: Any) -> None:
    """Append a STEP_START execution record for one step."""
    log_run_record(
        ctx, "STEP_START",
        step_name=step_name, step_sequence=step_sequence, step_type=step_type,
        **fields,
    )


def log_step_end(ctx: Any, step_name: str, status: str, *,
                 message: str = "", **fields: Any) -> None:
    """Append a STEP_END execution record with the step status (success/failure/skipped)."""
    log_run_record(
        ctx, "STEP_END",
        step_name=step_name, status=status, message=message, **fields,
    )


def log_run_complete(ctx: Any, status: str, *, message: str = "", **fields: Any) -> None:
    """Append a RUN_COMPLETE execution record with the final run status."""
    log_run_record(ctx, "RUN_COMPLETE", status=status, message=message, **fields)


def log_run_summary(ctx: Any, summary: dict[str, Any]) -> None:
    """Append a deterministic RUN_SUMMARY run-result record (no LLM required)."""
    log_run_record(ctx, "RUN_SUMMARY", summary=summary)


def log_input_file_reference(ctx: Any, path: str, *, file_role: str = "",
                             display_name: str = "", consumed_by_step: str = "",
                             **fields: Any) -> None:
    """Append an INPUT_FILE_REFERENCE record (files/input_files) for a consumed input.

    Input files are files the run reads/consumes (source data, inbound files). They
    are not artifacts unless the run also writes a new run-owned output copy.
    """
    log_run_record(
        ctx, "INPUT_FILE_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        file_role=file_role, source="runtime", consumed_by_step=consumed_by_step,
        **fields,
    )


def log_config_file_reference(ctx: Any, path: str, *, file_role: str = "",
                              display_name: str = "", consumed_by_step: str = "",
                              **fields: Any) -> None:
    """Append a CONFIG_FILE_REFERENCE record (files/config_files) for a run config file.

    Config files define or influence the run (workflow/pipeline/app YAML, contracts,
    templates). They are recorded from resolved config/provenance so the console
    reads them from the log rather than rescanning YAML or the filesystem.
    """
    log_run_record(
        ctx, "CONFIG_FILE_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        file_role=file_role, source="config_provenance",
        consumed_by_step=consumed_by_step, **fields,
    )


def log_config_file_manifest(ctx: Any, files: list[dict[str, Any]]) -> None:
    """Append the consolidated CONFIG_FILE_MANIFEST record (files/config_files)."""
    log_run_record(ctx, "CONFIG_FILE_MANIFEST", files=files)


def log_file_operation(ctx: Any, operation: str, *, source_path: str = "",
                       target_path: str = "", status: str = "success",
                       step_id: str = "", **fields: Any) -> None:
    """Append a FILE_OPERATION execution record for a file movement/operation.

    File movement (move/copy/rename/read/delete) is execution history, not artifact
    inventory. These records carry enough detail (from/to/status) to support
    rollback/recovery analysis derived from the append-only log rather than state
    files.
    """
    log_run_record(
        ctx, "FILE_OPERATION",
        operation=operation, source_path=str(source_path),
        target_path=str(target_path), status=status, step_id=step_id, **fields,
    )


# ---------------------------------------------------------------------------
# Ambient current run (SGC_Rey_File_Utils_Ambient_Run_Log_File_Recording).
#
# One current run per process, owned here. Runners bind the run at start and
# clear it at end; file_utils records file operations against it without any ctx
# threading. When no run is bound, recording is a no-op. Process-scoped for P1;
# concurrent/nested runs are out of scope.
# ---------------------------------------------------------------------------

# Single-slot holder for the process-scoped current run. A mutable module-level
# container (rather than a rebound module global) keeps the binding writable from
# bind_run/clear_run without the ``global`` keyword.
_CURRENT_RUN: dict[str, Any] = {"run": None}


def bind_run(ctx: Any = None, *, run_log_path: str = "", run_id: str = "",
             run_timestamp: str = "") -> None:
    """Bind the current run so file_utils records file operations against it.

    Reads run_log_path / run_id / run_timestamp from ``ctx`` when given, else from
    the keyword arguments. Binding without a durable run_log_path is a no-op.
    """
    if ctx is not None:
        run_log_path = str(getattr(ctx, "run_log_path", "") or run_log_path)
        run_id = str(getattr(ctx, "run_id", "") or run_id)
        run_timestamp = str(getattr(ctx, "run_timestamp", "") or run_timestamp)
    if not run_log_path:
        return
    _CURRENT_RUN["run"] = SimpleNamespace(
        run_id=run_id, run_timestamp=run_timestamp, run_log_path=str(run_log_path),
    )


def clear_run() -> None:
    """Clear the current run (recording becomes a no-op until the next bind)."""
    _CURRENT_RUN["run"] = None


def current_run() -> dict[str, str] | None:
    """Return the bound run's {run_log_path, run_id}, or None if unbound."""
    run = _CURRENT_RUN["run"]
    if run is None:
        return None
    return {"run_log_path": run.run_log_path, "run_id": run.run_id}


def record_file_operation(operation: str, *, source_path: str = "",
                          target_path: str = "", status: str = "success",
                          **fields: Any) -> None:
    """Append a FILE_OPERATION to the bound run log, or no-op if no run is bound.

    Called by file_utils after a file operation; emission is fail-safe and never
    raises into the caller (a logging failure must not break a file operation).
    """
    run = _CURRENT_RUN["run"]
    if run is None:
        return
    try:
        log_file_operation(
            run, operation, source_path=source_path, target_path=target_path,
            status=status, **fields,
        )
    except Exception as exc:  # noqa: BLE001 — recording must never break a file op.
        logging.getLogger(__name__).warning(
            "run log: could not record file operation '%s': %s", operation, exc
        )


def log_artifact_reference(ctx: Any, path: str, *, role: str = "",
                           event: str = "created", created_by_step: str = "",
                           display_name: str = "", **fields: Any) -> None:
    """Append an ARTIFACT_REFERENCE record (files/artifacts) for a created artifact.

    Only created/generated/written/exported/reported files are artifacts. Moved,
    copied, renamed, read, or deleted files are FILE_OPERATION execution records,
    not artifacts.
    """
    log_run_record(
        ctx, "ARTIFACT_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        artifact_role=role, event=event, created_by_step=created_by_step, **fields,
    )


def log_artifact_manifest(ctx: Any, artifacts: list[dict[str, Any]]) -> None:
    """Append the consolidated ARTIFACT_MANIFEST record (files/artifacts) at completion."""
    log_run_record(ctx, "ARTIFACT_MANIFEST", artifacts=artifacts)


def log_artifact_manifest_from_run_log(ctx: Any) -> None:
    """Append a consolidated ARTIFACT_MANIFEST built from this run's own records.

    Collects the artifacts already recorded on the append-only run log for this run
    and appends a single consolidated ARTIFACT_MANIFEST (files/artifacts). It reads
    only the run log — it never rescans directories or infers artifacts from
    filenames — and includes only files/artifacts entries, i.e. created/generated
    outputs. Moved, read, and copied files are FILE_OPERATION execution records and
    are never included (SGC_Rey_Run_Artifact_Naming_Convention). Meant to run at run
    completion, after RUN_COMPLETE/RUN_SUMMARY. Emission is fail-safe.
    """
    try:
        path = getattr(ctx, "run_log_path", None)
        if not path:
            return
        artifacts = read_run_log_sections(path)["sections"]["files"]["artifacts"]["files"]
        if artifacts:
            log_artifact_manifest(ctx, artifacts)
    except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
        logging.getLogger(__name__).warning(
            "run log: could not append ARTIFACT_MANIFEST: %s", exc
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Indent unit applied per depth level.
_INDENT = "  "

# Explicit log level mapping.
_LEVEL_MAP: dict[str, int] = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
}


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

class _IndentFormatter(logging.Formatter):
    """
    Custom formatter that prepends indentation based on the current call depth.

    Depth is stored in a module-level variable updated by log_enter/log_exit
    and read at format time so every record reflects the current call depth.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record, indenting only the message portion.

        Timestamp, level, and module name remain left-aligned and fixed width.
        Only the message is indented to reflect call depth.
        """
        indent   = _INDENT * _current_depth
        asctime  = self.formatTime(record, self.datefmt)
        prefix   = f"{asctime}  {record.levelname:<8}  {record.name:<32}"
        return f"{prefix}{indent}{record.getMessage()}"


class _TimestampFilter(logging.Filter):
    """Stamp every LogRecord with a pre-computed ISO-8601 UTC timestamp."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True after setting record.timestamp to an ISO UTC string."""
        record.timestamp = datetime.fromtimestamp(  # type: ignore[attr-defined]
            record.created, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
        return True


class _ProviderWarningFilter(logging.Filter):
    """Promote provider back-pressure messages that libraries log too softly."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True after promoting known provider warning messages."""
        message = record.getMessage()
        if _is_too_many_requests_record(record.name, message):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


class _TextFileHandler(logging.StreamHandler):
    """Human-readable log handler whose file stream is opened by file_utils."""

    def __init__(self, path: Path) -> None:
        from rey_lib.files.file_utils import open_text_file

        self._rey_stream = open_text_file(path, "a", encoding="utf-8")
        super().__init__(self._rey_stream)

    def close(self) -> None:
        """Flush and close the file stream owned by this handler."""
        try:
            self.flush()
            self._rey_stream.close()
        finally:
            super().close()


def _is_too_many_requests_record(logger_name: str, message: str) -> bool:
    """Return True when a provider HTTP log record is a rate-limit response."""
    if logger_name != "httpx":
        return False
    return "429" in message and "Too Many Requests" in message


# Module-level depth mirror — kept in sync with ctx.log_depth.
_current_depth: int = 0


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_logging(ctx: Any, operation: str = "app") -> None:
    """
    Initialise logging for the application.

    Sets up handlers based on what is configured in ctx:
      - Console (stderr): always active, respects log level (live transport).
      - JSONL: active by default unless disabled in ctx.logging.jsonl_enabled
        or ctx.jsonl_enabled. This is the single durable execution log.
      - Human-readable file: opt-in legacy only (readable_enabled=true). New runs
        do not produce a separate human-readable execution log
        (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup); readable views are
        rendered on demand from the JSONL run log via the render_* helpers.

    Both path templates support two placeholders:
      {operation}  — the current operation name (e.g. 'scan', 'import')
      {timestamp}  — run start time as YYYYMMDD_HHMMSS

    When both are configured both handlers are active. When only one is
    configured only that handler is added. ctx.log_file is set to the
    JSONL path when present, otherwise the human-readable log path.

    The resolved log level is written back to ctx.log_level.

    Parameters
    ----------
    ctx : Any
        Application context Namespace. Optionally has .log_path and/or
        .jsonl_path. ctx.log_level and ctx.log_file are updated in-place
        after setup.
    operation : str
        Current operation name. Substituted into path templates.
        Defaults to 'app'.
    """
    global _current_depth

    level_name = getattr(ctx, "log_level", None) or "INFO"
    level      = _LEVEL_MAP.get(level_name.upper(), logging.INFO)

    fmt     = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    formatter = _IndentFormatter(fmt=fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addFilter(_TimestampFilter())

    # Remove any pre-existing handlers to avoid duplicate output.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    # Console handler — always present.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_ProviderWarningFilter())
    root.addHandler(console_handler)

    # Establish the run identity before any handler so run_id exists before the
    # first log record, and the log filename uses the stable run_timestamp
    # (SGC_Rey_Run_ID_Standard). The run log is a run-created artifact and follows
    # the same <name>.<run_timestamp>.<ext> convention as every other artifact.
    resolve_run_identity(ctx)
    timestamp = ctx.run_timestamp
    log_file  = None

    # The append-only JSONL run log is the single durable execution log
    # (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup). A separate human-readable
    # execution log is no longer produced for new runs; it is opt-in legacy only via
    # readable_enabled=true. When log_path is configured it is still resolved so the
    # JSONL log is written beside it, but the text handler is added only when opted in.
    resolved_log: Path | None = None
    if getattr(ctx, "log_path", None):
        resolved_log = _resolve_log_path(ctx.log_path, ctx, operation, timestamp)
        if _log_bool(ctx, "readable_enabled", False):
            file_handler = _TextFileHandler(resolved_log)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(_ProviderWarningFilter())
            root.addHandler(file_handler)
            log_file = str(resolved_log)

    if _log_bool(ctx, "jsonl_enabled", True):
        jsonl_path = _resolve_jsonl_path(ctx, operation, timestamp, resolved_log)
        # Every run log record carries the run identity so records can be correlated
        # to the run regardless of filename.
        jsonl_handler = JsonlHandler(
            jsonl_path = jsonl_path,
            context    = {
                "run_id":         ctx.run_id,
                "run_timestamp":  ctx.run_timestamp,
                "run_started_at": getattr(ctx, "run_started_at", ""),
            },
            ctx        = ctx,
            ctx_fields = tuple(getattr(ctx, "jsonl_ctx_fields", ())),
        )
        jsonl_handler.setLevel(level)
        jsonl_handler.addFilter(_ProviderWarningFilter())
        root.addHandler(jsonl_handler)
        log_file = str(jsonl_path)  # JSONL takes precedence for ctx.log_file

    ctx.log_level = level_name
    ctx.log_depth = getattr(ctx, "log_depth", 0)
    _current_depth = ctx.log_depth

    # ctx.log_file used by hooks (e.g. begin_batch) to record the log path.
    # JSONL path takes precedence when both are configured.
    if log_file:
        setattr(ctx, "log_file", log_file)


def _log_bool(ctx: Any, key: str, default: bool) -> bool:
    """Read a boolean logging option from ctx.logging first, then ctx."""
    logging_cfg = getattr(ctx, "logging", None)
    for source in (logging_cfg, ctx):
        if source is None:
            continue
        value = getattr(source, key, None)
        if value is not None:
            return _coerce_bool(value)
    return default


def _coerce_bool(value: Any) -> bool:
    """Return a conservative bool for YAML/env friendly values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _resolve_log_path(template: Any, ctx: Any, operation: str, timestamp: str) -> Path:
    """Resolve a configured log path template against ctx values."""
    return Path(
        str(template).format_map(_LogPathValues(ctx, operation, timestamp))
    ).expanduser().resolve()


def _resolve_jsonl_path(
    ctx: Any,
    operation: str,
    timestamp: str,
    resolved_log: Path | None,
) -> Path:
    """Resolve the authoritative JSONL path, defaulting beside readable logs."""
    if getattr(ctx, "jsonl_path", None):
        return _resolve_log_path(ctx.jsonl_path, ctx, operation, timestamp)
    if resolved_log is not None:
        return resolved_log.with_suffix(".jsonl")

    app_name = getattr(ctx, "app_name", None) or getattr(ctx, "name", None) or "app"
    template = f"~/logs/{app_name}/{app_name}.{{operation}}.{{timestamp}}.jsonl"
    return _resolve_log_path(template, ctx, operation, timestamp)


class _LogPathValues(dict[str, Any]):
    """Path format values backed by operation, timestamp, and ctx attrs."""

    def __init__(self, ctx: Any, operation: str, timestamp: str) -> None:
        super().__init__(operation=operation, timestamp=timestamp)
        self._ctx = ctx

    def __missing__(self, key: str) -> str:
        value = getattr(self._ctx, key, None)
        if value is None:
            return "unknown"
        return str(value)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger for use in an application module.

    All modules should obtain their logger via this function rather than
    calling logging.getLogger() directly.

    Parameters
    ----------
    name : str
        Logger name. Conventionally the module's __name__.

    Returns
    -------
    logging.Logger
        A configured logger instance.
    """
    return logging.getLogger(name)


def add_jsonl_handler(
    logger_name: str,
    jsonl_path: Path,
    *,
    context: dict[str, Any],
    ctx: Any = None,
    ctx_fields: tuple[str, ...] = (),
    level: int | None = None,
) -> JsonlHandler:
    """Attach a JSONL handler through the shared logging utility boundary."""
    handler = JsonlHandler(
        jsonl_path=jsonl_path,
        context=context,
        ctx=ctx,
        ctx_fields=ctx_fields,
    )
    if level is not None:
        handler.setLevel(level)
    handler.addFilter(_ProviderWarningFilter())
    get_logger(logger_name).addHandler(handler)
    return handler


def log_file_metadata(path: Path, jsonl_stems: set[str] | None = None) -> dict[str, Any]:
    """Return JSONL-authority metadata for one log file path."""
    log_type = "jsonl" if path.suffix == ".jsonl" else "readable"
    return {
        "log_type": log_type,
        "authoritative": log_type == "jsonl",
        "derived": log_type != "jsonl",
        "derived_from": _derived_jsonl_path(path, jsonl_stems or set()),
    }


def read_jsonl_records(
    path: Path,
    content: str,
    *,
    filters: dict[str, str] | None = None,
    max_records: int = 250,
    truncated_file: bool = False,
) -> dict[str, Any]:
    """Parse and filter authoritative JSONL log records."""
    if path.suffix != ".jsonl":
        return {
            "path": str(path),
            "records": [],
            "records_matched": 0,
            "records_returned": 0,
            "truncated_file": truncated_file,
            "parse_errors": [],
            "error": "Structured log records are available only for JSONL logs.",
            **log_file_metadata(path),
        }

    selected_filters = filters or {}
    records: list[dict[str, Any]] = []
    parse_errors: list[str] = []

    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        if _record_matches(record, selected_filters):
            records.append(record)

    limited_records = records[:max_records]
    return {
        "path": str(path),
        "records": limited_records,
        "records_matched": len(records),
        "records_returned": len(limited_records),
        "truncated_file": truncated_file,
        "parse_errors": parse_errors,
        "rendered_text": format_jsonl_records(limited_records),
        **log_file_metadata(path),
    }


_RUN_EXECUTION_TYPES = {
    "RUN_START",
    "STEP_START",
    "STEP_END",
    "INFO",
    "WARNING",
    "ERROR",
    "RUN_COMPLETE",
}
_RUN_RESULT_TYPES = {
    "RUN_SUMMARY",
    "EMAIL_SUMMARY",
    "LLM_ANALYSIS_PACKAGE",
    "LLM_ANALYSIS_RESULT",
}
_ARTIFACT_CREATE_EVENTS = {
    "created",
    "generated",
    "written",
    "exported",
    "reported",
}
_ARTIFACT_IGNORE_EVENTS = {
    "moved",
    "copied",
    "renamed",
    "read",
    "touched",
    "deleted",
}


def read_run_log_sections(path: Path | str) -> dict[str, Any]:
    """Read an append-only run log and return section projections.

    The returned payload contains metadata and structured record projections only.
    File content preview belongs to file utilities, not log utilities.
    """
    log_path = Path(path).expanduser().resolve()
    records: list[dict[str, Any]] = []
    parse_errors: list[str] = []

    try:
        content = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "path": str(log_path),
            "exists": log_path.exists(),
            "records": [],
            "sections": _empty_run_sections(),
            "parse_errors": [str(exc)],
            **log_file_metadata(log_path),
        }

    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        if isinstance(record, dict):
            records.append(record)

    return {
        "path": str(log_path),
        "exists": True,
        "records": records,
        "sections": _run_log_sections(records),
        "parse_errors": parse_errors,
        **log_file_metadata(log_path),
    }


def project_run_log(path: Path | str) -> dict[str, Any]:
    """Return a run-centered tree projection from one append-only run log."""
    sections_payload = read_run_log_sections(path)
    records = sections_payload["records"]
    sections = sections_payload["sections"]
    run = _run_log_identity(Path(sections_payload["path"]), records, sections)
    return {
        "run": run,
        "log": {
            "path": sections_payload["path"],
            "name": Path(sections_payload["path"]).name,
            "exists": sections_payload["exists"],
            "authoritative": sections_payload["authoritative"],
        },
        "sections": sections,
        "tree": _run_log_tree(run, sections),
        "parse_errors": sections_payload["parse_errors"],
    }


# Run sections addressable through the backend run API. The four files subgroups
# live under the "files" group; execution and results are top-level groups.
_RUN_SECTION_NAMES = (
    "execution",
    "input_files",
    "config_files",
    "file_operations",
    "artifacts",
    "results",
)
_RUN_FILE_SUBGROUPS = ("input_files", "config_files", "file_operations", "artifacts")


def run_summary(path: Path | str) -> dict[str, Any]:
    """Return one run's discovery summary — identity and counts, no raw records.

    This is the per-run row for run discovery (SGC_Rey_Run_Backend_Helper_API): the
    run identity, started/completed timestamps, status, warning/error counts, and
    the run-log path. It never returns raw log data.
    """
    payload = read_run_log_sections(path)
    identity = _run_log_identity(Path(payload["path"]), payload["records"], payload["sections"])
    return {
        "run_id": identity["run_id"],
        "run_timestamp": identity["run_timestamp"],
        "started_at": identity["run_started_at"],
        "completed_at": identity["run_completed_at"],
        "status": identity["status"],
        "warning_count": identity["warning_count"],
        "error_count": identity["error_count"],
        "app": identity["app"],
        "workflow": identity["workflow"],
        "pipeline": identity["pipeline"],
        "run_log_path": identity["log_path"],
    }


def discover_runs(log_dir: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Discover recent runs under a log directory, newest first.

    Scans ``run_log.<run_timestamp>.jsonl`` files in *log_dir* and returns one
    lightweight summary per run (see :func:`run_summary`) — never raw log records.
    This is the run-discovery authority for the console backend
    (SGC_Rey_Run_Backend_Helper_API); the console must not scan directories itself.

    Parameters
    ----------
    log_dir : Path | str
        Directory holding a workflow/pipeline's run logs.
    limit : int
        Maximum number of runs to return (most recent first). 0 means no limit.

    Returns
    -------
    list[dict[str, Any]]
        Run summaries sorted by run_timestamp descending.
    """
    directory = Path(log_dir).expanduser()
    if not directory.is_dir():
        return []
    summaries = [run_summary(path) for path in directory.glob("run_log.*.jsonl")]
    summaries.sort(key=lambda run: str(run.get("run_timestamp") or ""), reverse=True)
    return summaries[:limit] if limit else summaries


def get_run_section(path: Path | str, section: str) -> dict[str, Any]:
    """Return the records/files for one projected run section.

    ``section`` is one of execution, input_files, config_files, file_operations,
    artifacts, or results (SGC_Rey_Run_Backend_Helper_API). The projection comes
    from the append-only run log; no directory scan or filename inference occurs.

    Raises
    ------
    ValueError
        If ``section`` is not a known run section.
    """
    key = str(section or "").strip().lower()
    if key not in _RUN_SECTION_NAMES:
        raise ValueError(f"Unknown run section: {section!r}")
    sections = read_run_log_sections(path)["sections"]
    payload = sections["files"][key] if key in _RUN_FILE_SUBGROUPS else sections[key]
    return {"section": key, **payload}


def get_run_file_reference(path: Path | str, file_path: Path | str) -> dict[str, Any] | None:
    """Return the run-log reference entry for one file, or None if the run never used it.

    Looks the file up among the run's projected input/config/artifact/file-operation
    entries — the run log is the source of truth for which files belong to a run
    (SGC_Rey_Run_Backend_Helper_API). This returns the log-derived reference metadata
    (role, display name, owning section, actions); reading/previewing the file's
    contents is file_utils' responsibility, not this layer's.
    """
    targets = {str(file_path), str(Path(file_path).expanduser())}
    files = read_run_log_sections(path)["sections"]["files"]
    for key in _RUN_FILE_SUBGROUPS:
        for entry in files[key]["files"]:
            if str(entry.get("path")) in targets:
                return {"section": key, **entry}
    return None


def _empty_run_sections() -> dict[str, Any]:
    """Return the empty three-group projection (SGC_Rey_Log_Writer_Run_View_Groups)."""
    return {
        "execution": {"records": [], "count": 0},
        "files": {
            "input_files": {"records": [], "files": [], "count": 0},
            "config_files": {"records": [], "files": [], "count": 0},
            "file_operations": {"records": [], "files": [], "count": 0},
            "artifacts": {"records": [], "files": [], "count": 0},
            "count": 0,
        },
        "results": {"records": [], "count": 0},
    }


def _run_log_sections(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Project typed run-log records into execution/files/results groups.

    File movement (FILE_OPERATION) stays in execution and is additionally surfaced
    as a file-centric ``files.file_operations`` view. Only created/generated files
    become artifacts; moved/copied/read files never do.
    """
    sections = _empty_run_sections()
    files = sections["files"]
    for record in records:
        record_type = str(record.get("record_type") or record.get("event_type") or "").upper()
        record_group = str(record.get("record_group") or "").lower()

        if record_type in _RUN_EXECUTION_TYPES or record_group == "execution":
            sections["execution"]["records"].append(record)

        if record_type in _RUN_RESULT_TYPES or record_group in ("results", "run_result"):
            sections["results"]["records"].append(record)

        if record_type == "INPUT_FILE_REFERENCE":
            files["input_files"]["records"].append(record)
            entry = _file_entry_from_record(record, "input")
            if entry:
                files["input_files"]["files"].append(entry)
        elif record_type in ("CONFIG_FILE_MANIFEST", "RELEVANT_FILE_MANIFEST"):
            files["config_files"]["records"].append(record)
            files["config_files"]["files"].extend(_manifest_files(record))
        elif record_type in ("CONFIG_FILE_REFERENCE", "RELEVANT_FILE"):
            files["config_files"]["records"].append(record)
            entry = _file_entry_from_record(record, "config")
            if entry:
                files["config_files"]["files"].append(entry)
        elif record_type == "FILE_OPERATION":
            files["file_operations"]["records"].append(record)
            files["file_operations"]["files"].append(_file_operation_entry(record))
        elif record_type == "ARTIFACT_MANIFEST":
            files["artifacts"]["records"].append(record)
            files["artifacts"]["files"].extend(_manifest_files(record))
        elif record_type == "ARTIFACT_REFERENCE":
            event = str(record.get("event") or "").lower()
            if event in _ARTIFACT_IGNORE_EVENTS:
                continue
            if event and event not in _ARTIFACT_CREATE_EVENTS:
                continue
            files["artifacts"]["records"].append(record)
            entry = _file_entry_from_record(record, "artifact")
            if entry:
                files["artifacts"]["files"].append(entry)

    sections["execution"]["count"] = len(sections["execution"]["records"])
    sections["results"]["count"] = len(sections["results"]["records"])
    total_files = 0
    for key in ("input_files", "config_files", "artifacts"):
        subgroup = files[key]
        subgroup["files"] = _dedupe_file_entries(subgroup["files"])
        subgroup["count"] = len(subgroup["files"])
        total_files += subgroup["count"]
    # File operations are event-centric (one file may move several times), so they
    # are counted per operation rather than deduped by path.
    files["file_operations"]["count"] = len(files["file_operations"]["records"])
    total_files += len(files["file_operations"]["files"])
    files["count"] = total_files
    return sections


def _file_operation_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Return a file-centric row for a FILE_OPERATION execution record."""
    path = str(record.get("target_path") or record.get("source_path")
               or record.get("path") or "")
    return {
        "path": path,
        "display_name": Path(path).name if path else "",
        "operation": str(record.get("operation") or ""),
        "source_path": str(record.get("source_path") or ""),
        "target_path": str(record.get("target_path") or ""),
        "status": str(record.get("status") or ""),
        "step_id": str(record.get("step_id") or ""),
        "actions": ["view", "copy_path", "open_external"],
    }


def _manifest_files(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("files") or record.get("artifacts") or []
    files = raw.values() if isinstance(raw, dict) else raw
    result: list[dict[str, Any]] = []
    for item in files:
        if isinstance(item, str):
            result.append({"path": item, "display_name": Path(item).name})
        elif isinstance(item, dict):
            path = str(item.get("path") or item.get("artifact_path") or "")
            if not path:
                continue
            result.append({
                "path": path,
                "display_name": str(item.get("display_name") or item.get("name") or Path(path).name),
                "file_role": str(item.get("file_role") or item.get("role") or ""),
                "status": str(item.get("status") or ""),
                "actions": item.get("actions") or ["view", "copy_path", "open_external"],
            })
    return result


def _file_entry_from_record(record: dict[str, Any], default_role: str) -> dict[str, Any] | None:
    path = str(record.get("path") or record.get("file_path") or record.get("artifact_path") or "")
    if not path:
        return None
    return {
        "path": path,
        "display_name": str(record.get("display_name") or record.get("name") or Path(path).name),
        "file_role": str(record.get("file_role") or record.get("role")
                         or record.get("artifact_role") or default_role),
        "step_name": str(record.get("step_name") or ""),
        "status": str(record.get("status") or ""),
        "actions": ["view", "copy_path", "open_external"],
    }


def _dedupe_file_entries(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for file in files:
        path = str(file.get("path") or "")
        if path:
            rows[path] = file
    return sorted(rows.values(), key=lambda row: str(row.get("display_name") or row.get("path") or "").lower())


def _run_log_identity(path: Path, records: list[dict[str, Any]], sections: dict[str, Any]) -> dict[str, Any]:
    first = records[0] if records else {}
    complete = next(
        (
            record for record in reversed(records)
            if str(record.get("record_type") or "").upper() == "RUN_COMPLETE"
        ),
        {},
    )
    warning_count = sum(
        1 for record in records
        if str(record.get("record_type") or "").upper() == "WARNING"
    )
    error_count = sum(
        1 for record in records
        if str(record.get("record_type") or "").upper() == "ERROR"
    )
    return {
        "run_id": str(first.get("run_id") or ""),
        "run_timestamp": str(first.get("run_timestamp") or _timestamp_from_run_log_name(path)),
        "run_started_at": str(first.get("run_started_at") or first.get("timestamp") or ""),
        "run_completed_at": str(complete.get("timestamp") or ""),
        "status": str(complete.get("status") or ""),
        "warning_count": warning_count,
        "error_count": error_count,
        "app": str(first.get("app") or ""),
        "workflow": str(first.get("workflow") or first.get("workflow_name") or ""),
        "pipeline": str(first.get("pipeline") or first.get("pipeline_name") or ""),
        "log_path": str(path),
        "log_display_name": path.name,
        "execution_count": sections["execution"]["count"],
        "input_file_count": sections["files"]["input_files"]["count"],
        "config_file_count": sections["files"]["config_files"]["count"],
        "file_operation_count": sections["files"]["file_operations"]["count"],
        "artifact_count": sections["files"]["artifacts"]["count"],
        "file_count": sections["files"]["count"],
        "result_count": sections["results"]["count"],
    }


def _run_log_tree(run: dict[str, Any], sections: dict[str, Any]) -> dict[str, Any]:
    run_key = run.get("run_id") or run.get("run_timestamp") or run.get("log_display_name")
    files = sections["files"]
    return {
        "id": f"run:{run_key}",
        "label": str(run.get("run_timestamp") or run.get("log_display_name") or "Run"),
        "kind": "run",
        "status": str(run.get("status") or ""),
        "children": [
            {"id": f"run:{run_key}:execution", "label": "Execution", "kind": "execution", "count": sections["execution"]["count"], "children": []},
            {
                "id": f"run:{run_key}:files",
                "label": "Files",
                "kind": "files",
                "count": files["count"],
                "children": [
                    {
                        "id": f"run:{run_key}:input-files",
                        "label": "Input Files",
                        "kind": "input_files",
                        "count": files["input_files"]["count"],
                        "children": [_file_tree_node(file, "input_file") for file in files["input_files"]["files"]],
                    },
                    {
                        "id": f"run:{run_key}:config-files",
                        "label": "Config Files",
                        "kind": "config_files",
                        "count": files["config_files"]["count"],
                        "children": [_file_tree_node(file, "config_file") for file in files["config_files"]["files"]],
                    },
                    {
                        "id": f"run:{run_key}:artifacts",
                        "label": "Artifacts",
                        "kind": "artifacts",
                        "count": files["artifacts"]["count"],
                        "children": [_file_tree_node(file, "artifact") for file in files["artifacts"]["files"]],
                    },
                ],
            },
            {"id": f"run:{run_key}:results", "label": "Results", "kind": "results", "count": sections["results"]["count"], "children": []},
        ],
    }


def _file_tree_node(file: dict[str, Any], kind: str) -> dict[str, Any]:
    path = str(file.get("path") or "")
    return {
        "id": f"{kind}:{path}",
        "label": str(file.get("display_name") or Path(path).name),
        "kind": kind,
        "path": path,
        "status": str(file.get("status") or ""),
        "children": [],
    }


def _timestamp_from_run_log_name(path: Path) -> str:
    match = re.search(r"run_log\.(\d{8}_\d{6})", path.name)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Human-readable views (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup)
#
# Human-readable execution output is a projection of the append-only JSONL run
# log, rendered on demand — never a second durable execution log. Each render_*
# helper accepts a run-log path or an already-read list of records and returns
# safe plain text for console/CLI/email/report use.
# ---------------------------------------------------------------------------

def _run_records_for_view(source: Any) -> list[dict[str, Any]]:
    """Normalise a path or a record list into a list of run-log records."""
    if isinstance(source, (str, Path)):
        return read_run_log_sections(source)["records"]
    return [record for record in source if isinstance(record, dict)]


def _run_header_lines(records: list[dict[str, Any]]) -> list[str]:
    """Return human-readable run-identity header lines."""
    identity = _run_log_identity(Path(""), records, _run_log_sections(records))
    lines = [
        f"Run {identity['run_timestamp'] or identity['run_id'] or '(unknown)'}",
        f"  status:    {identity['status'] or 'in-progress'}",
        f"  run_id:    {identity['run_id']}",
        f"  started:   {identity['run_started_at']}",
        f"  completed: {identity['run_completed_at']}",
    ]
    for label, key in (("app", "app"), ("workflow", "workflow"), ("pipeline", "pipeline")):
        if identity[key]:
            lines.append(f"  {label}:{' ' * (9 - len(label))}{identity[key]}")
    if identity["warning_count"] or identity["error_count"]:
        lines.append(
            f"  warnings:  {identity['warning_count']}   errors: {identity['error_count']}"
        )
    return lines


def _render_file_block(label: str, subgroup: str, entries: list[dict[str, Any]]) -> str:
    """Render one files subgroup (input/config/artifacts/file-operations) as text."""
    if not entries:
        return f"{label} (0)\n  (none)"
    lines = [f"{label} ({len(entries)})"]
    for entry in entries:
        if subgroup == "file_operations":
            lines.append(
                f"  {entry.get('operation', '')}: {entry.get('source_path', '')}"
                f" -> {entry.get('target_path', '')} [{entry.get('status', '')}]"
            )
        else:
            role = str(entry.get("file_role") or "")
            suffix = f"  · {role}" if role else ""
            lines.append(f"  {entry.get('display_name') or entry.get('path')}{suffix}")
    return "\n".join(lines)


def render_execution_view(source: Any) -> str:
    """Render the execution audit trail from a JSONL run log (or records)."""
    sections = _run_log_sections(_run_records_for_view(source))
    return format_jsonl_records(sections["execution"]["records"]) or "(no execution records)"


def render_files_view(source: Any) -> str:
    """Render the Files group (input/config/file-operations/artifacts) as text."""
    files = _run_log_sections(_run_records_for_view(source))["files"]
    blocks = [
        _render_file_block("Input Files", "input_files", files["input_files"]["files"]),
        _render_file_block("Config Files", "config_files", files["config_files"]["files"]),
        _render_file_block("File Operations", "file_operations", files["file_operations"]["files"]),
        _render_file_block("Artifacts", "artifacts", files["artifacts"]["files"]),
    ]
    return "\n\n".join(blocks)


def render_results_view(source: Any) -> str:
    """Render the results records (summaries, analyses) from the run log."""
    sections = _run_log_sections(_run_records_for_view(source))
    return format_jsonl_records(sections["results"]["records"]) or "(no results)"


def render_summary_view(source: Any) -> str:
    """Render the run header plus the deterministic RUN_SUMMARY, if present."""
    records = _run_records_for_view(source)
    lines = list(_run_header_lines(records))
    summary = next(
        (record.get("summary") for record in reversed(records)
         if str(record.get("record_type") or "").upper() == "RUN_SUMMARY"
         and isinstance(record.get("summary"), dict)),
        None,
    )
    if summary:
        lines.append("Summary")
        lines.extend(f"  {key}: {value}" for key, value in summary.items())
    return "\n".join(lines)


def render_error_warning_view(source: Any) -> str:
    """Render only the WARNING/ERROR records from the run log."""
    records = _run_records_for_view(source)
    flagged = [
        record for record in records
        if str(record.get("record_type") or "").upper() in ("WARNING", "ERROR")
    ]
    return format_jsonl_records(flagged) if flagged else "(no warnings or errors)"


def render_run_view(source: Any) -> str:
    """Render the full human-readable run view (header + execution/files/results)."""
    records = _run_records_for_view(source)
    return "\n".join([
        *_run_header_lines(records),
        "",
        "== Execution ==",
        render_execution_view(records),
        "",
        "== Files ==",
        render_files_view(records),
        "",
        "== Results ==",
        render_results_view(records),
    ])


def format_jsonl_records(records: list[dict[str, Any]]) -> str:
    """Return a compact human-readable rendering of JSONL log records."""
    lines: list[str] = []
    for record in records:
        timestamp = str(record.get("timestamp") or record.get("asctime") or "")
        level = str(record.get("level") or record.get("levelname") or "").upper()
        source = str(record.get("source") or record.get("name") or "")
        message = str(record.get("message") or "")
        prefix = "  ".join(part for part in (timestamp, level, source) if part)
        lines.append(f"{prefix}  {message}" if prefix else message)

        details = _record_detail_lines(record)
        if details:
            lines.extend(f"  {line}" for line in details)
        lines.append("")

    return "\n".join(lines).rstrip()


def _record_detail_lines(record: dict[str, Any]) -> list[str]:
    """Return stable detail lines for non-envelope JSONL fields."""
    envelope = {
        "asctime",
        "created",
        "depth",
        "level",
        "levelname",
        "message",
        "name",
        "parent_sequence",
        "sequence",
        "source",
        "timestamp",
    }
    lines: list[str] = []
    for key in sorted(k for k in record if k not in envelope):
        value = record[key]
        if value in (None, "", [], {}):
            continue
        rendered = json.dumps(value, default=str, sort_keys=True)
        lines.append(f"{key}: {rendered}")
    return lines


def _derived_jsonl_path(path: Path, jsonl_stems: set[str]) -> str:
    """Return matching JSONL source path for a readable log when present."""
    if path.suffix == ".jsonl":
        return ""
    stem = path.with_suffix("").as_posix()
    return f"{stem}.jsonl" if stem in jsonl_stems else ""


def _record_matches(record: dict[str, Any], filters: dict[str, str]) -> bool:
    """Return true when a JSONL record matches all requested filters."""
    if filters.get("errors_only") == "true":
        level = str(record.get("level", record.get("levelname", ""))).upper()
        if level not in {"ERROR", "CRITICAL"}:
            return False

    for key in ("level", "app", "pipeline_run_id", "pipeline_step_name", "batch_id", "file_name"):
        expected = filters.get(key)
        if expected and str(record.get(key, "")) != expected:
            return False

    return True


# ---------------------------------------------------------------------------
# Depth-tracking entry / exit helpers
# ---------------------------------------------------------------------------

def log_enter(ctx: Any, msg: str, logger: logging.Logger) -> None:
    """
    Log function entry and increment ctx.log_depth.

    Parameters
    ----------
    ctx : Any
        Application context. ctx.log_depth is incremented in-place.
    msg : str
        Entry message describing the function and its key inputs.
    logger : logging.Logger
        Logger to write to.
    """
    global _current_depth
    logger.debug("→ %s", msg)
    ctx.log_depth  += 1
    _current_depth  = ctx.log_depth


def log_exit(ctx: Any, msg: str, logger: logging.Logger) -> None:
    """
    Log function exit and decrement ctx.log_depth.

    Parameters
    ----------
    ctx : Any
        Application context. ctx.log_depth is decremented in-place.
    msg : str
        Exit message describing the outcome.
    logger : logging.Logger
        Logger to write to.
    """
    global _current_depth
    ctx.log_depth   = max(0, ctx.log_depth - 1)
    _current_depth  = ctx.log_depth
    logger.debug("← %s", msg)

def log_row_values(
	logger: logging.Logger,
	message: str,
	row_num: int,
	row: dict[str, Any],
	column_types: dict[str, str],
) -> None:
	logger.error("%s row=%d", message, row_num)

	for col, value in row.items():
		logger.error(
			"  col=%s datatype=%s value=%r",
			col,
			column_types.get(col, "UNKNOWN"),
			value,
		)
