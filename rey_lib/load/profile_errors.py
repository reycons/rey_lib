"""The error the Loader's record-type profiling raises (row 589, step 5).

Its own module because both the profile step and the delimited layout functions
it profiles through raise it, and neither may import the other for it. It
replaces the legacy file_operator ``RedactorError`` / ``InputError`` /
``LayoutError`` on the profile path.
"""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError

__all__ = ["ProfilingError"]


class ProfilingError(AppError):
    """Raised when a governed file cannot be profiled."""
