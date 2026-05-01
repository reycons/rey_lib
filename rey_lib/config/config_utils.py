"""
Generic configuration loader.

Reads the main environment config file and all YAML config files under config/,
deep-merges them into a single hierarchy, and returns a Namespace object
that provides attribute-style access to every config value.

Design principles
-----------------
- No application-specific knowledge — works for any project
- project_root is passed explicitly — this module never assumes its own
  location on disk, which would break when installed as a pip package
- All YAML files under config/ are loaded and deep-merged automatically
- Adding a new config file requires only dropping a new YAML file into
  config/ — no code changes
- Path values are resolved automatically based on key name suffix
- Secrets are injected from .env — never from YAML
- Secret injection is the caller's responsibility via inject_secrets()

Config file locations
---------------------
  <project_root>/config/config.{env}.yaml     Main config — singleton values only
  <project_root>/config/**/*.yaml             Additional config files — merged by hierarchy

Runtime state (not from YAML)
------------------------------
  ctx.env          str   — 'dev' | 'prod'
  ctx.log_level    str   — written by log_utils.setup_logging()
  ctx.log_depth    int   — incremented/decremented by log_enter/log_exit

Public API
----------
  build_ctx(env, project_root)    Build and return a fully populated Namespace.
  inject_secrets(ctx, map)        Inject .env secrets into ctx at dot-separated paths.
  save_config(data, env, ...)     Write a dict back to the main config file.
  print_ctx(ctx)                  Log the full context at DEBUG level.
  Namespace                       Attribute-access wrapper around a config dict.
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
    "save_config",
    "print_ctx",
    "Namespace",
]

# Config directory name and main config filename pattern — constant across all projects.
_CONFIG_DIR_NAME   = "config"
_CONFIG_FILE_NAMES = {"dev": "config.dev.yaml", "prod": "config.prod.yaml"}
_ENV_FILE_NAME     = ".env"

# Keys whose string values are resolved to Path objects.
_PATH_KEY_SUFFIXES = ("_path", "_dir", "_file")
_PATH_KEY_NAMES    = ("path",)

# Keys whose string values are log path templates — placeholders must survive resolution.
_LOG_PATH_KEYS = ("log_path",)


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

class Namespace:
    """
    Recursive attribute-access wrapper around a plain dict.

    Supports both attribute access (ctx.key) and item access (ctx["key"]).
    Nested dicts become child Namespace objects. Lists are preserved with
    any dict items inside them also wrapped as Namespace objects.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            object.__setattr__(self, key, _wrap_config_value(value))

    def __getitem__(self, key: str) -> Any:
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            object.__getattribute__(self, key)
            return True
        except AttributeError:
            return False

    def keys(self) -> list[str]:
        return [k for k in self.__dict__ if not k.startswith("_")]

    def values(self) -> list[Any]:
        return [object.__getattribute__(self, k) for k in self.keys()]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, object.__getattribute__(self, k)) for k in self.keys()]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            return default

    def __repr__(self) -> str:
        pairs = ", ".join(f"{k}={repr(v)}" for k, v in self.items())
        return f"Namespace({pairs})"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ctx(
    env: str = "dev",
    project_root: Path | None = None,
    config_dir: Path | None = None,
) -> Namespace:
    """
    Build and return a fully populated Namespace for the given environment.

    Loads the main config file, deep-merges all additional YAML config files
    found under config_dir, resolves paths, and adds runtime state attributes.

    Secret injection is NOT performed here — call inject_secrets() separately
    after build_ctx() with a project-specific secret map.

    Parameters
    ----------
    env : str
        Target environment. Must be 'dev' or 'prod'.
    project_root : Path | None
        Root directory of the calling project. .env is resolved relative
        to this path. Defaults to Path.cwd() when None.
    config_dir : Path | None
        Directory containing config YAML files. When provided, overrides
        the default <project_root>/config/ location. Use this to point to
        a shared config directory outside the project folder.
        Reads APP_CONFIG_DIR from environment if neither is provided.

    Returns
    -------
    Namespace
        Fully populated context object.

    Raises
    ------
    ConfigError
        If the environment is invalid or the main config file is missing.
    """
    if project_root is None:
        project_root = Path.cwd()

    project_root = Path(project_root).resolve()
    env_file     = project_root / _ENV_FILE_NAME

    # Config dir priority: explicit parameter > APP_CONFIG_DIR env var > default
    if config_dir is None:
        env_config_dir = os.getenv("APP_CONFIG_DIR")
        config_dir = Path(env_config_dir) if env_config_dir else project_root / _CONFIG_DIR_NAME

    config_dir = Path(config_dir).resolve()
    env        = validate_env(env)
    _load_env_file(env_file)

    raw = _load_main_config(env, config_dir)
    raw = _merge_config_files(raw, config_dir)
    raw = _resolve_paths(raw, config_dir, parent_key="")

    ctx = Namespace(raw)
    _inject_env_blocks(ctx)
    
    # Runtime state — never from YAML.
    object.__setattr__(ctx, "env",       env)
    object.__setattr__(ctx, "log_level", "INFO")   # overwritten by log_utils
    object.__setattr__(ctx, "log_depth", 0)         # managed by log_enter/log_exit

    return ctx


def inject_secrets(ctx: Namespace, secret_map: dict[str, str]) -> None:
    """
    Read secrets from os.environ and inject them into ctx at their target paths.

    Target paths are dot-separated strings that walk Namespace attributes.
    For dynamic injection (e.g. per-connection FTP passwords) inject directly
    into the connection Namespace using object.__setattr__ after build_ctx().

    Parameters
    ----------
    ctx : Namespace
        The fully built context object. Modified in-place.
    secret_map : dict[str, str]
        Mapping of env var name → dot-separated ctx path.
        Example: {"ANTHROPIC_API_KEY": "llm.claude.api_key"}
    """
    for env_var, dotted_path in secret_map.items():
        secret_value = os.getenv(env_var, "")
        if not secret_value:
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


def save_config(
    data: dict[str, Any],
    env: str = "dev",
    project_root: Path | None = None,
) -> None:
    """
    Write a plain dict back to the main config file for the given environment.

    Parameters
    ----------
    data : dict
        Full config structure to write.
    env : str
        Target environment.
    project_root : Path | None
        Project root. Defaults to Path.cwd().
    """
    if project_root is None:
        project_root = Path.cwd()
    config_dir  = Path(project_root) / _CONFIG_DIR_NAME
    config_path = _config_path(env, config_dir)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


def print_ctx(ctx: Namespace) -> None:
    """Log the full context hierarchy at DEBUG level for diagnostic use."""
    _logger.debug("=== ctx dump ===")
    _print_namespace(ctx, indent=0)
    _logger.debug("=== end ctx dump ===")


# ---------------------------------------------------------------------------
# Private — loading and merging
# ---------------------------------------------------------------------------
def _inject_env_blocks(ns: Namespace) -> None:
    """
    Recursively scan a Namespace for 'env:' child blocks and inject
    os.environ values into the parent Namespace.

    Any Namespace with an 'env' attribute is treated as a secret
    injection map. Each key under 'env' is the target attribute name,
    each value is the os.environ variable name to read.

    Example YAML:
        env:
          password: SQLSERVER_NAVICONTROL_PASSWORD

    Result: parent.password = os.getenv("SQLSERVER_NAVICONTROL_PASSWORD")
    """
    for key, value in ns.items():
        if key == "env" and isinstance(value, Namespace):
            # value is the env block — parent is the target Namespace.
            # Walk up handled by caller passing the parent.
            continue
        if isinstance(value, Namespace):
            env_block = getattr(value, "env", None)
            if isinstance(env_block, Namespace):
                for attr, env_var in env_block.items():
                    secret = os.getenv(str(env_var), "")
                    if secret:
                        object.__setattr__(value, attr, secret)
                    else:
                        _logger.warning(
                            "Secret not found for '%s' — expected env var '%s' in .env.",
                            attr, env_var,
                        )
            _inject_env_blocks(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Namespace):
                    env_block = getattr(item, "env", None)
                    if isinstance(env_block, Namespace):
                        for attr, env_var in env_block.items():
                            secret = os.getenv(str(env_var), "")
                            if secret:
                                object.__setattr__(item, attr, secret)
                            else:
                                _logger.warning(
                                    "Secret not found for '%s' — expected env var '%s' in .env.",
                                    attr, env_var,
                                )
                    _inject_env_blocks(item)


def _load_env_file(env_file: Path) -> None:
    """Load the .env file into os.environ if it exists."""
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)


def _load_main_config(env: str, config_dir: Path) -> dict[str, Any]:
    """Load and return the main environment config file as a raw dict."""
    return _load_yaml(_config_path(env, config_dir))


def _config_path(env: str, config_dir: Path) -> Path:
    """Return the resolved Path to the main config file for the given environment."""
    filename = _CONFIG_FILE_NAMES.get(env)
    if not filename:
        raise ConfigError(f"No config file defined for environment '{env}'.")
    path = config_dir / filename
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    return path


def _merge_config_files(base: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Load and deep-merge all non-main YAML files under config_dir into base."""
    main_filenames = set(_CONFIG_FILE_NAMES.values())
    result = dict(base)
    for config_file in sorted(config_dir.rglob("*.yaml")):
        if config_file.name in main_filenames:
            continue
        result = _deep_merge(result, _load_yaml(config_file))
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file, returning an empty dict on blank files."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override into base, returning a new dict.

    Nested dicts are merged recursively. Lists are concatenated with
    deduplication on named items (dicts with a 'name' key). All other
    values are overwritten by override.
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
            result[key] = result[key] + new_items
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Private — path resolution
# ---------------------------------------------------------------------------

def _is_path_key(key: str) -> bool:
    return key in _PATH_KEY_NAMES or any(key.endswith(s) for s in _PATH_KEY_SUFFIXES)


def _is_log_path_key(key: str) -> bool:
    return key in _LOG_PATH_KEYS


def _resolve_path_value(value: str, base: Path) -> Path:
    if value.startswith("~"):
        return Path(value).expanduser().resolve()
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def _resolve_log_path_value(value: str, base: Path) -> str:
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
                _resolve_paths(item, base, parent_key) if isinstance(item, dict) else item
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
    """Recursively log a Namespace at DEBUG level, masking secrets."""
    prefix = "  " * indent
    for key, value in ns.items():
        if isinstance(value, Namespace):
            _logger.debug("%s%s:", prefix, key)
            _print_namespace(value, indent + 1)
        elif isinstance(value, list):
            _logger.debug("%s%s: [%d item(s)]", prefix, key, len(value))
        else:
            display = "***" if any(
                s in key.lower() for s in ("password", "key", "token", "secret")
            ) else value
            _logger.debug("%s%s: %s", prefix, key, display)
