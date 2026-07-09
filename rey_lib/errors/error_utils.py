"""
Centralised error handling and generic exception definitions.

All exception catching, formatting, and escalation goes through this module.
No raw except blocks are permitted in any other module. Bare except without
logging and re-raising is forbidden everywhere.

App-specific exception classes must be defined in the application's own
error_utils.py and extend AppError from this module.

Public API
----------
AppError              Generic base exception — extend this in every project.
ConfigError           Raised on invalid or missing configuration.
DatabaseError         Raised when a database operation fails — connection, DDL, or DML.
StateError            Raised when a JSON state file cannot be read or written.
FtpConnectionError    Raised when an FTP connection cannot be established.
FtpDownloadError      Raised when a file download fails or is incomplete.
handle_exception      Log and re-raise with chained traceback context.
validate_path         Validate that a required path exists on disk.
validate_required     Validate that a required string value is non-empty.
"""

from __future__ import annotations

import logging
import re
import traceback
import uuid
from typing import Any

from rey_lib.logs import get_logger

__all__ = [
    "AppError",
    "ConfigError",
    "DatabaseError",
    "StateError",
    "FtpConnectionError",
    "FtpDownloadError",
    "handle_exception",
    "build_error_record_payload",
    "build_process_failure_payload",
    "build_safe_error_payload",
    "validate_path",
    "validate_required",
]

_logger = get_logger(__name__)

_SECRET_ERROR_RE = re.compile(
    r"(?i)"
    r"("
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"credential|connection[_-]?string|private[_-]?key)"
    r"\s*[:=]\s*"
    r")"
    r"([^,\s;]+)"
)

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def _redact_error_text(value: Any) -> str:
    """Return exception/log text with common secret shapes masked."""
    text = "" if value is None else str(value)
    text = _SECRET_ERROR_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return text


def _sanitize_error_value(value: Any) -> Any:
    """Return an error payload value with secret-like text and keys redacted."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_ERROR_RE.search(f"{key_text}="):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize_error_value(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_error_value(item) for item in value]
    if isinstance(value, str):
        return _redact_error_text(value)
    return value


def _traceback_summary(lines: list[str], *, max_lines: int = 8) -> str:
    """Return a compact traceback summary from sanitized traceback lines."""
    if not lines:
        return ""
    selected = lines[-max_lines:]
    return "".join(selected).strip()


def _diagnostic_summary(value: Any, *, max_chars: int = 4000) -> str:
    """Return a bounded, sanitized diagnostic text summary."""
    text = _redact_error_text(value).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}...[truncated]"


def build_error_record_payload(
    *,
    message: str,
    error_type: str = "",
    error_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Build the canonical sanitized ERROR payload; log_utils writes it."""
    safe_message = _redact_error_text(message)
    return {
        "error_id": str(error_id or uuid.uuid4()),
        "status": "failed",
        "error_type": _redact_error_text(error_type),
        "message": safe_message,
        "error_message": safe_message,
        **_sanitize_error_value(fields),
    }


def build_process_failure_payload(
    *,
    message: str = "",
    error_type: str = "AppExecutionError",
    exit_code: int | None = None,
    stdout: Any = "",
    stderr: Any = "",
    stdout_summary: str = "",
    stderr_summary: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Build canonical sanitized ERROR payload fields for a failed process.

    This helper owns subprocess diagnostic interpretation: stdout/stderr are
    sanitized and bounded before being written to run logs.
    """
    safe_stdout = _diagnostic_summary(stdout_summary if stdout_summary else stdout)
    safe_stderr = _diagnostic_summary(stderr_summary if stderr_summary else stderr)
    detail = safe_stderr or safe_stdout
    if not message:
        message = (
            f"Application exited with code {exit_code}"
            if exit_code is not None else "Application execution failed"
        )
    if detail and message.startswith("Application exited with code"):
        message = f"{message}: {detail}"
    elif not detail and message.startswith("Application exited with code"):
        message = (
            f"{message} and did not emit stderr, stdout failure detail, "
            "exception detail, or canonical child ERROR evidence."
        )

    payload_fields = dict(fields)
    if exit_code is not None:
        payload_fields["exit_code"] = exit_code
    if safe_stdout:
        payload_fields["stdout_summary"] = safe_stdout
    if safe_stderr:
        payload_fields["stderr_summary"] = safe_stderr

    return build_error_record_payload(
        message=message,
        error_type=error_type,
        **payload_fields,
    )


def build_safe_error_payload(
    exc: BaseException,
    *,
    message: str = "",
    include_traceback: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """Build sanitized structured error fields for run-log evidence.

    error_utils owns exception interpretation, traceback shaping, and
    exception-text redaction. log_utils owns the event envelope.
    """
    error_message = _redact_error_text(exc)
    payload: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": _redact_error_text(message or error_message),
        "error_message": error_message,
        "sanitized_exception": error_message,
    }
    if include_traceback:
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        sanitized_lines = [_redact_error_text(line) for line in lines]
        payload["sanitized_traceback"] = "".join(sanitized_lines).strip()
        payload["traceback_summary"] = _traceback_summary(sanitized_lines)
    return build_error_record_payload(**payload, **fields)

# ---------------------------------------------------------------------------
# Generic exception hierarchy
# ---------------------------------------------------------------------------

class AppError(Exception):
    """
    Generic base exception for all rey_lib applications.

    Every project defines its own exception hierarchy by extending this class.
    This allows callers to catch all application errors at the base level while
    still being able to narrow to specific types when needed.

    Example
    -------
    # In your project's error_utils.py:
    from rey_lib.errors.error_utils import AppError

    class MyProjectError(AppError): ...
    class DataImportError(MyProjectError): ...
    """

class ConfigError(AppError):
    """Raised when configuration is invalid, missing, or cannot be loaded."""


class DatabaseError(AppError):
    """Raised when a database operation fails — connection, DDL, or DML."""


class StateError(AppError):
    """Raised when a JSON state file cannot be read, parsed, or written."""


class FtpConnectionError(AppError):
    """Raised when an FTP connection cannot be established or is unexpectedly lost."""


class FtpDownloadError(AppError):
    """Raised when a file download fails or is incomplete."""


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

def handle_exception(
    logger: logging.Logger,
    exc: Exception,
    msg: str,
    new_exc_type: type[AppError] = AppError,
    ctx: Any | None = None,
) -> None:
    """
    Log an exception and re-raise it as an AppError subclass.

    Always uses exception chaining to preserve the original traceback.
    Never silently swallows exceptions.

    Parameters
    ----------
    logger : logging.Logger
        The logger to write the error message to.
    exc : Exception
        The original exception that was caught.
    msg : str
        Human-readable context message describing where the error occurred.
    new_exc_type : type[AppError]
        The exception type to raise. Defaults to AppError.
    ctx : Any | None
        Optional context object. Reserved for future use.

    Raises
    ------
    AppError
        Always raises — this function never returns normally.
    """
    logger.error("%s: %s", msg, exc, exc_info=True)
    raise new_exc_type(f"{msg}: {exc}") from exc


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def validate_path(path: object, label: str, must_exist: bool = True) -> None:
    """
    Validate that a path value is non-None and, optionally, exists on disk.

    Parameters
    ----------
    path : object
        The path value to check. Expected to be a pathlib.Path or str.
    label : str
        Human-readable name for the path (used in error messages).
    must_exist : bool
        When True (default), raise if the path does not exist on disk.

    Raises
    ------
    ConfigError
        If path is None, empty, or does not exist when must_exist is True.
    """
    from pathlib import Path

    if not path:
        raise ConfigError(f"Required path '{label}' is not configured.")

    resolved = Path(str(path))

    if must_exist and not resolved.exists():
        raise ConfigError(
            f"Path '{label}' does not exist on disk: {resolved}"
        )


def validate_required(value: str, label: str) -> str:
    """
    Validate that a required string configuration value is non-empty.

    Parameters
    ----------
    value : str
        The string value to validate.
    label : str
        Human-readable name for the value (used in error messages).

    Returns
    -------
    str
        The validated value, stripped of surrounding whitespace.

    Raises
    ------
    ConfigError
        If the value is empty or whitespace-only.
    """
    stripped = value.strip() if value else ""
    if not stripped:
        raise ConfigError(
            f"Required configuration value '{label}' is missing or empty."
        )
    return stripped
