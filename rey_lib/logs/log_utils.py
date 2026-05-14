"""
Logging configuration and helpers.

All logging setup is centralised here. No module outside log_utils.py
may call logging.basicConfig(), add handlers, or configure formatters directly.

Log level is environment-aware: DEBUG in dev, INFO in prod. The active level
is written to ctx.log_level after setup_logging() runs so all modules can
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

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from rey_lib.logs.jsonl_handler import JsonlHandler

__all__ = [
    "setup_logging",
    "get_logger",
    "log_enter",
    "log_exit",
]

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

# Environment → default log level.
_ENV_LEVELS: dict[str, str] = {
    "dev":  "DEBUG",
    "prod": "INFO",
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


# Module-level depth mirror — kept in sync with ctx.log_depth.
_current_depth: int = 0


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_logging(ctx: Any, operation: str = "app") -> None:
    """
    Initialise logging for the application.

    Sets up two handlers:
      - Console (stderr): always active, respects log level.
      - JSONL: one file per run, named using ctx.jsonl_path template.

    Both path templates support two placeholders:
      {operation}  — the current operation name (e.g. 'scan', 'import')
      {timestamp}  — run start time as YYYYMMDD_HHMMSS

    Each run produces a distinct JSONL file regardless of restarts or
    parallel executions on the same day. ctx.log_file is set to the
    resolved JSONL path so hooks (e.g. begin_batch) can pass it to the DB.

    The resolved log level is written back to ctx.log_level.

    Parameters
    ----------
    ctx : Any
        Application context Namespace. Must have .env, .log_path, and
        .jsonl_path. ctx.log_level and ctx.log_file are updated in-place.
    operation : str
        Current operation name. Substituted into path templates.
        Defaults to 'app'.
    """
    global _current_depth

    # Prefer an explicit log_level in ctx (set via config), fall back to env default.
    level_name = getattr(ctx, "log_level", None) or _ENV_LEVELS.get(ctx.env, "INFO")
    level      = _LEVEL_MAP.get(level_name.upper(), logging.INFO)

    fmt     = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    formatter = _IndentFormatter(fmt=fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any pre-existing handlers to avoid duplicate output.
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Console handler — always present.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = Path(
        ctx.jsonl_path.format(operation=operation, timestamp=timestamp)
    ).expanduser().resolve()

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.log_level = level_name
    ctx.log_depth = getattr(ctx, "log_depth", 0)
    _current_depth = ctx.log_depth
    jsonl_handler = JsonlHandler(
        jsonl_path = jsonl_path,
        context    = {"env": getattr(ctx, "env", "")},
        ctx        = ctx,
        ctx_fields = tuple(getattr(ctx, "jsonl_ctx_fields", ())),
    )
    jsonl_handler.setLevel(level)
    root.addHandler(jsonl_handler)

    # Expose the JSONL path on ctx so hooks (e.g. begin_batch) can pass it
    # to the database as the canonical log file reference for this run.
    setattr(ctx, "log_file", str(jsonl_path))


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