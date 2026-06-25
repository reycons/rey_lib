"""rey_lib.artifacts — exceptions for artifact post-processing."""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError


class ArtifactProcessingError(AppError):
    """Raised when artifact formatting/validation fails and fail_on_error is set.

    Carries a clear, human-readable message. When this is raised the caller
    must not write a final artifact file.
    """
