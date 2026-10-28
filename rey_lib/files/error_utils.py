"""
App-specific exceptions for file_operator.

All exceptions extend rey_lib AppError so that the shared handle_exception
helper can catch, log, and format them uniformly.

Public API
----------
RedactorError    Base exception for all file_operator failures.
InputError       Raised when input files cannot be resolved.
LayoutError      Raised when a file layout cannot be determined.
"""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError

__all__: list[str] = ["RedactorError", "InputError", "LayoutError"]


class RedactorError(AppError):
    """Base exception for all file_operator failures."""


class InputError(RedactorError):
    """Raised when input files cannot be resolved or read."""


class LayoutError(RedactorError):
    """Raised when a file layout cannot be determined from its extension."""
