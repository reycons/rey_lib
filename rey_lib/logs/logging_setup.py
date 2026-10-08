"""Legacy logging helpers: setup_logging, get_logger, log_enter and log_exit.

Logging configuration and call depth are AppLogger's
(rey_lib.logs.app_logger.AppLogger; backlog logging.app_logger.s3_configuration
and s4_depth). What remains here is migration state with a scheduled removal:
setup_logging, log_enter and log_exit are pass-throughs to AppLogger kept for
their remaining callers until S6, and get_logger is a wrapper returning
AppLogger(name) (S5) until S7 deletes it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs.app_logger import AppLogger


def setup_logging(ctx: Any, operation: str = "app") -> None:
    """Configure logging through ``AppLogger._configure`` (removed in S6).

    The configuration itself is AppLogger's; this delegate keeps the remaining
    callers working until the legacy helpers are removed.
    """
    AppLogger._configure(ctx, operation)


def get_logger(name: str) -> AppLogger:
    """Return ``AppLogger(name)`` (compatibility wrapper, deleted in S7).

    Existing callers keep calling this and now hold the canonical object; S7
    moves them to ``AppLogger(__name__)`` directly.

    Parameters
    ----------
    name : str
        Logger name. Conventionally the module's __name__.

    Returns
    -------
    AppLogger
        The application logger for ``name``.
    """
    return AppLogger(name)


def log_enter(ctx: Any, msg: str, logger: AppLogger) -> None:
    """Log function entry through ``AppLogger.enter`` (removed in S6).

    Parameters
    ----------
    ctx : Any
        Unused. Kept so the existing callers are unchanged until S6.
    msg : str
        Entry message describing the function and its key inputs.
    logger : AppLogger
        The caller's logger, from get_logger.
    """
    logger.enter(msg)


def log_exit(ctx: Any, msg: str, logger: AppLogger) -> None:
    """Log function exit through ``AppLogger.exit`` (removed in S6).

    Parameters
    ----------
    ctx : Any
        Unused. Kept so the existing callers are unchanged until S6.
    msg : str
        Exit message describing the outcome.
    logger : AppLogger
        The caller's logger, from get_logger.
    """
    logger.exit(msg)
