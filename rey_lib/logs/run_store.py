"""Where a run log is persisted, and what a chosen destination guarantees.

``logging.run_store`` is authoritative for run-log persistence:

    jsonl   the append-only JSONL run log only (the historical behaviour)
    db      the control database only
    both    both, and both are required

``both`` exists for the migration. It is not best-effort: if either destination
fails the run log is only half written, which is the two-sources-of-truth
problem this consolidation exists to remove, so a one-sided write is raised
rather than logged and passed over.

Two boundaries that look alike and are not
------------------------------------------
``log_run_record`` is the fail-safe *record* writer. It warns and returns None
rather than raising, because logging must never mask application execution, and
that stays true here.

The *run store* is a durability contract chosen in configuration. When an
operator selects ``db`` or ``both`` they have said where this run must be
recorded, and silently recording it somewhere else is not a degraded success.
So a required destination failing is an error, while an individual record write
failing under ``jsonl`` behaves exactly as it always has.

Layering
--------
This module is the logs layer's persistence orchestration. It calls
``Control`` and nothing beneath it -- never a procedure map, never a DB
adapter -- and it never reaches up into ``rey_lib.run``. Run identity arrives on
the context, already established at the launch boundary.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "RUN_STORE_MODES",
    "run_store_mode",
    "writes_jsonl",
    "writes_db",
    "new_batch_intent",
    "validate_run_store",
    "persist_run_start",
    "persist_step_start",
    "persist_step_end",
    "persist_run_complete",
    "persist_event",
    "require_jsonl_record",
]

RUN_STORE_MODES = ("jsonl", "db", "both")

_DEFAULT_MODE = "jsonl"


# rey_lib.errors imports rey_lib.logs for its logger, and rey_lib.control does
# too, so both are reached late from inside functions. This is the idiom the
# package already uses across those boundaries; a module-level import here
# would close the cycle at interpreter start.


def _errors() -> Any:
    """Return the shared error module, imported late to avoid an import cycle."""
    from rey_lib.errors import error_utils

    return error_utils


def _logging_setting(ctx: Any, name: str) -> Any:
    """Read one key from the logging config, namespace or mapping."""
    logging_cfg = getattr(ctx, "logging", None)
    if logging_cfg is None:
        return None
    value = getattr(logging_cfg, name, None)
    if value is None and isinstance(logging_cfg, dict):
        value = logging_cfg.get(name)
    return value


def run_store_mode(ctx: Any) -> str:
    """Return the configured run-store mode, defaulting to ``jsonl``.

    Existing installations that say nothing keep the behaviour they already
    have. Choosing the database is a migration someone performs, never
    something a missing key turns on.

    Raises
    ------
    ConfigError
        When the configured value is not one of :data:`RUN_STORE_MODES`.
    """
    value = _logging_setting(ctx, "run_store")
    if value is None:
        return _DEFAULT_MODE
    mode = str(value).strip().lower()
    if mode not in RUN_STORE_MODES:
        raise _errors().ConfigError(
            f"logging.run_store is '{value}'. Use one of {', '.join(RUN_STORE_MODES)}."
        )
    return mode


def writes_jsonl(ctx: Any) -> bool:
    """Whether the JSONL run log is a selected destination."""
    return run_store_mode(ctx) in ("jsonl", "both")


def writes_db(ctx: Any) -> bool:
    """Whether the control database is a selected destination."""
    return run_store_mode(ctx) in ("db", "both")


def new_batch_intent(ctx: Any) -> bool:
    """Return the declared batch intent for this execution.

    Launch states whether this execution starts a batch or continues one. The
    intent is read here and never inferred: deciding from whether ``batch_id``
    happens to be set would make an unrelated leftover value silently mean
    "reuse", which is how an execution joins a batch it has nothing to do with.

    Absent means true. A launch that says nothing is starting its own batch,
    which is the safe reading -- continuing someone else's is the case that has
    to be asked for.
    """
    declared = getattr(ctx, "new_batch", None)
    if declared is None:
        return True
    return bool(declared)


def validate_run_store(ctx: Any) -> None:
    """Refuse an impossible destination before any application work starts.

    A run that cannot reach its required run store should fail at launch rather
    than part-way through, having already done work it cannot record.

    Raises
    ------
    ConfigError
        When ``db``/``both`` is selected without the connection or routine map
        it needs, or when an invalid reuse of a batch has been requested.
    """
    mode = run_store_mode(ctx)
    if mode == "jsonl":
        return

    if not _logging_setting(ctx, "db_connection"):
        raise _errors().ConfigError(
            f"logging.run_store is '{mode}' but logging.db_connection names no "
            "connection. State which connection the run store is written to."
        )

    control_cfg = getattr(ctx, "control", None)
    if not getattr(control_cfg, "procedure_map", None):
        raise _errors().ConfigError(
            f"logging.run_store is '{mode}' but control.procedure_map is not set. "
            "The routine contract for control database calls must be named."
        )

    # An invalid reuse request is a launch error, not something to repair by
    # creating the batch the caller explicitly said not to create.
    if not new_batch_intent(ctx) and not getattr(ctx, "batch_id", None):
        raise _errors().ConfigError(
            "newBatch is false but no batch_id is bound to reuse. Reuse is an "
            "explicit continuation of a batch that already exists; a launch that "
            "needs its own batch must ask for one."
        )


def require_jsonl_record(ctx: Any, record_id: Optional[int], record_type: str) -> None:
    """Escalate a failed JSONL write when JSONL is a required destination.

    Under ``both`` a missing record means the run log is half written. The
    record writer has already warned and returned None on its own terms; this
    is the run store's separate durability contract on top of that.

    Under ``jsonl`` nothing is raised -- that is the historical behaviour, kept
    exactly.
    """
    if record_id is not None:
        return
    if run_store_mode(ctx) != "both":
        return
    raise _errors().StateError(
        f"run_store is 'both' but the JSONL {record_type} record was not written. "
        "Both destinations are required; the run log is now recorded in one place "
        "and not the other."
    )


# ---------------------------------------------------------------------------
# Control-database persistence
# ---------------------------------------------------------------------------


def _control(ctx: Any) -> Any:
    """Return this run's Control, constructing it once.

    Control takes the ``control`` procedure map off the context when it is
    built, so a second construction would find nothing to take. It is therefore
    made once and kept for the run, which is also what lets ``batch_id`` and
    ``batch_step_id`` persist across the lifecycle calls that follow.

    Imported late: rey_lib.control imports rey_lib.logs for its logger, so a
    module-level import here would close a cycle.
    """
    existing = getattr(ctx, "control_api", None)
    if existing is not None:
        return existing

    from rey_lib.control import Control

    ctx.control_api = Control(ctx)
    return ctx.control_api


def persist_run_start(ctx: Any, **fields: Any) -> None:
    """Establish the batch this run belongs to, then record the run starting.

    Honours the declared intent and nothing else:

    - ``newBatch`` true (or absent) -- start a batch; the procedure map's
      ``load_to_ctx`` binds the returned id to ``ctx.batch_id``.
    - ``newBatch`` false -- require an existing ``ctx.batch_id`` and start none.

    ``ctx.batch_owned_by_run`` records whether this execution created the batch,
    so completion knows whether ending it is its business.
    """
    if not writes_db(ctx):
        return

    control = _control(ctx)
    if new_batch_intent(ctx):
        control.start_batch(
            batch_name=str(fields.get("operation") or getattr(ctx, "app_name", "") or "run"),
            required=True,
        )
        if not control.batch_id:
            raise _errors().StateError(
                "control start_batch returned no batch_id. The run store cannot "
                "record steps or events without the batch that groups them."
            )
        ctx.batch_owned_by_run = True
    else:
        if not control.batch_id:
            raise _errors().ConfigError(
                "newBatch is false but no batch_id is bound to reuse. A batch is "
                "never manufactured to satisfy a reuse request."
            )
        ctx.batch_owned_by_run = False

    control.log_event(
        severity="INFO",
        event_name="RUN_START",
        message=str(fields.get("message") or "run started"),
        required=True,
    )


def persist_step_start(ctx: Any, step_name: str, step_sequence: int,
                       step_type: str = "", **fields: Any) -> None:
    """Open a control step for this run; the map binds ctx.batch_step_id."""
    if not writes_db(ctx):
        return
    _control(ctx).start_step(
        step_name=step_name,
        step_sequence=step_sequence,
        step_type=step_type or None,
        required=True,
    )


def persist_step_end(ctx: Any, step_name: str, status: str,
                     message: str = "", **fields: Any) -> None:
    """Close the open control step."""
    if not writes_db(ctx):
        return
    control = _control(ctx)
    control.end_step(status=status, message=message or None, required=True)
    # The step is closed; later events belong to the run, not to it.
    control.batch_step_id = None


def persist_run_complete(ctx: Any, status: str, message: str = "", **fields: Any) -> None:
    """Record the run finishing, and end the batch only if this run began it.

    A batch may contain several runs. Ending it because one of them finished
    would close it under the others, so completion ends only a batch this
    execution created.
    """
    if not writes_db(ctx):
        return

    control = _control(ctx)
    control.log_event(
        severity="INFO" if status == "success" else "ERROR",
        event_name="RUN_COMPLETE",
        message=message or status,
        required=True,
    )
    if getattr(ctx, "batch_owned_by_run", False):
        control.end_batch(status=status,
                          error_message=None if status == "success" else (message or status),
                          required=True)


def persist_event(ctx: Any, severity: str, event_name: str, message: str,
                  **fields: Any) -> None:
    """Record one durable event against this run.

    Carries ``run_id`` whether or not a step is open, so events from different
    runs sharing a batch stay distinguishable when ``batch_step_id`` is null.
    """
    if not writes_db(ctx):
        return
    _control(ctx).log_event(
        severity=severity, event_name=event_name, message=message, required=True,
    )
