"""
Generic configuration loader.

Reads the main environment config file and all YAML config files under config/,
deep-merges them into a single hierarchy, and returns a Namespace object
that provides attribute-style access to every config value.

Design principles
-----------------
- No application-specific knowledge — works for any project
- Filename of config files is irrelevant — internal YAML hierarchy is
  the source of truth for where values are merged
- All YAML files under config/ are loaded and deep-merged automatically
- Adding a new config file requires only dropping a new YAML file into
  config/ — no code changes
- Path values are resolved automatically based on key name suffix
- Secrets are injected from .env — never from YAML
- Secret injection is the caller's responsibility via inject_secrets() —
  this module has no knowledge of which secrets any project requires

Config file locations
---------------------
  config/config.{env}.yaml          Main config — singleton values only
  config/**/*.yaml                  Additional config files — merged by hierarchy

Runtime state (not from YAML)
------------------------------
  ctx.env          str   — 'dev' | 'prod'
  ctx.log_level    str   — written by log_utils.setup_logging()
  ctx.log_depth    int   — incremented/decremented by log_enter/log_exit

Public API
----------
  build_ctx(env)              Build and return a fully populated Namespace.
  inject_secrets(ctx, map)    Inject .env secrets into ctx at dot-separated paths.
  get_config_path(env)        Return Path to the main config file for env.
  save_config(data, env)      Write a dict back to the main config file.
  print_ctx(ctx)              Log the full context at DEBUG level.
  Namespace                   Attribute-access wrapper around a config dict.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from rey_lib.errors.error_utils import ConfigError, validate_env

_logger = logging.getLogger(__name__)

__all__ = [
    "build_ctx",
    "inject_secrets",
    "get_config_path",
    "save_config",
    "print_ctx",
    "Namespace",
]

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_LIB_DIR      = Path(__file__).parent
_PROJECT_ROOT = _LIB_DIR.parent
_CONFIG_DIR   = _PROJECT_ROOT / "config"
_ENV_FILE     = _PROJECT_ROOT / ".env"

# Keys whose string values are resolved to Path objects.
_PATH_KEY_SUFFIXES = ("_path", "_dir", "_file")
_PATH_KEY_NAMES    = ("path",)

# Keys whose string values are treated as log path templates.
# Placeholders ({operation}, {timestamp}) must survive resolution intact.
_LOG_PATH_KEYS = ("log_path",)

_CONFIG_FILES: dict[str, str] = {
    "dev":  "config.dev.yaml",
    "prod": "config.prod.yaml",
}


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

class Namespace:
    """
    Recursive attribute-access wrapper around a plain dict.

    Supports both attribute access (ctx.config_key) and item access
    (ctx["config_key"]) for nested values. Constructed recursively:
    nested dicts become child Namespace objects, and lists are preserved
    as lists while any dicts inside them are also wrapped as Namespace
    objects.

    Read-only after construction except for runtime state attributes
    (log_level, log_depth) which are written by log_utils.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            object.__setattr__(self, key, _wrap_config_value(value))

    def __getitem__(self, key: str) -> Any:
        """Support dict-style access: ctx["config_key"]."""
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        """Support `"config_key" in ctx`."""
        try:
            object.__getattribute__(self, key)
            return True
        except AttributeError:
            return False

    def keys(self) -> list[str]:
        """Return all public attribute names."""
        return [k for k in self.__dict__ if not k.startswith("_")]

    def values(self) -> list[Any]:
        """Return all attribute values."""
        return [object.__getattribute__(self, k) for k in self.keys()]

    def items(self) -> list[tuple[str, Any]]:
        """Return (key, value) pairs — mirrors dict.items()."""
        return [(k, object.__getattribute__(self, k)) for k in self.keys()]

    def get(self, key: str, default: Any = None) -> Any:
        """Return attribute value or default if not present."""
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            return default

    def __repr__(self) -> str:
        """Human-readable representation showing all keys."""
        pairs = ", ".join(f"{k}={repr(v)}" for k, v in self.items())
        return f"Namespace({pairs})"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ctx(env: str = "dev") -> Namespace:
    """
    Build and return a fully populated Namespace for the given environment.

    Loads the main config file, deep-merges all additional YAML config files,
    resolves paths, and adds runtime state attributes.

    Secret injection is NOT performed here — call inject_secrets() separately
    after build_ctx() with a project-specific secret map.

    Parameters
    ----------
    env : str
        Target environment. Must be 'dev' or 'prod'.

    Returns
    -------
    Namespace
        Fully populated context object. ctx.log_level is written later
        by log_utils.setup_logging().

    Raises
    ------
    ConfigError
        If the environment is invalid or the main config file is missing.
    """
    env = validate_env(env)

    # Load .env into os.environ so inject_secrets() can read from it.
    _load_env_file()

    raw = _load_main_config(env)
    raw = _merge_config_files(raw)

    base = get_config_path(env).parent
    raw  = _resolve_paths(raw, base, parent_key="")

    ctx = Namespace(raw)

    # Runtime state — never from YAML.
    object.__setattr__(ctx, "env",       env)
    object.__setattr__(ctx, "log_level", "INFO")  # overwritten by log_utils
    object.__setattr__(ctx, "log_depth", 0)        # managed by log_enter/log_exit

    return ctx


def inject_secrets(ctx: Namespace, secret_map: dict[str, str]) -> None:
    """
    Read secrets from os.environ and inject them into ctx at their target paths.

    Called by the application after build_ctx() with a project-specific map.
    This module has no knowledge of which secrets any project requires.

    If an intermediate key does not exist in ctx the secret is silently
    skipped — the relevant provider may not be configured in this environment.

    Parameters
    ----------
    ctx : Namespace
        The fully built context object. Modified in-place.
    secret_map : dict[str, str]
        Mapping of env var name → dot-separated ctx path.
        Example: {"FTP_PASSWORD_CLIENTA": "connections.0.ftp.password"}
        For dynamic injection (e.g. per-connection passwords) call
        this function once per secret rather than building the full map
        upfront.

    Example
    -------
    inject_secrets(ctx, {
        "ANTHROPIC_API_KEY": "llm.claude.api_key",
        "OPENAI_API_KEY":    "llm.gpt4o.api_key",
    })
    """
    for env_var, dotted_path in secret_map.items():
        secret_value = os.getenv(env_var, "")
        if not secret_value:
            # Secret not set — skip rather than overwriting with empty string.
            continue

        parts  = dotted_path.split(".")
        target = ctx
        for part in parts[:-1]:
            try:
                target = object.__getattribute__(target, part)
            except AttributeError:
                target = None
                break

        if target is not None:
            object.__setattr__(target, parts[-1], secret_value)


def get_config_path(env: str) -> Path:
    """
    Return the resolved Path to the main config file for the given environment.

    Parameters
    ----------
    env : str
        Target environment. Must be 'dev' or 'prod'.

    Returns
    -------
    Path
        Absolute path to the YAML config file.

    Raises
    ------
    ConfigError
        If the config file does not exist on disk.
    """
    filename = _CONFIG_FILES.get(env)
    if not filename:
        raise ConfigError(f"No config file defined for environment '{env}'.")
    path = _CONFIG_DIR / filename
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    return path


def save_config(data: dict[str, Any], env: str = "dev") -> None:
    """
    Write a plain dict back to the main config file for the given environment.

    Parameters
    ----------
    data : dict
        Full config structure to write. Must be PyYAML-serialisable.
    env : str
        Target environment. Determines which file is written.

    Raises
    ------
    ConfigError
        If the config file path cannot be resolved.
    """
    config_path = get_config_path(env)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False,
                  sort_keys=False, allow_unicode=True)


def print_ctx(ctx: Namespace) -> None:
    """
    Log the full context hierarchy at DEBUG level for diagnostic use.

    Parameters
    ----------
    ctx : Namespace
        The context object to log.
    """
    _logger.debug("=== ctx dump ===")
    _print_namespace(ctx, indent=0)
    _logger.debug("=== end ctx dump ===")


# ---------------------------------------------------------------------------
# Private — loading and merging
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    """Load the .env file from the project root into os.environ."""
    if _ENV_FILE.exists():
        load_dotenv(dotenv_path=_ENV_FILE, override=False)


def _load_main_config(env: str) -> dict[str, Any]:
    """Load and return the main environment config file as a raw dict."""
    return _load_yaml(get_config_path(env))


def _merge_config_files(base: dict[str, Any]) -> dict[str, Any]:
    """
    Load and deep-merge all non-main YAML files under config/ into base.

    Parameters
    ----------
    base : dict
        Starting config dict from the main environment config file.

    Returns
    -------
    dict
        Merged config dict with all sub-config files applied.
    """
    result = dict(base)
    for config_file in sorted(_CONFIG_DIR.rglob("*.yaml")):
        if config_file.name in _CONFIG_FILES.values():
            continue
        config_raw = _load_yaml(config_file)
        result = _deep_merge(result, config_raw)
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file, returning an empty dict on blank files."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override into base, returning a new dict.

    - Nested dicts are merged recursively.
    - Lists are concatenated; dicts with a 'name' key are deduplicated
      so two config files defining the same named entry do not produce
      duplicate entries.
    - All other values are overwritten by override.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            existing_names = {
                item["name"] for item in result[key]
                if isinstance(item, dict) and "name" in item
            }
            new_items = [
                item for item in value
                if not (isinstance(item, dict) and item.get("name") in existing_names)
            ]
            if len(new_items) < len(value):
                _logger.warning(
                    "Config merge: duplicate named entries skipped for key '%s'", key
                )
            result[key] = result[key] + new_items
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Private — path resolution
# ---------------------------------------------------------------------------

def _is_path_key(key: str) -> bool:
    """Return True if the key name indicates a path value."""
    return key in _PATH_KEY_NAMES or any(
        key.endswith(suffix) for suffix in _PATH_KEY_SUFFIXES
    )


def _is_log_path_key(key: str) -> bool:
    """Return True if the key holds a log path template with placeholders."""
    return key in _LOG_PATH_KEYS


def _resolve_path_value(value: str, base: Path) -> Path:
    """Resolve a path string to an absolute Path relative to base."""
    if value.startswith("~"):
        return Path(value).expanduser().resolve()
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def _resolve_log_path_value(value: str, base: Path) -> str:
    """Resolve a log path template string, preserving {operation} and {timestamp}."""
    if value.startswith("~"):
        return str(Path(value).expanduser())
    p = Path(value)
    return str(p) if p.is_absolute() else str((base / p).resolve())


def _resolve_paths(data: dict[str, Any], base: Path, parent_key: str) -> dict[str, Any]:
    """Recursively walk a config dict and resolve path-like string values."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = _resolve_paths(
                value, base, f"{parent_key}.{key}" if parent_key else key
            )
        elif isinstance(value, list):
            result[key] = [
                _resolve_paths(item, base, parent_key)
                if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, str) and _is_log_path_key(key):
            result[key] = _resolve_log_path_value(value, base)
        elif isinstance(value, str) and _is_path_key(key):
            result[key] = _resolve_path_value(value, base)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Private — Namespace construction
# ---------------------------------------------------------------------------

def _wrap_config_value(value: Any) -> Any:
    """Wrap a config value for storage in a Namespace."""
    if isinstance(value, dict):
        return Namespace(value)
    if isinstance(value, list):
        return [_wrap_config_value(item) for item in value]
    return value


def _print_namespace(ns: Namespace, indent: int) -> None:
    """Recursively log a Namespace at DEBUG level."""
    prefix = "  " * indent
    for key, value in ns.items():
        if isinstance(value, Namespace):
            _logger.debug("%s%s:", prefix, key)
            _print_namespace(value, indent + 1)
        elif isinstance(value, list):
            _logger.debug("%s%s: [%d items]", prefix, key, len(value))
        else:
            # Mask secrets — any key containing 'password', 'key', or 'token'
            display = "***" if any(
                s in key.lower() for s in ("password", "key", "token", "secret")
            ) else value
            _logger.debug("%s%s: %s", prefix, key, display)
