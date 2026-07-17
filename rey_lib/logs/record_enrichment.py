"""Run-log record enrichment, context binding, and ambient execution state."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.logs.record_validation import _validate_run_record, _validate_run_record_fields


_RUN_RECORD_SCHEMA_VERSION = 1


EXECUTION_RECORD_TYPES = frozenset({
    "RUN_START", "EXECUTION_PLAN", "STEP_START", "STEP_END", "INFO", "WARNING",
    "ERROR", "FILE_OPERATION", "RUN_COMPLETE", "STEP_FAILURE", "APP_EXECUTION",
    "SQL_EXECUTION", "ROW_COUNT", "VALIDATION_RESULT",
})


RUN_RESULT_RECORD_TYPES = frozenset({
    "RUN_SUMMARY", "EMAIL_SUMMARY",
    "LLM_ANALYSIS_PACKAGE", "LLM_ANALYSIS_RESULT",
    "MANUAL_REVIEW", "POST_MORTEM",
})


FILES_RECORD_SUBGROUP = {
    "INPUT_FILE_REFERENCE": "input_files",
    "INPUT_DISCOVERED": "input_files",
    "CONFIG_FILE_REFERENCE": "config_files",
    "CONFIG_FILE_MANIFEST": "config_files",
    "ARTIFACT_REFERENCE": "artifacts",
    "ARTIFACT_MANIFEST": "artifacts",
}


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


def _execution_name(ctx: Any) -> str:
    """Return the execution-owned name for the durable run log filename."""
    for key in ("pipeline_name", "workflow_name", "owner_app_name", "app_name", "name"):
        value = str(getattr(ctx, key, "") or "").strip()
        if value:
            return value
    return "app"


def _execution_log_filename(ctx: Any) -> str:
    """Return the standardized execution log filename for one run."""
    return f"{_execution_name(ctx)}.{ctx.run_timestamp}.jsonl"


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

    The run log is a run-created artifact named
    ``{execution_name}.<run_timestamp>.jsonl`` beside the configured log directory.
    The path is resolved once and cached on ``ctx.run_log_path``; run identity is
    established first so ``run_id``/``run_timestamp`` exist before the first
    record. The logging layer names and writes its own run log (it cannot depend
    on files/file_utils).

    Parameters
    ----------
    ctx : Any
        Application context. Must have either ``run_log_dir`` set explicitly or
        ``log_file`` set (by setup_logging) so the run-log directory is known;
        execution should not proceed without a durable log path.

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
    run_log_dir = getattr(ctx, "run_log_dir", None)
    log_file = getattr(ctx, "log_file", None)
    if run_log_dir:
        directory = Path(run_log_dir)
    elif log_file:
        directory = Path(log_file).parent
    else:
        raise ValueError(
            "Cannot open run log: no durable log path (ctx.run_log_dir or "
            "ctx.log_file). Configure logging before starting a run."
        )
    path = directory / _execution_log_filename(ctx)
    ctx.run_log_path = str(path)
    return path


_SECRET_WRITE_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|access[_-]?key|"
    r"credential|connection[_-]?string|private[_-]?key)",
    re.IGNORECASE,
)


def sanitize_log_value(value: Any) -> Any:
    """Return a write-safe copy of a log value with secret-like keys masked."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            sanitized[key_text] = (
                "[REDACTED]" if _SECRET_WRITE_KEY_RE.search(key_text)
                else sanitize_log_value(item)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_log_value(item) for item in value]
    return value


def sanitize_command_arguments(arguments: list[Any] | tuple[Any, ...]) -> list[str]:
    """Return command arguments with values after secret-like flags redacted."""
    sanitized: list[str] = []
    redact_next = False
    for arg in arguments:
        text = str(arg)
        key = text.lstrip("-").split("=", 1)[0]
        if redact_next:
            sanitized.append("[REDACTED]")
            redact_next = False
            continue
        if _SECRET_WRITE_KEY_RE.search(key):
            if "=" in text:
                prefix = text.split("=", 1)[0]
                sanitized.append(f"{prefix}=[REDACTED]")
            else:
                sanitized.append(text)
                redact_next = True
            continue
        sanitized.append(text)
    return sanitized


def _base_record(ctx: Any, record_type: str, message: str) -> dict[str, Any]:
    """Build the shared typed-record envelope before event fields are merged."""
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
    return record


def _context_fields() -> dict[str, Any]:
    """Return active step/correlation context for typed run-log records."""
    merged: dict[str, Any] = {}
    step = current_step()
    if step:
        merged.update(step)
    correlation = current_correlation()
    if correlation:
        merged.update(correlation)
    return merged


def _enrich_run_record(
    ctx: Any,
    record_type: str,
    *,
    message: str = "",
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge context, sanitize values, and validate one typed run-log record."""
    record = _base_record(ctx, record_type, message)
    record.update(_context_fields())
    record.update(fields or {})
    record = sanitize_log_value(record)
    _validate_run_record(record)
    return record


def _has_durable_run_path(ctx: Any) -> bool:
    """Return True when ctx appears able to write an append-only run log."""
    return bool(
        getattr(ctx, "run_log_path", None)
        or getattr(ctx, "run_log_dir", None)
        or getattr(ctx, "log_file", None)
    )


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
    if _has_durable_run_path(ctx):
        _validate_run_record_fields(record_type, fields)
    try:
        path = open_run_log(ctx)
        record = _enrich_run_record(ctx, record_type, message=message, fields=fields)

        # Logical record identity and parent, derived from the nest-level state
        # (SGC_Rey_Log_Record_Parenting_Phase_2). Stamped before the append; the
        # sequence advances only after a successful write, so a failed append does
        # not skip an id.
        from rey_lib.logs import record_parenting
        from rey_lib.logs.nest_level import get_nest_level

        nest_level = get_nest_level(ctx)
        record_id = record_parenting.stamp_record(ctx, record, nest_level)

        # Route the durable append through the primitive I/O layer so the run-log
        # writer shares one low-level append with file_utils without either
        # foundational module importing the other (SGC_Rey_Lib_Primitive_File_IO_Layer).
        # Imported lazily because the rey_lib.files package eagerly loads file_utils,
        # which imports this logging layer — a module-level import would form a cycle.
        from rey_lib.files import primitive_file_io

        primitive_file_io.append_jsonl(path, record)
        record_parenting.commit_record(ctx, record_id, nest_level)
    except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
        logging.getLogger(__name__).warning(
            "run log: could not append %s record: %s", record_type, exc
        )


_CURRENT_RUN: dict[str, Any] = {"run": None}


_CURRENT_STEP: dict[str, Any] = {"step": None}


_CURRENT_CORRELATION: dict[str, Any] = {"correlation": None}


def bind_run(ctx: Any = None, *, run_log_path: str = "", run_id: str = "",
             run_timestamp: str = "") -> None:
    """Bind the current run so file_utils records file operations against it.

    Reads run_log_path / run_id / run_timestamp from ``ctx`` when given, else from
    the keyword arguments. Binding without a durable run_log_path is a no-op.

    The execution-identity fields ``_base_record`` reads (app identity, workflow_name,
    pipeline_name) are captured onto the bound run so ambient FILE_OPERATION records
    written through it receive the same standard enrichment as any other log write,
    rather than lacking ``app`` and context. Empty values are left off by
    ``_base_record`` exactly as an absent attribute would be.
    """
    identity: dict[str, str] = {
        key: "" for key in
        ("owner_app_name", "app_name", "name", "workflow_name", "pipeline_name")
    }
    if ctx is not None:
        run_log_path = str(getattr(ctx, "run_log_path", "") or run_log_path)
        run_id = str(getattr(ctx, "run_id", "") or run_id)
        run_timestamp = str(getattr(ctx, "run_timestamp", "") or run_timestamp)
        for key in identity:
            identity[key] = str(getattr(ctx, key, "") or "")
    if not run_log_path:
        return
    _CURRENT_RUN["run"] = SimpleNamespace(
        run_id=run_id, run_timestamp=run_timestamp, run_log_path=str(run_log_path),
        **identity,
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


def bind_step(
    *,
    step_id: str,
    step_name: str = "",
    step_sequence: int | None = None,
    app: str = "",
    pipeline_name: str = "",
    workflow_name: str = "",
) -> None:
    """Bind the current step context independently from the current run."""
    if not step_id:
        return
    step: dict[str, Any] = {"step_id": str(step_id)}
    if step_name:
        step["step_name"] = str(step_name)
    if step_sequence is not None:
        step["step_sequence"] = step_sequence
    if app:
        step["app"] = str(app)
    if pipeline_name:
        step["pipeline_name"] = str(pipeline_name)
    if workflow_name:
        step["workflow_name"] = str(workflow_name)
    _CURRENT_STEP["step"] = SimpleNamespace(**step)


def clear_step() -> None:
    """Clear the current step context."""
    _CURRENT_STEP["step"] = None


def current_step() -> dict[str, Any] | None:
    """Return the active step context, or None if no step is bound."""
    step = _CURRENT_STEP["step"]
    if step is None:
        return None
    return dict(vars(step))


def bind_correlation(correlation_id: str = "") -> None:
    """Bind the current correlation id independently from run and step context."""
    if not correlation_id:
        return
    _CURRENT_CORRELATION["correlation"] = SimpleNamespace(
        correlation_id=str(correlation_id)
    )


def clear_correlation() -> None:
    """Clear the current correlation context."""
    _CURRENT_CORRELATION["correlation"] = None


def current_correlation() -> dict[str, str] | None:
    """Return the active correlation context, or None if no correlation is bound."""
    correlation = _CURRENT_CORRELATION["correlation"]
    if correlation is None:
        return None
    return {"correlation_id": correlation.correlation_id}
