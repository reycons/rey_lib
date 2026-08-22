"""Run-log record enrichment, context binding, and ambient execution state."""

from __future__ import annotations

import re
import threading
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






def _record_group(record_type: str) -> str:
    """Map a record type to its top-level run-log group (execution/files/results)."""
    if record_type in FILES_RECORD_SUBGROUP:
        return "files"
    if record_type in RUN_RESULT_RECORD_TYPES:
        return "results"
    return "execution"




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






def log_run_record(
    run_log: 'RunLog', record_type: str, *, message: str = "", **fields: Any
) -> int | None:
    """Append one typed record through the run log that owns the write.

    The run log is passed, never located. It owns the record sequence, the
    parenting, the destination and the state every record is stamped with, so
    there is nothing left for this to do but hand the call over.
    """
    return run_log.append(record_type, message=message, **fields)


# The ambient run binding.
#
# A stack, not a slot. Nesting is real -- run_app_operation binds, and a
# workflow inside it binds and clears -- so a single slot let the inner clear
# unbind the run that was still executing, and every ambient file operation
# after that workflow returned was silently dropped.
#
# The bound *value* is process-global on purpose, not thread-local:
# pipeline_coordinator runs a parallel step group in a ThreadPoolExecutor
# sharing one run, and those threads must record against the run that owns them.
#
# The *frames* are per-thread, because nesting is a per-thread property. A
# global frame list would be correct only for properly nested pushes and pops,
# and threads do not nest: A.bind, B.bind, A.clear, B.clear is possible, and a
# global list would restore the wrong owner.
#
# THE INVARIANT, enforced below rather than assumed:
#
#   While an ambient scope is active, every concurrent bind in this process
#   must bind the same RunLog. Distinct RunLog ambient scopes may not overlap
#   in one process.
#
# Today the runtime satisfies this because every concurrent unit of execution
# is a subprocess -- pipeline steps, Console app runs -- and a subprocess binds
# in its own interpreter. Nothing in the language enforces that, so a bind that
# breaks it is detected and reported instead of silently corrupting the
# binding. It is reported, not raised: logging must not mask execution.
_BINDING_GUARD = threading.Lock()

_CURRENT_RUN: dict[str, Any] = {
    "run": None,       # the bound RunLog, shared across threads
    "owner": None,     # thread that established the current distinct binding
    "frames": {},      # thread ident -> [value to restore on that thread's clears]
}


_CURRENT_STEP: dict[str, Any] = {"step": None}


_CURRENT_CORRELATION: dict[str, Any] = {"correlation": None}


def bind_run(run_log: Any) -> None:
    """Bind the run log that ambient file operations are recorded through.

    File operations happen deep in utility code that holds no run log, so the
    run in progress is bound around them. What is bound is the run log itself:
    the one owner of the write, not a description of it. Rebuilding an owner
    from a description is how a single run ends up with two of them.

    Binding a run log with no durable path keeps whatever was already bound --
    there is nothing for an ambient record to be appended to -- but it still
    opens a scope, so the matching ``clear_run`` closes its own scope and not
    an enclosing one.
    """
    if run_log is not None:
        try:
            if not run_log.path():
                run_log = None
        except Exception:  # noqa: BLE001 — an unopened run log binds nothing.
            run_log = None

    ident = threading.get_ident()
    with _BINDING_GUARD:
        active = _CURRENT_RUN["run"]
        owner = _CURRENT_RUN["owner"]
        violation = (
            run_log is not None
            and active is not None
            and active is not run_log
            and owner is not None
            and owner != ident
        )
        _CURRENT_RUN["frames"].setdefault(ident, []).append(active)
        if run_log is not None:
            _CURRENT_RUN["run"] = run_log
            _CURRENT_RUN["owner"] = ident

    if violation:
        from rey_lib.logs.logging_setup import get_logger

        get_logger(__name__).warning(
            "run log: thread %s bound run %s while thread %s held an ambient "
            "scope for run %s. Distinct ambient run scopes must not overlap in "
            "one process; ambient file operations may be recorded against the "
            "wrong run.",
            ident, getattr(run_log, "run_id", "?"), owner,
            getattr(active, "run_id", "?"),
        )


def clear_run() -> None:
    """Close this thread's innermost ambient scope, restoring what it replaced.

    Unbinding entirely only happens when the outermost scope on this thread is
    closed, so a nested scope ending does not silently stop the enclosing run
    from recording.
    """
    ident = threading.get_ident()
    with _BINDING_GUARD:
        frames = _CURRENT_RUN["frames"].get(ident)
        restored = frames.pop() if frames else None
        if frames is not None and not frames:
            del _CURRENT_RUN["frames"][ident]
        _CURRENT_RUN["run"] = restored
        _CURRENT_RUN["owner"] = ident if restored is not None else None


def reset_run_binding() -> None:
    """Drop the binding and every scope. The runtime's teardown, not a scope's.

    A collected run log must not stay bound: the next thing to record ambiently
    in this process would append to a log whose owner is closed.
    """
    with _BINDING_GUARD:
        _CURRENT_RUN["run"] = None
        _CURRENT_RUN["owner"] = None
        _CURRENT_RUN["frames"].clear()


def current_run() -> dict[str, str] | None:
    """Return the bound run's {run_log_path, run_id}, or None if unbound."""
    run = _CURRENT_RUN["run"]
    if run is None:
        return None
    return {"run_log_path": str(run.path()), "run_id": run.run_id}


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
