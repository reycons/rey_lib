"""rey_lib.artifacts — shared artifact post-processing framework.

Formats, lints, and validates generated artifacts after LLM envelope extraction
and before the final file is written. Engines (SQLFluff for SQL today; ruff,
shfmt, ... later) sit behind a Rey abstraction so application and pipeline code
never call a specific formatter directly.

Public API
----------
process_artifact            Post-process artifact content by artifact_type.
lint_artifact               Return diagnostics without changing the artifact.
artifact_config_from_ctx    Read the artifact_processing routing config from ctx.
validate_artifact_processing  Validate effective artifact_processing routes (ctx).
ArtifactProcessingError     Raised on a hard processing failure.
"""

from __future__ import annotations

from rey_lib.artifacts.api import (
    artifact_config_from_ctx,
    lint_artifact,
    process_artifact,
    validate_artifact_processing,
)
from rey_lib.artifacts.envelope import (
    ARTIFACT_TYPE_FIELD,
    CONTENT_FIELD,
    NOTES_FIELD,
    REJECTION_PREFIX,
    build_envelope_instruction,
    extract_artifact,
    extract_artifact_envelope,
    loads_llm_json,
    rejection_reason_from_notes,
)
from rey_lib.artifacts.errors import ArtifactEnvelopeError, ArtifactProcessingError
from rey_lib.artifacts.store import ArtifactStore, LocalArtifactStore

__all__ = [
    "ARTIFACT_TYPE_FIELD",
    "CONTENT_FIELD",
    "NOTES_FIELD",
    "REJECTION_PREFIX",
    "ArtifactEnvelopeError",
    "ArtifactProcessingError",
    "ArtifactStore",
    "LocalArtifactStore",
    "build_envelope_instruction",
    "extract_artifact",
    "extract_artifact_envelope",
    "loads_llm_json",
    "rejection_reason_from_notes",
    "artifact_config_from_ctx",
    "lint_artifact",
    "process_artifact",
    "validate_artifact_processing",
]
