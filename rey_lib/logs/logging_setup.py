"""Legacy logging helpers: call depth, the indenting console formatter, get_logger.

Logging configuration is AppLogger's (rey_lib.logs.app_logger.AppLogger._configure,
backlog logging.app_logger.s3_configuration). What remains here is migration
state with a scheduled removal: depth and its formatter move into AppLogger in
S4, get_logger becomes a wrapper in S5 and is deleted in S7, and setup_logging
is a delegate kept for its remaining callers until S6.
"""

from __future__ import annotations

import logging
from typing import Any

from rey_lib.logs.app_logger import AppLogger


_INDENT = "  "


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


_current_depth: int = 0


def setup_logging(ctx: Any, operation: str = "app") -> None:
    """Configure logging through ``AppLogger._configure`` (removed in S6).

    The configuration itself is AppLogger's; this delegate keeps the remaining
    callers working until the legacy helpers are removed.
    """
    AppLogger._configure(ctx, operation)


def _initialise_depth(ctx: Any) -> None:
    """Start the call depth at the context's value, or zero.

    Moved verbatim from setup_logging; AppLogger._configure calls it until depth
    moves into AppLogger (backlog logging.app_logger.s4_depth).
    """
    global _current_depth
    ctx.log_depth = getattr(ctx, "log_depth", 0)
    _current_depth = ctx.log_depth


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
