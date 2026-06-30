"""
Shared context bootstrap for Rey app entry points.

In the new installation contract, context is built directly from a
config.yaml or app.yaml via build_ctx_from_path.  Pass the path via the
--config-path CLI argument; PathResolver resolves all named paths.

Public API
----------
  build_ctx_for_app(config_path, app_name)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rey_lib.config.config_utils import Namespace, build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError

__all__ = ["build_ctx_for_app"]


def build_ctx_for_app(
    installation_config_path: Path,
    app_name: str,
    project_root: Optional[Path] = None,
) -> Namespace:
    """Bootstrap an app context from a config.yaml or app.yaml.

    Delegates to build_ctx_from_path which loads the file, merges all
    sibling YAML files, resolves the paths: list into a PathResolver,
    and returns a fully populated Namespace.

    If a directory is given, config.yaml is tried first, then app.yaml.

    Parameters
    ----------
    installation_config_path : Path
        Path to a config.yaml, app.yaml, or the directory containing one.
    app_name : str
        Name of the active app. Informational only — not used for path
        lookup in the new contract.
    project_root : Path, optional
        Unused in the new contract; retained for API compatibility.

    Returns
    -------
    Namespace
        Fully populated app context with resolved paths.

    Raises
    ------
    ConfigError
        On missing file or invalid config.
    """
    config_path = Path(installation_config_path).expanduser().resolve()

    if config_path.is_dir():
        for name in ("config.yaml", "app.yaml"):
            candidate = config_path / name
            if candidate.exists():
                config_path = candidate
                break
        else:
            raise ConfigError(
                f"No config.yaml or app.yaml found in: {config_path}"
            )

    return build_ctx_from_path(config_path, app_name=app_name, project_root=project_root)
