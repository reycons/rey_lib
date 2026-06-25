"""rey_lib.artifacts — artifact-processing configuration (from ctx).

The routing config (``configs/shared/artifact_processing.yaml``) is loaded into
the application context by ``config_loading``, with its ``config_path`` tokens
already resolved to absolute paths. This module reads the config from the ctx —
it never reads or parses the YAML file directly. All callers (pipeline and CLI)
obtain the config the same way.
"""

from __future__ import annotations

from typing import Any


def artifact_config_from_ctx(ctx: Any) -> dict[str, Any]:
    """Return the artifact_processing routing config from ctx as a plain dict.

    Reads ``ctx.artifact_processing`` (a Namespace populated by config_loading)
    and converts it to ``{artifact_type: {enabled, engine, config_path, ...}}``.

    Parameters
    ----------
    ctx : Any
        Application context built from the installation config.

    Returns
    -------
    dict[str, Any]
        The routing mapping, or ``{}`` when artifact processing is not
        configured for the installation.
    """
    processing = getattr(ctx, "artifact_processing", None)
    if processing is None:
        return {}

    result: dict[str, Any] = {}
    for artifact_type in vars(processing):
        spec = getattr(processing, artifact_type)
        if isinstance(spec, dict):
            result[artifact_type] = dict(spec)
        elif hasattr(spec, "__dict__"):
            result[artifact_type] = {key: getattr(spec, key) for key in vars(spec)}
    return result
