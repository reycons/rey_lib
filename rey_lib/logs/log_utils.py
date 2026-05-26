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

import json
import logging
from datetime import datetime
from pathlib import Path
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
    "read_jsonl_records",
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
      - Console (stderr): always active, respects log level.
      - Human-readable file: when ctx.log_path is present.
      - JSONL: active by default unless disabled in ctx.logging.jsonl_enabled
        or ctx.jsonl_enabled.

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
        Application context Namespace. Must have .env. Optionally has
        .log_path and/or .jsonl_path. ctx.log_level and ctx.log_file
        are updated in-place after setup.
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = None

    resolved_log: Path | None = None
    if getattr(ctx, "log_path", None) and _log_bool(ctx, "readable_enabled", True):
        resolved_log = _resolve_log_path(ctx.log_path, ctx, operation, timestamp)
        file_handler = _TextFileHandler(resolved_log)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_ProviderWarningFilter())
        root.addHandler(file_handler)
        log_file = str(resolved_log)

    if _log_bool(ctx, "jsonl_enabled", True):
        jsonl_path = _resolve_jsonl_path(ctx, operation, timestamp, resolved_log)
        jsonl_handler = JsonlHandler(
            jsonl_path = jsonl_path,
            context    = {"env": getattr(ctx, "env", "")},
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
