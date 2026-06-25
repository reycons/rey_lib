"""rey_lib.artifacts — runtime config validation (Path 1).

Validates the EFFECTIVE merged configuration from the ctx — not a raw YAML
source file — so it checks what Rey will actually run after config_loading has
merged and resolved everything. First pass: validate the artifact_processing
routes (each enabled route must name a registered engine, and any config_path
must resolve to an existing file).

Broader effective-config validation (required keys, invalid app/provider names,
named-path availability) is intentionally out of scope for this first pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.artifacts.config import artifact_config_from_ctx
from rey_lib.artifacts.engines import get_engine
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

# Provenance for artifact_processing routes (best-effort source attribution).
_SOURCE = "configs/shared/artifact_processing.yaml"


def validate_artifact_processing(ctx: Any) -> list[str]:
    """Validate the effective artifact_processing routes from ctx.

    Parameters
    ----------
    ctx : Any
        Application context built from the installation config.

    Returns
    -------
    list[str]
        Human-readable validation error messages with provenance. Empty when
        the effective artifact_processing config is valid.
    """
    errors: list[str] = []
    config = artifact_config_from_ctx(ctx)

    for artifact_type, spec in config.items():
        if not isinstance(spec, dict) or not spec.get("enabled", False):
            continue

        engine_name = str(spec.get("engine") or "")
        if get_engine(engine_name) is None:
            errors.append(
                "Config validation failed.\n\n"
                "Resolved key:\n"
                f"artifact_processing.{artifact_type}.engine\n\n"
                "Reason:\n"
                f"No engine registered for '{engine_name}'.\n\n"
                f"Source:\n{_SOURCE}"
            )

        config_path = spec.get("config_path")
        if config_path and not Path(str(config_path)).is_file():
            errors.append(
                "Config validation failed.\n\n"
                "Resolved key:\n"
                f"artifact_processing.{artifact_type}.config_path\n\n"
                "Reason:\n"
                "Resolved path does not exist.\n\n"
                "Resolved value:\n"
                f"{config_path}\n\n"
                f"Source:\n{_SOURCE}"
            )

    return errors
