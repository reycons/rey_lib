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
  build_config_sources_yaml(...)  Return source-annotated YAML loaded by build_ctx().
  inject_secrets(ctx, map)        Inject .env secrets into ctx at dot-separated paths.
  save_config(data, env, ...)     Write a dict back to the main config file.
  print_ctx(ctx)                  Log the full context at DEBUG level.
  Namespace                       Attribute-access wrapper around a config dict.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

from rey_lib.errors.error_utils import ConfigError, validate_env
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

__all__ = [
    "build_app_ctx",
    "build_config_sources_yaml",
    "build_config_sources_yaml_from_path",
    "build_ctx",
    "build_ctx_from_path",
    "inject_secrets",
    "save_config",
    "print_ctx",
    "validate_yaml_file",
    "validate_yaml_folder",
    "Namespace",
]

# Config directory name and main config filename pattern — constant across all projects.
_CONFIG_DIR_NAME   = "config"
_CONFIG_FILE_NAMES = {"dev": "config.dev.yaml", "test": "config.test.yaml", "prod": "config.prod.yaml"}
_ENV_FILE_NAME     = ".env"

# Keys whose string values are resolved to Path objects.
_PATH_KEY_SUFFIXES = ("_path", "_dir", "_file")
_PATH_KEY_NAMES    = ("path",)

# Keys whose string values are log path templates — placeholders must survive resolution.
_LOG_PATH_KEYS = ("log_path",)

# Matches ${VAR_NAME} and $VAR_NAME patterns in YAML string values.
_ENV_VAR_PATTERN = re.compile(
    r"""
    \$\{([A-Z0-9_]+)\} |
    \$([A-Z0-9_]+)
    """,
    re.VERBOSE,
)


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

    raw = _read_config_yaml(env, config_dir)
    raw = _assemble_ctx_data(raw, config_dir)

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


def build_ctx_from_path(
    config_path: Path,
    project_root: Path | None = None,
) -> Namespace:
    """Build context directly from a config file path.

    Derives ``env`` and ``config_dir`` from ``config_path`` so the caller
    does not need to pass ``--env`` separately.  Relative paths inside the
    config file resolve relative to the config file's parent directory.

    Parameters
    ----------
    config_path : Path
        Path to a config file following the naming pattern
        ``config.<env>.yaml`` (e.g. ``config.dev.yaml``).
    project_root : Path | None
        Root directory of the calling project. Defaults to ``Path.cwd()``.

    Returns
    -------
    Namespace
        Fully populated context object.

    Raises
    ------
    ConfigError
        If the file does not exist or the filename does not match
        ``config.<env>.yaml``.
    """
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    env = _env_from_config_path(config_path)

    return build_ctx(env=env, project_root=project_root, config_dir=config_path.parent)


def build_config_sources_yaml(
    env: str = "dev",
    project_root: Path | None = None,
    config_dir: Path | None = None,
) -> str:
    """Return source-annotated YAML files loaded for a config context.

    This follows the same config-file selection as ``build_ctx()``: the
    environment-specific main config first, then every non-main ``*.yaml``
    file under ``config_dir`` in sorted order. The function intentionally
    returns text, not a context object, so logs and UIs can show exactly which
    YAML sources were available to the loader without reimplementing config
    assembly in each app.
    """
    if project_root is None:
        project_root = Path.cwd()

    if config_dir is None:
        env_config_dir = os.getenv("APP_CONFIG_DIR")
        config_dir = Path(env_config_dir) if env_config_dir else Path(project_root) / _CONFIG_DIR_NAME

    config_dir = Path(config_dir).expanduser().resolve()
    env = validate_env(env)
    sources = _read_config_yaml_sources(env, config_dir)

    docs: list[str] = []
    for path, _data in sources:
        docs.append(_source_yaml_document(config_dir, path))

    return "\n".join(docs).rstrip() + "\n"


def build_config_sources_yaml_from_path(
    config_path: Path,
    project_root: Path | None = None,
) -> str:
    """Return source-annotated YAML files loaded for ``config_path``.

    ``config_path`` follows the same ``config.<env>.yaml`` convention as
    ``build_ctx_from_path()``.
    """
    config_path = Path(config_path).expanduser().resolve()
    env = _env_from_config_path(config_path)
    return build_config_sources_yaml(
        env=env,
        project_root=project_root,
        config_dir=config_path.parent,
    )


def build_app_ctx(
    project_root: Path,
    env: str,
    log_level: Optional[str] = None,
) -> Namespace:
    """
    Build application context from YAML config and .env.

    Convenience wrapper around build_ctx() that optionally overrides the
    log level after loading.  Suitable as the standard entry point for any
    project's CLI startup.

    Parameters
    ----------
    project_root : Path
        Absolute path to the project root directory.
    env : str
        Runtime environment — ``'dev'`` or ``'prod'``.
    log_level : str, optional
        When provided, overrides the log level value from config with this
        value (e.g. ``'DEBUG'``, ``'INFO'``).

    Returns
    -------
    Namespace
        Fully populated context object.
    """
    ctx = build_ctx(env=env, project_root=project_root)
    if log_level is not None:
        # Override whatever the config declared so the caller's --log-level flag wins.
        object.__setattr__(ctx, "log_level", log_level.upper())
    return ctx


def print_ctx(ctx: Namespace) -> None:
    """Log the full context hierarchy at DEBUG level for diagnostic use."""
    _logger.debug("=== ctx dump ===")
    _print_namespace(ctx, indent=0)
    _logger.debug("=== end ctx dump ===")


def validate_yaml_file(path: str | Path) -> dict:
    """Validate a single YAML file for parse errors and unresolved env vars.

    Parameters
    ----------
    path : str | Path
        Path to the YAML file to validate.

    Returns
    -------
    dict
        Result with keys: status, checked_files, errors, warnings,
        env_refs, unresolved_env_refs.
        status is 'valid', 'warning', or 'invalid'.
    """
    path = Path(path)
    result = _new_validation_result()
    result["checked_files"].append(str(path))

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        _add_validation_issue(result, severity="error", file_path=path, message=f"Unable to read file: {exc}")
        return result

    if not text.strip():
        _add_validation_issue(result, severity="warning", file_path=path, message="YAML file is empty.")
        return result

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = column = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line   = exc.problem_mark.line + 1
            column = exc.problem_mark.column + 1
        _add_validation_issue(result, severity="error", file_path=path, message=str(exc), line=line, column=column)
        return result

    env_refs = sorted(_find_yaml_env_refs(data))
    result["env_refs"] = env_refs

    for env_name in env_refs:
        if os.environ.get(env_name) is None:
            result["unresolved_env_refs"].append(env_name)
            _add_validation_issue(
                result,
                severity="warning",
                file_path=path,
                message=f"Unresolved environment variable: {env_name}",
            )

    return result


def validate_yaml_folder(path: str | Path) -> dict:
    """Validate all YAML files in a directory tree.

    Parameters
    ----------
    path : str | Path
        Directory to search recursively for *.yaml and *.yml files.

    Returns
    -------
    dict
        Aggregated result across all files. Same structure as
        validate_yaml_file.
    """
    path = Path(path)
    result = _new_validation_result()

    yaml_files = sorted(list(path.rglob("*.yaml")) + list(path.rglob("*.yml")))
    for yaml_file in yaml_files:
        file_result = validate_yaml_file(yaml_file)
        result["checked_files"].extend(file_result["checked_files"])
        result["errors"].extend(file_result["errors"])
        result["warnings"].extend(file_result["warnings"])
        result["env_refs"].extend(file_result["env_refs"])
        result["unresolved_env_refs"].extend(file_result["unresolved_env_refs"])

    result["env_refs"]            = sorted(set(result["env_refs"]))
    result["unresolved_env_refs"] = sorted(set(result["unresolved_env_refs"]))

    if result["errors"]:
        result["status"] = "invalid"
    elif result["warnings"]:
        result["status"] = "warning"

    return result


# ---------------------------------------------------------------------------
# Private — YAML validation helpers
# ---------------------------------------------------------------------------

def _new_validation_result() -> dict:
    return {
        "status": "valid",
        "checked_files": [],
        "errors": [],
        "warnings": [],
        "env_refs": [],
        "unresolved_env_refs": [],
    }


def _add_validation_issue(
    result: dict,
    *,
    severity: str,
    file_path: str | Path,
    message: str,
    line: int | None = None,
    column: int | None = None,
) -> None:
    issue = {
        "severity": severity,
        "file_path": str(file_path),
        "message": message,
        "line": line,
        "column": column,
    }
    if severity == "error":
        result["errors"].append(issue)
        result["status"] = "invalid"
    else:
        result["warnings"].append(issue)
        if result["status"] != "invalid":
            result["status"] = "warning"


def _find_yaml_env_refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found.update(_find_yaml_env_refs(v))
    elif isinstance(value, list):
        for v in value:
            found.update(_find_yaml_env_refs(v))
    elif isinstance(value, str):
        for match in _ENV_VAR_PATTERN.findall(value):
            env_name = match[0] or match[1]
            if env_name:
                found.add(env_name)
    return found


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


def _config_path(env: str, config_dir: Path) -> Path:
    """Return the resolved Path to the main config file for the given environment."""
    filename = _CONFIG_FILE_NAMES.get(env)
    if not filename:
        raise ConfigError(f"No config file defined for environment '{env}'.")
    path = config_dir / filename
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    return path


def _read_config_yaml(env: str, config_dir: Path) -> dict[str, Any]:
    """Read all YAML files for a context and return the merged raw dict."""
    sources = _read_config_yaml_sources(env, config_dir)
    result: dict[str, Any] = {}
    for _path, data in sources:
        result = _deep_merge(result, data)
    return result


def _assemble_ctx_data(raw: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Apply the non-file transformations needed before Namespace wrapping."""
    raw = _resolve_env_references(raw)
    raw = _resolve_paths(raw, config_dir, parent_key="")
    return raw


def _read_config_yaml_sources(env: str, config_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Read the exact YAML source files used by ``build_ctx()``."""
    return [
        (config_file, _load_yaml(config_file))
        for config_file in _config_source_files(env, config_dir)
    ]


def _config_source_files(env: str, config_dir: Path) -> list[Path]:
    """Return YAML files in the same order ``build_ctx()`` loads them."""
    return [_config_path(env, config_dir), *_additional_config_files(config_dir)]


def _additional_config_files(config_dir: Path) -> list[Path]:
    """Return all non-main YAML files under ``config_dir`` in load order."""
    main_filenames = set(_CONFIG_FILE_NAMES.values())
    return [
        config_file
        for config_file in sorted(config_dir.rglob("*.yaml"))
        if config_file.name not in main_filenames
    ]


def _source_yaml_document(config_dir: Path, path: Path) -> str:
    """Return one source-annotated YAML document."""
    relative = path.relative_to(config_dir).as_posix()
    text = path.read_text(encoding="utf-8").rstrip()
    return (
        "---\n"
        "# =============================================================================\n"
        f"# SOURCE: {relative}\n"
        "# =============================================================================\n"
        f"{text}\n"
    )


def _env_from_config_path(config_path: Path) -> str:
    """Derive and validate env from a ``config.<env>.yaml`` path."""
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    stem = config_path.stem
    parts = stem.split(".", 1)
    if len(parts) != 2 or parts[0] != "config":
        raise ConfigError(
            f"Config filename must follow 'config.<env>.yaml', got: {config_path.name}"
        )
    return validate_env(parts[1])


def _resolve_env_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve values like 'env.key_name' using top-level env declarations.

    Expected top-level format in main config:

        env:
          - name: account_encryption_key
            env_var: ACCOUNT_ENCRYPTION_KEY
            generate: true

    Any string value exactly matching `env.<name>` is replaced with the
    corresponding environment variable value.
    """
    env_map = _build_env_reference_map(raw)
    if not env_map:
        return raw

    env_values: dict[str, str] = {}
    for key_name, env_var in env_map.items():
        value = os.getenv(env_var, "")
        if not value:
            _logger.warning(
                "Secret not found for '%s' — expected env var '%s' in .env.",
                key_name,
                env_var,
            )
        env_values[key_name] = value

    return _replace_env_refs(raw, env_values, is_root=True)


def _build_env_reference_map(raw: dict[str, Any]) -> dict[str, str]:
    """Build key_name -> env_var map from top-level env config entries."""
    entries = raw.get("env", [])
    if not isinstance(entries, list):
        return {}

    env_map: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key_name = str(entry.get("name", "")).strip()
        env_var = str(entry.get("env_var", "")).strip()
        if key_name and env_var:
            env_map[key_name] = env_var
    return env_map


def _replace_env_refs(
    value: Any,
    env_values: dict[str, str],
    *,
    is_root: bool = False,
) -> Any:
    """Recursively replace strings matching env.<key_name> with secret values."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            # Preserve the top-level env declaration block as-is.
            if is_root and key == "env":
                result[key] = child
            else:
                result[key] = _replace_env_refs(child, env_values, is_root=False)
        return result

    if isinstance(value, list):
        return [_replace_env_refs(item, env_values, is_root=False) for item in value]

    if isinstance(value, str) and value.startswith("env."):
        key_name = value[4:]
        if key_name not in env_values:
            raise ConfigError(
                f"Unknown env reference '{value}' — no matching key name in top-level env block."
            )
        return env_values[key_name]

    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    """Validate then read a YAML file, returning an empty dict on blank files."""
    result = validate_yaml_file(path)
    if result["status"] == "invalid":
        messages = "; ".join(e["message"] for e in result["errors"])
        raise ConfigError(f"Invalid YAML file '{path}': {messages}")
    for w in result["warnings"]:
        _logger.warning("YAML validation warning in '%s': %s", path, w["message"])
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
        elif isinstance(value, str) and _is_path_key(key) and not value.startswith("ctx."):
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
