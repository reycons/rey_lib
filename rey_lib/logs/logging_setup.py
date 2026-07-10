"""Logging setup, handler attachment, and depth helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rey_lib.logs.jsonl_handler import JsonlHandler
from rey_lib.logs.record_enrichment import resolve_run_identity


_INDENT = "  "


_LEVEL_MAP: dict[str, int] = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
}


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


_current_depth: int = 0


def setup_logging(ctx: Any, operation: str = "app") -> None:
    """
    Initialise logging for the application.

    Sets up handlers based on what is configured in ctx:
      - Console (stderr): always active, respects log level (live transport).
      - JSONL: active by default unless disabled in ctx.logging.jsonl_enabled
        or ctx.jsonl_enabled. This is the single durable execution log.
      - Human-readable file: opt-in legacy only (readable_enabled=true). New runs
        do not produce a separate human-readable execution log
        (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup); readable views are
        rendered on demand from the JSONL run log via the render_* helpers.

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
        Application context Namespace. Optionally has .log_path and/or
        .jsonl_path. ctx.log_level and ctx.log_file are updated in-place
        after setup.
    operation : str
        Current operation name. Substituted into path templates.
        Defaults to 'app'.
    """
    global _current_depth

    level_name = getattr(ctx, "log_level", None) or "INFO"
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

    # Establish the run identity before any handler so run_id exists before the
    # first log record, and the log filename uses the stable run_timestamp
    # (SGC_Rey_Run_ID_Standard). The run log is a run-created artifact and follows
    # the same <name>.<run_timestamp>.<ext> convention as every other artifact.
    resolve_run_identity(ctx)
    timestamp = ctx.run_timestamp
    log_file  = None

    # The append-only JSONL run log is the single durable execution log
    # (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup). A separate human-readable
    # execution log is no longer produced for new runs; it is opt-in legacy only via
    # readable_enabled=true. When log_path is configured it is still resolved so the
    # JSONL log is written beside it, but the text handler is added only when opted in.
    resolved_log: Path | None = None
    if getattr(ctx, "log_path", None):
        resolved_log = _resolve_log_path(ctx.log_path, ctx, operation, timestamp)
        if _log_bool(ctx, "readable_enabled", False):
            file_handler = _TextFileHandler(resolved_log)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(_ProviderWarningFilter())
            root.addHandler(file_handler)
            log_file = str(resolved_log)

    if _log_bool(ctx, "jsonl_enabled", True):
        jsonl_path = _resolve_jsonl_path(ctx, operation, timestamp, resolved_log)
        # Every run log record carries the run identity so records can be correlated
        # to the run regardless of filename.
        jsonl_handler = JsonlHandler(
            jsonl_path = jsonl_path,
            context    = {
                "run_id":         ctx.run_id,
                "run_timestamp":  ctx.run_timestamp,
                "run_started_at": getattr(ctx, "run_started_at", ""),
            },
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
