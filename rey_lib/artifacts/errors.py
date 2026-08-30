"""rey_lib.artifacts — exceptions for artifact post-processing."""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError


class ArtifactProcessingError(AppError):
    """Raised when artifact formatting/validation fails and fail_on_error is set.

    Carries a clear, human-readable message. When this is raised the caller
    must not write a final artifact file.
    """


class ArtifactEnvelopeError(ArtifactProcessingError):
    """The artifact envelope could not be read.

    An envelope that cannot be parsed, or that carries no content, is a failed
    artifact rather than a failed model call -- which is why this is an artifact
    error and not an AI one. It replaces the old ``rey_lib.llm.exceptions``
    ``ParseFailure`` for envelope reading; a stored artifact is read long after
    whatever produced it, so its failures belong to the package that owns
    artifacts.
    """
