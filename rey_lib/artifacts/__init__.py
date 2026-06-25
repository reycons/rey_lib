"""rey_lib.artifacts — shared artifact post-processing framework.

Formats, lints, and validates generated artifacts after LLM envelope extraction
and before the final file is written. Engines (SQLFluff for SQL today; ruff,
shfmt, ... later) sit behind a Rey abstraction so application and pipeline code
never call a specific formatter directly.

Public API
----------
process_artifact         Post-process artifact content by artifact_type.
artifact_config_from_ctx Read the artifact_processing routing config from ctx.
ArtifactProcessingError  Raised on a hard processing failure.
"""

from __future__ import annotations

from rey_lib.artifacts.api import artifact_config_from_ctx, process_artifact
from rey_lib.artifacts.errors import ArtifactProcessingError

__all__ = ["ArtifactProcessingError", "artifact_config_from_ctx", "process_artifact"]
