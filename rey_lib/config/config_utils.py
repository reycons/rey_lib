"""
Generic configuration loader.

Reads a config.yaml (or app.yaml) and all YAML files in the same directory,
deep-merges them into a single hierarchy, resolves the paths: list into a
PathResolver, and returns a Namespace with attribute-style access.

Design principles
-----------------
- No application-specific knowledge — works for any project
- One config.yaml per installation owns ALL physical paths via paths: list
- Apps use app.yaml with {logicalname} references resolved by PathResolver
- Path values are resolved automatically based on key name suffix
- Secrets are injected from environment variables via env: blocks in YAML

Runtime state (not from YAML)
------------------------------
  ctx.log_level    str   — written by log_utils.setup_logging()
  ctx.log_depth    int   — incremented/decremented by log_enter/log_exit

Public API
----------
  build_ctx_from_path(config_path)  Build context from a config.yaml or app.yaml.
  inject_secrets(ctx, map)          Inject env secrets into ctx at dot-separated paths.
  print_ctx(ctx)                    Log the full context at DEBUG level.
  Namespace                         Attribute-access wrapper around a config dict.
  PathResolver                      Named-path resolver built from paths: list.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

__all__ = [
    "build_ctx_from_path",
    "inject_secrets",
    "print_ctx",
    "validate_yaml_file",
    "validate_yaml_folder",
    "Namespace",
    "PathResolver",
]

# Config directory name — constant across all projects.
_CONFIG_DIR_NAME = "config"
_ENV_FILE_NAME   = ".env"

# Keys whose string values are resolved to Path objects.
_PATH_KEY_SUFFIXES = ("_path", "_dir", "_file", "_root")
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
# PathResolver
# ---------------------------------------------------------------------------

class PathResolver:
    """Resolved named paths from the installation config ``paths:`` list.

    Attached to ``ctx.paths`` after loading a ``config.yaml``.
    Call ``ctx.paths.resolve("name")`` to get the physical ``Path``.
    """

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = dict(paths)

    def resolve(self, name: str) -> Path:
        """Return the resolved ``Path`` for the given logical name."""
        if name not in self._paths:
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._paths[name]

    def __repr__(self) -> str:
        return f"PathResolver({list(self._paths)})"


def _build_path_resolver(raw_paths: Any) -> PathResolver:
    """Process a ``paths:`` list into a ``PathResolver``.

    Each entry must have ``name`` and ``path`` keys.  Values may reference
    earlier-resolved names with ``{name}`` placeholders.
    """
    resolved_strs: dict[str, str] = {}
    resolved_paths: dict[str, Path] = {}

    entries: list[Any] = raw_paths if isinstance(raw_paths, list) else []
    for entry in entries:
        if isinstance(entry, Namespace):
            name = str(getattr(entry, "name", "") or "")
            template = str(getattr(entry, "path", "") or "")
        elif isinstance(entry, dict):
            name = str(entry.get("name", "") or "")
            template = str(entry.get("path", "") or "")
        else:
            continue
        if not name or not template:
            continue
        path_str = template.format_map(_SafePathFormat(resolved_strs))
        path = Path(path_str).expanduser().resolve()
        resolved_strs[name] = str(path)
        resolved_paths[name] = path

    return PathResolver(resolved_paths)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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


def build_ctx_from_path(
    config_path: Path,
    project_root: Path | None = None,
) -> Namespace:
    """Build context from a ``config.yaml`` file.

    Loads the config, deep-merges all additional YAML files in the same
    directory, resolves the ``paths:`` named-path list into ``ctx.paths``,
    and returns a fully populated Namespace.

    If the specified file does not contain a ``paths:`` list, parent directories
    are searched for a ``config.yaml`` that does, and its ``paths:`` is merged in
    so the PathResolver is always available.

    Parameters
    ----------
    config_path : Path
        Path to the ``config.yaml`` file (top-level install config or app-specific).
    project_root : Path | None
        Defaults to ``Path.cwd()``.

    Returns
    -------
    Namespace
        Fully populated context object with resolved ``ctx.paths``.

    Raises
    ------
    ConfigError
        If the file does not exist.
    """
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    config_dir = config_path.parent
    if project_root is None:
        project_root = Path.cwd()

    _load_env_file(config_dir / _ENV_FILE_NAME)

    raw: dict[str, Any] = _load_yaml(config_path)
    for extra in sorted(config_dir.rglob("*.yaml")):
        if extra == config_path:
            continue
        raw = _deep_merge(raw, _load_yaml(extra))

    if not isinstance(raw.get("paths"), list):
        parent_raw = _find_parent_install_raw(config_path)
        if parent_raw:
            # Inject the parent's paths list directly so the app-level paths dict
            # (if any) cannot shadow it during the merge.
            parent_paths = parent_raw.get("paths")
            parent_rest  = {k: v for k, v in parent_raw.items() if k != "paths"}
            raw = _deep_merge(parent_rest, raw)
            if isinstance(parent_paths, list):
                raw["paths"] = parent_paths

    raw = _assemble_ctx_data(raw, config_dir)

    ctx = Namespace(raw)
    _inject_env_blocks(ctx)

    raw_paths = getattr(ctx, "paths", None)
    if isinstance(raw_paths, list):
        path_resolver = _build_path_resolver(raw_paths)
        object.__setattr__(ctx, "paths", path_resolver)
        _apply_path_resolver(ctx, path_resolver)

    object.__setattr__(ctx, "log_level", "INFO")
    object.__setattr__(ctx, "log_depth", 0)

    return ctx


def _find_parent_install_raw(config_path: Path) -> dict[str, Any] | None:
    """Walk parent directories to find a ``config.yaml`` with a ``paths:`` list."""
    current = config_path.parent.parent
    while True:
        candidate = current / "config.yaml"
        if candidate.exists() and candidate != config_path:
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "paths" in data:
                    return data
            except Exception:
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


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
        _add_validation_issue(
            result,
            severity="error",
            file_path=path,
            message=f"Unable to read file: {exc}",
        )
        return result

    if not text.strip():
        _add_validation_issue(
            result,
            severity="warning",
            file_path=path,
            message="YAML file is empty.",
        )
        return result

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = column = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line   = exc.problem_mark.line + 1
            column = exc.problem_mark.column + 1
        _add_validation_issue(
            result,
            severity="error",
            file_path=path,
            message=str(exc),
            line=line,
            column=column,
        )
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


def _assemble_ctx_data(raw: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Apply the non-file transformations needed before Namespace wrapping."""
    raw = _resolve_env_references(raw)
    raw = _resolve_paths(raw, config_dir, parent_key="")
    return raw


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
        elif key == "paths" and isinstance(result.get(key), list) and not isinstance(value, list):
            # PathResolver list must never be replaced by an app-level paths dict.
            pass
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
    if "{" in value:
        return Path(value)  # logical path template — substituted after PathResolver is built
    if value.startswith("~"):
        return Path(value).expanduser().resolve()
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def _resolve_log_path_value(value: str, base: Path) -> str:
    if "{" in value:
        return value  # logical path template — substituted after PathResolver is built
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


class _SafePathFormat(dict):  # type: ignore[type-arg]
    """dict subclass that returns the key in braces for missing keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def _apply_path_resolver(obj: Any, resolver: PathResolver) -> None:
    """Walk a Namespace tree and substitute {logicalname} references using *resolver*.

    Only names present in the PathResolver are substituted; unknown placeholders
    like {operation} and {timestamp} survive unchanged.
    """
    resolver_strs: dict[str, str] = {
        name: str(resolver.resolve(name)) for name in resolver._paths
    }
    _walk_logical_refs(obj, resolver_strs)


def _walk_logical_refs(obj: Any, resolver_strs: dict[str, str]) -> None:
    """Recursive worker for :func:`_apply_path_resolver`."""
    if isinstance(obj, Namespace):
        for key in obj.keys():
            value = object.__getattribute__(obj, key)
            raw = str(value) if isinstance(value, Path) else value
            if isinstance(raw, str) and "{" in raw:
                substituted = raw.format_map(_SafePathFormat(resolver_strs))
                if _is_path_key(key):
                    object.__setattr__(obj, key, Path(substituted).expanduser())
                else:
                    object.__setattr__(obj, key, substituted)
            elif isinstance(value, (Namespace, list)):
                _walk_logical_refs(value, resolver_strs)
    elif isinstance(obj, list):
        for item in obj:
            _walk_logical_refs(item, resolver_strs)


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
