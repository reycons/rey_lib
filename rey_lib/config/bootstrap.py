"""
Shared context bootstrap for Rey app entry points.

Each app is called with a path to the installation config.  The bootstrap
reads the installation config, resolves the active app's config path from
the ``apps`` registry, loads that app config via the existing pipeline,
and stamps installation metadata onto the returned context.

Public API
----------
  build_ctx_for_app(installation_config_path, app_name, project_root)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from rey_lib.config.config_utils import Namespace, build_ctx
from rey_lib.errors.error_utils import ConfigError

__all__ = ["build_ctx_for_app"]


def build_ctx_for_app(
    installation_config_path: Path,
    app_name: str,
    project_root: Optional[Path] = None,
) -> Namespace:
    """Bootstrap an app context from an installation config.

    Bootstrap sequence
    ------------------
    1. Load the installation config.
    2. Read ``installation:`` name from the config.
    3. Resolve ``apps.<app_name>.config_path`` relative to the installation
       config folder.
    4. Derive ``env`` from the app config filename (``config.<env>.yaml``).
    5. Load the app config via the existing ``build_ctx`` pipeline
       (behavior unchanged).
    6. Stamp installation metadata onto the returned context.

    Stamped attributes
    ------------------
    ``ctx.config_path``
        Resolved path to the installation config file.
    ``ctx.config_root``
        Installation config folder.
    ``ctx.installation``
        Installation name from the ``installation:`` key.
    ``ctx.environment``
        Alias for ``ctx.env``.
    ``ctx.apps``
        App registry with each entry's ``config_path`` resolved to an
        absolute ``Path``.  Used by delegation:
        ``ctx.apps.rey_loader.config_path``.

    Parameters
    ----------
    installation_config_path : Path
        Path to the installation config (e.g.
        ``configs/mytrades/config.dev.yaml``).
    app_name : str
        Name of the active app (e.g. ``"trade_analyzer"``).  Must match a
        key under ``apps:`` in the installation config.
    project_root : Path, optional
        App project root directory.  Defaults to ``Path.cwd()``.

    Returns
    -------
    Namespace
        Fully populated app context with installation metadata.

    Raises
    ------
    ConfigError
        On missing file, missing key, or malformed app config filename.
    """
    _config_path = Path(installation_config_path).expanduser().resolve()
    if not _config_path.exists():
        raise ConfigError(f"Installation config not found: {_config_path}")

    if _config_path.is_dir():
        matches = sorted(_config_path.glob("config.*.yaml"))
        if len(matches) != 1:
            raise ConfigError(
                f"Expected exactly one config.*.yaml in '{_config_path}', "
                f"found {len(matches)}."
            )
        _config_path = matches[0]

    installation_raw = _load_yaml(_config_path)
    _config_root     = _config_path.parent

    installation_name: str = installation_raw.get("installation", "")
    if not installation_name:
        raise ConfigError(
            f"Installation config missing 'installation:' key: {_config_path}"
        )

    apps_raw: dict[str, Any] = installation_raw.get("apps", {})
    if app_name not in apps_raw:
        registered = ", ".join(apps_raw) or "(none)"
        raise ConfigError(
            f"App '{app_name}' not found in installation config. "
            f"Registered apps: {registered}"
        )

    app_entry = apps_raw[app_name]
    if not isinstance(app_entry, dict) or "config_path" not in app_entry:
        raise ConfigError(
            f"apps.{app_name} must define a 'config_path' in installation config."
        )

    # Resolve app config path — relative paths resolve from the installation root.
    raw_app_cfg  = app_entry["config_path"]
    app_cfg_path = Path(raw_app_cfg)
    if not app_cfg_path.is_absolute():
        app_cfg_path = (_config_root / app_cfg_path).resolve()
    else:
        app_cfg_path = app_cfg_path.expanduser().resolve()

    if not app_cfg_path.exists():
        raise ConfigError(f"App config not found: {app_cfg_path}")

    # Derive env from app config filename — must be config.<env>.yaml
    stem  = app_cfg_path.stem
    parts = stem.split(".", 1)
    if len(parts) != 2 or parts[0] != "config":
        raise ConfigError(
            f"App config filename must follow 'config.<env>.yaml', "
            f"got: {app_cfg_path.name}"
        )
    env = parts[1]

    # Load the app config — existing pipeline, behavior unchanged.
    ctx = build_ctx(env=env, project_root=project_root, config_dir=app_cfg_path.parent)

    # Build resolved apps registry so delegating apps can pass config paths.
    apps_dict: dict[str, dict[str, Any]] = {}
    for name, entry in apps_raw.items():
        if not isinstance(entry, dict) or "config_path" not in entry:
            continue
        raw = entry["config_path"]
        resolved = (
            (_config_root / raw).resolve()
            if not Path(raw).is_absolute()
            else Path(raw).expanduser().resolve()
        )
        apps_dict[name] = {"config_path": resolved}

    # Stamp installation metadata — runs last so these are never overridden
    # by any same-named key that build_ctx pulled from the app config.
    object.__setattr__(ctx, "config_path",  _config_path)
    object.__setattr__(ctx, "config_root",  _config_root)
    object.__setattr__(ctx, "installation", installation_name)
    object.__setattr__(ctx, "environment",  ctx.env)
    object.__setattr__(ctx, "apps",         Namespace(apps_dict))

    return ctx


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file; return an empty dict on blank files."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}
