"""What a run store destination is, read from configuration at launch.

This module is the configuration reader, not an owner. It answers which
outputs are configured, before any record is written, and nothing afterwards.

Everything that happens *at* an output belongs to the logging writer
(``rey_lib.logs.log_writer.LogWriter``): which outputs a record is written to,
and whether a required write succeeded. Which records are structural belongs to
``RunLog``.

``logging.run_store`` is the single authority for the logging outputs:

    db      the control database only (the default when unset)
    jsonl   the append-only JSONL file only
    both    both, and both are required

JSONL is never written unless ``jsonl`` or ``both`` is stated (backlog
logging.app_logger.s8_json_output_cleanup).

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
``rey_lib.run``. The database output writes through the launch Control, which
the launch boundary already requires; nothing here reaches it.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "RUN_STORE_MODES",
    "run_store_mode",
    "validate_run_store",
]

RUN_STORE_MODES = ("jsonl", "db", "both")

_DEFAULT_MODE = "db"


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
    """Return the configured run-store mode, defaulting to ``db``.

    Unset means the database only: JSONL is written only when it is asked for.

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
    """Refuse an invalid logging-output configuration before any work starts.

    Validates the writer configuration only: ``logging.run_store`` must name a
    known mode. The database output writes through the launch Control, which
    the launch boundary already requires, so no logging connection or routine
    map is asked for here.

    Raises
    ------
    ConfigError
        When ``logging.run_store`` is not one of :data:`RUN_STORE_MODES`.
    """
    run_store_mode(ctx)


# ---------------------------------------------------------------------------
# Control-database persistence
# ---------------------------------------------------------------------------


