"""Run-log record enrichment, context binding, and ambient execution state."""

from __future__ import annotations

import re
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
    "SOURCE_FILE_INVENTORY": "input_files",
    "CONFIG_FILE_REFERENCE": "config_files",
    "CONFIG_FILE_MANIFEST": "config_files",
    "ARTIFACT_REFERENCE": "artifacts",
    "ARTIFACT_MANIFEST": "artifacts",
}


def require_run_id(ctx: Any) -> str:
    """
    Return the run identity this context already carries, or refuse.

    Logging consumes a run identity; it does not create one. Whoever launches an
    execution establishes it through :mod:`rey_lib.run` and the context arrives
    carrying it -- so this reads, and fails when it cannot.

    It replaces ``resolve_run_identity``, whose name promised "find or create"
    and whose implementation reached upward into ``rey_lib.run`` to mint. That
    was an ownership leak in the direction the layer chain forbids: identity is
    established at a launch boundary and travels down, never fetched back up
    from the layer that records it.

    Parameters
    ----------
    ctx : Any
        Application context expected to carry ``run_id``.

    Returns
    -------
    str
        The bound run id.

    Raises
    ------
    ValueError
        If no run identity has been established.
    """
    run_id = getattr(ctx, "run_id", None)
    if not run_id:
        raise ValueError(
            "No run identity has been established. A run is identified by its "
            "launch boundary through rey_lib.run before anything is logged; "
            "logging reads that identity and never mints one."
        )
    return str(run_id)


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
    The path is resolved once and cached on ``ctx.run_log_path``. Run identity is
    read, never created: the launch boundary establishes it through
    ``rey_lib.run`` and ``setup_logging`` requires it, so ``run_timestamp``
    already exists by the time the first record is written. The logging layer
    names and writes its own run log (it cannot depend on files/file_utils).

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

    # Consumed, never minted. Identity is established at the launch boundary
    # through rey_lib.run and required by setup_logging; this is the write path,
    # which must not raise on its own account -- logging must not mask
    # execution, so an unidentified context fails here exactly as any other
    # write fault does, warned rather than thrown.
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


#: The canonical run lineage every durable record carries, so the execution tree
#: is readable from the log itself rather than reconstructed from runtime state.
#:
#: Deliberately generic. Lineage is ``run_id -> parent_run_id -> parent_run_id``
#: and nothing else, so from any leaf the root is reached by walking parents:
#:
#:     R100  pipeline
#:     |-- R101  workflow
#:     |   |-- R102  app
#:     |   +-- R103  app
#:     +-- R104  app
#:
#: What a run is *of* lives in its subject, not in the lineage. That is what lets
#: FTP, SQL, AI, external apps and kinds not yet imagined nest without the
#: contract growing a ``<kind>_run_id`` every time a new nesting type appears.
#:
#: ``parent_run_id`` is explicit rather than derived. "Nearest enclosing run" is
#: semantic, not structural: with parallel children, or a kind that nests two
#: levels, inferring parentage from a set of enclosing identities is ambiguous.
RUN_LINEAGE_FIELDS: tuple[str, ...] = (
    "parent_run_id",
    "subject_type",
    "subject_id",
    "subject_name",
)

#: Domain identities that are **not** canonical lineage.
#:
#: Kept because existing log readers depend on them, and because
#: ``pipeline_run_id`` is the one enclosing run identity the estate already
#: computes -- recording it distinguishes two runs of the same pipeline for those
#: readers. New work reads lineage above; this is legacy domain metadata and
#: nothing should be added to it.
RUN_DOMAIN_FIELDS: tuple[str, ...] = (
    "pipeline_run_id",
    "workflow_run_id",
    "pipeline_id",
    "workflow_id",
)


def _lineage_value(ctx: Any, field: str) -> str:
    """Return one lineage value from the context, or empty when it has none.

    ``pipeline_run_id`` is read from ``ctx.runtime`` as well as from the context
    itself: pipeline_coordinator stamps it there, and it is the one enclosing
    run identity the estate already computes.
    """
    value = getattr(ctx, field, None)
    if value:
        return str(value)
    runtime = getattr(ctx, "runtime", None)
    if runtime is None:
        return ""
    found = getattr(runtime, field, None)
    if found is None and isinstance(runtime, dict):
        found = runtime.get(field)
    return str(found) if found else ""


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
    # Lineage and domain metadata, stamped rather than supplied: no caller adds
    # these, so a record cannot be written without the tree it belongs to.
    # Absent values are left off exactly as an absent attribute already is.
    for key in (*RUN_LINEAGE_FIELDS, *RUN_DOMAIN_FIELDS):
        found = _lineage_value(ctx, key)
        if found:
            record[key] = found
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


def log_run_record(
    run_log: 'RunLog', record_type: str, *, message: str = "", **fields: Any
) -> int | None:
    """Append one typed record through the run log that owns the write.

    The run log is passed, never located. It owns the record sequence, the
    parenting, the destination and the state every record is stamped with, so
    there is nothing left for this to do but hand the call over.
    """
    return run_log.append(record_type, message=message, **fields)


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
        ("owner_app_name", "app_name", "name", "workflow_name", "pipeline_name",
         *RUN_LINEAGE_FIELDS, *RUN_DOMAIN_FIELDS)
    }
    if ctx is not None:
        run_log_path = str(getattr(ctx, "run_log_path", "") or run_log_path)
        run_id = str(getattr(ctx, "run_id", "") or run_id)
        run_timestamp = str(getattr(ctx, "run_timestamp", "") or run_timestamp)
        for key in identity:
            # Lineage is resolved rather than read: the enclosing pipeline run
            # is stamped on ctx.runtime, so a plain attribute read would bind a
            # run whose ambient records lack the tree its own records carry.
            identity[key] = (
                _lineage_value(ctx, key)
                if key in RUN_LINEAGE_FIELDS or key in RUN_DOMAIN_FIELDS
                else str(getattr(ctx, key, "") or "")
            )
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
