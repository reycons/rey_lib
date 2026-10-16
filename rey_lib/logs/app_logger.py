"""AppLogger, the application logging object.

``AppLogger(name)`` is how application code logs. It wraps the stdlib logger of
that name, so every record still travels the one Python logging pipeline and
its handlers; this object adds no second path.

The public surface is what application code is allowed to do: ``debug``,
``info``, ``warning`` and ``isEnabledFor``. There is no ``error``,
``exception``, ``critical`` or ``setLevel`` -- an ERROR record is AppError's to
emit (rule 79), through the private ``_error`` that only ``AppError.report``
calls, and configuring the logging pipeline is not a caller's job: it is the
private ``_configure`` that only the launch boundary calls.

Records are attributed to the caller, not to this wrapper: each method raises
the stdlib ``stacklevel`` by one, so ``funcName``, ``lineno`` and ``pathname``
name the line that called ``AppLogger``.

Configuration
-------------
``AppLogger._configure(ctx, operation, writer)`` owns the process-wide logging pipeline:
the root logger's level and filters, the console and text-file handlers, the
handler that hands every record to the logging writer it is given,
the stderr bridge, and the levels of named third-party libraries. It is the one
place the pipeline is built, and it runs when the launch boundary
(``rey_lib.config.bootstrap.build_ctx_for_app``) calls it -- constructing an
``AppLogger`` never configures anything. It is private because configuration
is owned here but is not part of the application logging surface; the rule
gate names its callers. Calling it again rebuilds the pipeline: existing root
handlers are removed and closed first, so there is one set of handlers, not one
per call.

Call depth
----------
``AppLogger.log_depth`` is the one call depth, shared by every instance in the
process: ``enter(msg)`` logs ``-> msg`` at DEBUG and then increments it,
``exit(msg)`` decrements it (never below zero) and then logs ``<- msg``. The
configured handlers stamp it onto each record as ``record.log_depth`` before
the record is formatted or written; the console formatter indents by it and the
JSONL handler records it. Neither consumer knows where it comes from
(backlog logging.app_logger.s4_depth).
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rey_lib.errors.error_utils import AppError
from rey_lib.logs.jsonl_handler import JsonlHandler
from rey_lib.logs.record_enrichment import require_run_id


_INDENT = "  "

_LEVEL_MAP: dict[str, int] = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
}

#: Third-party loggers whose level the configuration sets, whatever the run's
#: level. SQLFluff logs parse trees and diagnostics verbosely; raising it to
#: WARNING keeps ``lint-sql`` output clean (formerly rey_db_admin's
#: ``_quiet_sqlfluff``, which set it at the boundary itself).
_LIBRARY_LEVELS: dict[str, int] = {
    "sqlfluff": logging.WARNING,
}


class AppLogger:
    """The application logger for one logger name.

    Args:
        name: Logger name. Conventionally the module's ``__name__``.

    Attributes:
        log_depth: The process-wide call depth. A class attribute, so every
            ``AppLogger`` reads and moves the same value; ``enter`` and
            ``exit`` are the only writers.
    """

    __slots__ = ("_logger",)

    log_depth: int = 0

    #: The process's logging writer and record builder, set by ``_configure``.
    #: Private: the launch boundary hands them to the run log it opens.
    _writer: Any = None
    _builder: Any = None

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at DEBUG; keyword arguments such as ``extra`` pass through."""
        self._logger.debug(msg, *args, **_from_caller(kwargs))

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at INFO; keyword arguments such as ``extra`` pass through."""
        self._logger.info(msg, *args, **_from_caller(kwargs))

    def warning(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at WARNING; keyword arguments such as ``extra`` pass through."""
        self._logger.warning(msg, *args, **_from_caller(kwargs))

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 -- the stdlib name callers use
        """Return whether a record at ``level`` would be processed by this logger."""
        return self._logger.isEnabledFor(level)

    def enter(self, msg: str) -> None:
        """Log entry to ``msg`` at DEBUG, then go one level deeper."""
        self._logger.debug("→ %s", msg, **_from_caller({}))
        AppLogger.log_depth += 1

    def exit(self, msg: str) -> None:
        """Come back one level (never below zero), then log the exit at DEBUG."""
        AppLogger.log_depth = max(0, AppLogger.log_depth - 1)
        self._logger.debug("← %s", msg, **_from_caller({}))

    def _error(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR. Private: AppError.report is its only caller (rule 79).

        Not part of the application surface. ``AppError.report`` passes the
        operational context as ``msg`` and the error itself as ``exc_info``.
        """
        self._logger.error(msg, *args, **_from_caller(kwargs))

    @classmethod
    def _configure(cls, ctx: Any, operation: str, writer: Any) -> None:
        """
        Initialise logging for the application.

        Sets up handlers based on what is configured in ctx:
          - Console (stderr): the live transport, for a reader watching a terminal.
            Removed once the durable output is live if stdout is not a terminal,
            because a redirected stream turns every governed record into a second
            text copy of the run log at a destination configuration never named.
          - The logging writer: every record is built once by ``LogRecordBuilder``
            and handed to ``writer``, which writes it to the outputs it holds
            (``rey_lib.logs.log_writer``). Which outputs those are is the writer's;
            this method knows none of them.
          - Human-readable file: opt-in legacy only (readable_enabled=true). New runs
            do not produce a separate human-readable execution log
            (SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup).

        The readable path template supports two placeholders:
          {operation}  — the current operation name (e.g. 'scan', 'import')
          {timestamp}  — run start time as YYYYMMDD_HHMMSS

        ctx.log_file is set to the writer's JSONL path when it writes one,
        otherwise the human-readable log path when that is enabled.

        The resolved log level is written back to ctx.log_level. Named
        third-party libraries get their configured level (``_LIBRARY_LEVELS``).

        Parameters
        ----------
        ctx : Any
            Application context Namespace. Optionally has .log_path and/or
            .jsonl_path. ctx.log_level and ctx.log_file are updated in-place
            after setup.
        operation : str
            Current operation name. Substituted into path templates.
        writer : LogWriter
            The process's logging writer, assembled by the caller
            (``rey_lib.logs.log_writer.build_log_writer``).
        """
        # Read, never resolved. _settle_log_level owns the precedence chain --
        # command line, application declaration, installation, default -- and hands
        # this one settled value over. The fallbacks here are for a context that
        # never went through settlement at all, not a second opinion about it.
        level_name = getattr(ctx, "log_level", None) or "ERROR"
        level      = _LEVEL_MAP.get(level_name.upper(), logging.ERROR)

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
        console_handler.addFilter(_DepthFilter())
        root.addHandler(console_handler)

        for library, library_level in _LIBRARY_LEVELS.items():
            logging.getLogger(library).setLevel(library_level)

        # The run identity is already established by the launch boundary; this
        # requires it so run_id exists before the first log record, and the log
        # filename uses the stable run_timestamp
        # (SGC_Rey_Run_ID_Standard). The run log is a run-created artifact and follows
        # the same <name>.<run_timestamp>.<ext> convention as every other artifact.
        require_run_id(ctx)

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
                file_handler.addFilter(_DepthFilter())
                root.addHandler(file_handler)
                log_file = str(resolved_log)

        from rey_lib.logs.log_record_builder import LogRecordBuilder

        # Every record is built once and handed to the writer (backlog
        # logging.app_logger.s8_json_output_cleanup).
        builder = LogRecordBuilder(
            run_id        = ctx.run_id,
            run_timestamp = ctx.run_timestamp,
            app           = str(getattr(ctx, "owner_app_name", "")
                                or getattr(ctx, "app_name", "")
                                or getattr(ctx, "name", "") or ""),
            context       = {"run_started_at": getattr(ctx, "run_started_at", "")},
            ctx           = ctx,
            ctx_fields    = tuple(getattr(ctx, "jsonl_ctx_fields", ())),
        )
        output_handler = JsonlHandler(builder, writer)
        output_handler.setLevel(level)
        output_handler.addFilter(_ProviderWarningFilter())
        output_handler.addFilter(_DepthFilter())
        root.addHandler(output_handler)
        cls._writer = writer
        cls._builder = builder
        if writer.json_path() is not None:
            log_file = str(writer.json_path())  # JSONL takes precedence for ctx.log_file

        # The live transport is for a reader watching a terminal. When stderr is
        # not a terminal it is a file or a pipe somebody redirected, and every
        # governed record written there becomes a second, text copy of the run
        # log at a destination configuration never named -- which is the one
        # thing the durable run log exists to be the only one of.
        #
        # Capturing stderr before this point is right: there is no governed
        # logger yet, and a run log at a configured destination cannot record
        # that configuration failing to resolve. That capture ends here. After
        # it, an error belongs in the run log, so anything still writing to
        # stderr is an ungoverned error path -- a defect to fix or a bridge to
        # add, not a second log to keep.
        if not _stderr_is_watched():
            root.removeHandler(console_handler)
            console_handler.close()
            _bridge_stderr_to_logger()

        ctx.log_level = level_name

        # ctx.log_file used by hooks (e.g. begin_batch) to record the log path.
        # JSONL path takes precedence when both are configured.
        if log_file:
            setattr(ctx, "log_file", log_file)


def _from_caller(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``kwargs`` with ``stacklevel`` raised past this wrapper's frame."""
    return {**kwargs, "stacklevel": kwargs.get("stacklevel", 1) + 1}


class _DepthFilter(logging.Filter):
    """Stamp every record with the current ``AppLogger.log_depth``.

    Attached to each configured handler, so the depth is on the record before
    the formatter or the JSONL handler reads it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True after setting record.log_depth."""
        record.log_depth = AppLogger.log_depth  # type: ignore[attr-defined]
        return True


class _IndentFormatter(logging.Formatter):
    """Console formatter that indents the message by the record's call depth."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record, indenting only the message portion.

        Timestamp, level, and module name remain left-aligned and fixed width.
        Only the message is indented to reflect call depth.
        """
        indent   = _INDENT * record.log_depth  # type: ignore[attr-defined]
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


def redacted_url(url: Any) -> str:
    """``url`` as scheme, host, port and path only: no query, fragment or userinfo.

    What may be logged about an HTTP request. A query or a fragment can carry
    keys and data values, and userinfo carries credentials.
    """
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _redact_httpx_record(record: logging.LogRecord) -> None:
    """Remove query, fragment and userinfo from the URLs in an httpx record.

    httpx logs every request at INFO with its full URL ('HTTP Request: GET
    <url> ...'). Method, host, path and status stay; only the URL's sensitive
    parts go.
    """
    if record.name != "httpx" or not isinstance(record.args, tuple):
        return
    record.args = tuple(
        redacted_url(arg) if str(arg).startswith(("http://", "https://")) else arg
        for arg in record.args
    )


class _ProviderWarningFilter(logging.Filter):
    """Promote provider back-pressure messages that libraries log too softly,
    and keep URL query values out of httpx's request records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True after redacting httpx URLs and promoting known provider
        warning messages."""
        _redact_httpx_record(record)
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


class _UngovernedStderr:
    """Post-handover stderr, reported through the logger instead of a file.

    The startup capture is still open at the file-descriptor level, so anything
    written to stderr after handover would keep accumulating in it -- a shadow
    text log growing beside the run log nobody asked for.

    Writing here is not an error in itself; writing here *after* the governed
    logger exists is, because an error that never reaches the run log is an
    error nobody finds. So each line becomes a governed record naming the path
    it took, which makes an ungoverned write visible enough to fix or bridge
    deliberately rather than quietly persisted.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._buffer = ""
        self._reporting = False

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._report(line)
        return len(text)

    def _report(self, line: str) -> None:
        # A report that itself wrote to stderr would recurse; nothing routes
        # there now, but the guard keeps that true if a handler is ever added.
        if self._reporting or not line.strip():
            return
        self._reporting = True
        try:
            # Handled -- the write is absorbed -- so reported here, through
            # AppError (rule 79). The line is the error's evidence and goes
            # through sanitization with it; the context names only the event.
            error = AppError(f"ungoverned stderr write: {line}")
            error.report("ungoverned stderr write", source="rey_lib.logs")
        finally:
            self._reporting = False

    def flush(self) -> None:
        if self._buffer.strip():
            self._report(self._buffer)
        self._buffer = ""

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _bridge_stderr_to_logger() -> None:
    """Send post-handover stderr to the run log rather than the startup capture."""
    if isinstance(sys.stderr, _UngovernedStderr):
        return
    sys.stderr = _UngovernedStderr(sys.stderr)


def _stderr_is_watched() -> bool:
    """Whether anything is reading stderr as the process writes it.

    The console handler writes to stderr, so stderr is the stream that decides.
    The question is not what kind of handle it is but whether a reader is on the
    other end -- which is what the console handler exists to serve.

    Three answers, because ``isatty()`` alone conflates two opposite cases:

    * a **terminal** -- someone is watching it.
    * a **pipe** -- something is reading it, or the pipe would not exist. A
      Console-launched run is this: the parent captures the stream and shows it
      live. Treating it as unwatched is what silenced live output.
    * a **regular file** -- nobody is reading it as it is written. This is the
      startup capture, and writing governed records there is the shadow text
      copy of the run log that this handover exists to end.

    A stream that cannot answer is treated as watched, so an unusual environment
    keeps its output rather than losing it silently.
    """
    try:
        if sys.stderr.isatty():
            return True
    except (AttributeError, ValueError, OSError):
        return True
    try:
        mode = os.fstat(sys.stderr.fileno()).st_mode
    except (AttributeError, ValueError, OSError):
        return True
    # A regular file is the capture; anything else -- pipe, socket, character
    # device -- has a reader or is a terminal-like stream.
    return not stat.S_ISREG(mode)
