"""What a run store destination is, read from configuration at launch.

This module is the configuration reader, not an owner. It answers two
questions before any run log exists -- which destination is configured, and
whether that destination is reachable -- and nothing afterwards.

Everything that happens *at* a destination belongs to ``RunLog``: whether to
write to the database, when to open and close the control batch and step, what
a lost structural record means, and how a record type maps to an event
severity. Those used to live here, taking a RunLog and re-deciding what the
run log already knew, which made this a second authority over persistence.

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
Configuration only. It calls nothing beneath it and never reaches up into
``rey_lib.run``. ``RunLog`` calls ``Control``; ``Control`` calls the procedure
map; nothing here does either.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "RUN_STORE_MODES",
    "run_store_mode",
    "validate_run_store",
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


# ---------------------------------------------------------------------------
# Control-database persistence
# ---------------------------------------------------------------------------


