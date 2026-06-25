"""rey_lib.artifacts — artifact post-processing orchestration.

Owns the routing: given clean artifact content and its type, resolve the
per-type processing config, select the configured engine adapter, run it, and
return the processed content. Engines own the actual formatting/validation
rules; this layer only routes and enforces ``enabled`` / ``fail_on_error``.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.artifacts.engines import get_engine
from rey_lib.artifacts.errors import ArtifactProcessingError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)


def process_artifact(
    content:       str,
    artifact_type: str,
    config:        Optional[dict[str, Any]] = None,
    context:       Optional[dict[str, Any]] = None,
) -> str:
    """Post-process extracted artifact content by artifact type.

    Parameters
    ----------
    content : str
        Clean artifact content (already extracted from the LLM JSON envelope).
    artifact_type : str
        The artifact type (e.g. ``"sql"``). Empty disables processing.
    config : Optional[dict[str, Any]]
        The artifact-processing routing mapping (``artifact_type -> spec``),
        as produced by :func:`rey_lib.artifacts.config.load_artifact_config`.
    context : Optional[dict[str, Any]]
        Reserved for future engine context (unused for SQL).

    Returns
    -------
    str
        Processed content, or the original content unchanged when processing is
        not enabled / not configured for the type.

    Raises
    ------
    ArtifactProcessingError
        When processing is enabled with ``fail_on_error: true`` and the engine
        is unknown or formatting/validation fails.
    """
    if not artifact_type or not config:
        return content

    spec = config.get(artifact_type)
    if not isinstance(spec, dict) or not spec.get("enabled", False):
        return content

    fail_on_error = bool(spec.get("fail_on_error", True))
    engine_name = str(spec.get("engine") or "")
    engine = get_engine(engine_name)

    if engine is None:
        message = (
            "Artifact post-processing failed. Artifact type: "
            f"{artifact_type}. Reason: no engine registered for "
            f"'{engine_name}'. No final output file was written."
        )
        if fail_on_error:
            raise ArtifactProcessingError(message)
        _logger.warning("%s (passing content through)", message)
        return content

    try:
        return engine.process(content, artifact_type, spec)
    except ArtifactProcessingError:
        if fail_on_error:
            raise
        _logger.warning(
            "Artifact post-processing failed for %s via %s — passing content "
            "through (fail_on_error=false).", artifact_type, engine_name,
        )
        return content
