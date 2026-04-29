"""
Centralised error handling and generic exception definitions.

All exception catching, formatting, and escalation goes through this module.
No raw except blocks are permitted in any other module. Bare except without
logging and re-raising is forbidden everywhere.

App-specific exception classes must be defined in the application's own
error_utils.py and extend AppError from this module.

Public API
----------
AppError            Generic base exception — extend this in every project.
ConfigError         Raised on invalid or missing configuration.
handle_exception    Log and re-raise with chained traceback context.
validate_env        Validate environment string ('dev' | 'prod').
validate_path       Validate that a required path exists on disk.
validate_required   Validate that a required string value is non-empty.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = [
    "AppError",
    "ConfigError",
    "handle_exception",
    "validate_env",
    "validate_path",
    "validate_required",
]


# ---------------------------------------------------------------------------
# Generic exception hierarchy
# ---------------------------------------------------------------------------

class AppError(Exception):
    """
    Generic base exception for all rey_lib applications.

    Every project defines its own exception hierarchy by extending this class.
    This allows callers to catch rey_lib exceptions at the base level while
    still being able to narrow to project-specific types when needed.

    Example
    -------
    # In your project's error_utils.py:
    from rey_lib.error_utils import AppError

    class MyProjectError(AppError): ...
    class DataImportError(MyProjectError): ...
    """


class ConfigError(AppError):
    """Raised when configuration is invalid, missing, or cannot be loaded."""

class StateError(AppError):
	pass

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
        Optional context object. If present, ctx.env is prepended to the
        log message for easier environment identification.

    Raises
    ------
    AppError
        Always raises — this function never returns normally.
    """
    env_tag = f"[{ctx.env}] " if ctx is not None else ""
    logger.error("%s%s: %s", env_tag, msg, exc, exc_info=True)
    raise new_exc_type(f"{msg}: {exc}") from exc


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

_VALID_ENVS = frozenset({"dev", "prod"})


def validate_env(env: str) -> str:
    """
    Validate that the environment string is one of the allowed values.

    Parameters
    ----------
    env : str
        The environment value to validate (e.g. from a CLI argument).

    Returns
    -------
    str
        The validated environment string, lowercased and stripped.

    Raises
    ------
    ConfigError
        If the value is not a recognised environment name.
    """
    normalised = env.strip().lower()
    if normalised not in _VALID_ENVS:
        raise ConfigError(
            f"Invalid environment '{env}'. "
            f"Must be one of: {', '.join(sorted(_VALID_ENVS))}"
        )
    return normalised


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
