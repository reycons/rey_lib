"""rey_lib.artifacts — public API.

The stable entry point for artifact post-processing. Callers (the LLM runner,
the CLI, and later Rey Console) use ``process_artifact`` and never import an
engine directly.
"""

from __future__ import annotations

from rey_lib.artifacts.config import artifact_config_from_ctx
from rey_lib.artifacts.post_processor import process_artifact

__all__ = ["artifact_config_from_ctx", "process_artifact"]
