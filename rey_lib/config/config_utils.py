"""
Generic configuration loader.

Loads the installation root config.yaml, resolves the app-scoped include
list from ``config_loading.apps``, deep-merges all include-folder YAML
files in declared order, resolves the ``paths:`` list into a PathResolver,
and returns a Namespace with attribute-style access.

Design principles
-----------------
- No application-specific knowledge — works for any project
- One config.yaml per installation owns ALL physical paths via paths: list
- Apps identify themselves; config utilities decide what to load
- Include folders are declared in the root config, not guessed by apps
- Path values are resolved automatically based on key name suffix
- Secrets are injected from environment variables via env: blocks in YAML

Loading order
-------------
  1. root config.yaml only
  2. Build preliminary PathResolver for token expansion
  3. Resolve include folder list for the app
  4. Walk each include folder (sorted rglob *.yaml) and merge in order
  5. Assemble Namespace, inject env blocks, finalise PathResolver

Runtime state (not from YAML)
------------------------------
  ctx.config_path  str   — absolute path to the root config.yaml
  ctx.app_name     str   — app name passed to build_ctx_from_path
  ctx.log_level    str   — written by log_utils.setup_logging()
  ctx.log_depth    int   — incremented/decremented by log_enter/log_exit

Public API
----------
  build_ctx_from_path(config_path, app_name)  Build context from a config.yaml.
  inject_secrets(ctx, map)                    Inject env secrets into ctx.
  print_ctx(ctx)                              Log the full context at DEBUG level.
  Namespace                                   Attribute-access wrapper around a dict.
  PathResolver                                Named-path resolver built from paths: list.
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
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
    "parse_yaml",
    "parse_yaml_namespace",
    "dump_yaml",
    "Namespace",
    "PathResolver",
]

# Config directory name — constant across all projects.
_CONFIG_DIR_NAME = "config"
_ENV_FILE_NAME   = ".env"

# Keys whose string values are filesystem paths and are resolved to Path
# objects. Explicit, reviewed membership only — path behaviour is never inferred
# from how a key is spelled (no suffix/name/regex matching, no fallback). Add a
# key here, under review, to opt it in. Notably absent: 'contract_file' (a
# logical contract identifier, not a rey_lib path) and bare 'path' (the 'paths:'
# block is resolved explicitly by _build_path_resolver).
_PATH_KEYS = frozenset({
    "app_path", "artifacts_path", "config_path", "contracts_root",
    "converted_path", "env_file", "failed_path", "inbox_path", "jsonl_path",
    "jsonl_root", "output_root", "pipeline_log_dir", "processing_path",
    "raw_output_path", "readable_root", "records_path", "rejected_path",
    "repo_root", "results_path", "script_path", "sql_path", "success_path",
    "venv_path", "working_dir",
})

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
    app_name: str | None = None,
    project_root: Path | None = None,
) -> Namespace:
    """Build context from an installation ``config.yaml`` using app-scoped includes.

    Loading order
    -------------
    1. Load the root ``config.yaml`` only.
    2. Build a preliminary ``PathResolver`` from its ``paths:`` list so that
       ``{configs}`` and other tokens can be expanded in include entries.
    3. Resolve the ordered include folder list from
       ``config_loading.apps.<app_name>.include``.  Falls back to a full
       rglob of the config directory when ``app_name`` is not provided or
       has no include block (``default_behavior: full_folder``).
    4. Walk each include folder in declared order; within each folder load
       all ``*.yaml`` files sorted by path.
    5. Merge root config then each include folder's files deterministically.
    6. Assemble the final ``Namespace``: env injection, ``PathResolver``,
       logical-path substitution.

    Parameters
    ----------
    config_path : Path
        Path to the installation root ``config.yaml``.
    app_name : str | None
        The app's own identity string (e.g. ``"rey_console"``).  Used to
        select the include list from ``config_loading.apps``.  When omitted
        the full config folder is loaded for backward compatibility.
    project_root : Path | None
        Defaults to ``Path.cwd()``.

    Returns
    -------
    Namespace
        Fully populated context with resolved ``ctx.paths``,
        ``ctx.config_path``, and ``ctx.app_name`` (when provided).

    Raises
    ------
    ConfigError
        If the root file does not exist or a declared include folder is
        missing from disk.
    """
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    config_dir = config_path.parent
    if project_root is None:
        project_root = Path.cwd()

    _load_env_file(config_dir / _ENV_FILE_NAME)

    # Step 1 — root config only; no rglob yet.
    root_raw: dict[str, Any] = _load_yaml(config_path)
    _logger.info("config_loader root=%s app=%s", config_path, app_name or "(none)")

    # Step 2 — preliminary resolver so include path tokens can be expanded.
    prelim_resolver = _build_path_resolver(root_raw.get("paths", []))
    resolver_strs: dict[str, str] = {
        k: str(v) for k, v in prelim_resolver._paths.items()
    }

    # Step 3 — determine the ordered list of include folders.
    include_folders = _resolve_include_folders(
        root_raw, resolver_strs, app_name, config_path
    )

    # Steps 4–5 — walk each folder and merge files in declared order.
    raw: dict[str, Any] = root_raw
    for folder in include_folders:
        folder_files = _yaml_files_in_folder(folder, config_path)
        for yaml_file in folder_files:
            raw = _deep_merge(raw, _load_yaml(yaml_file))
            _logger.debug("config_loader   file=%s", yaml_file)
        _logger.info("config_loader include=%s files=%d", folder, len(folder_files))

    # Backward-compat: if paths list is still missing, search parent directories.
    if not isinstance(raw.get("paths"), list):
        parent_raw = _find_parent_install_raw(config_path)
        if parent_raw:
            parent_paths = parent_raw.get("paths")
            parent_rest  = {k: v for k, v in parent_raw.items() if k != "paths"}
            raw = _deep_merge(parent_rest, raw)
            if isinstance(parent_paths, list):
                raw["paths"] = parent_paths

    # Step 6 — assemble and wrap.
    raw = _apply_compatibility_aliases(raw)
    raw = _assemble_ctx_data(raw, config_dir)
    ctx = Namespace(raw)
    _inject_env_blocks(ctx)

    raw_paths = getattr(ctx, "paths", None)
    if isinstance(raw_paths, list):
        path_resolver = _build_path_resolver(raw_paths)
        object.__setattr__(ctx, "paths", path_resolver)
        _apply_path_resolver(ctx, path_resolver)

    object.__setattr__(ctx, "config_path", str(config_path))
    if app_name:
        object.__setattr__(ctx, "app_name", app_name)
    object.__setattr__(ctx, "log_level", "INFO")
    object.__setattr__(ctx, "log_depth", 0)

    _logger.info(
        "config_loader complete top_level_keys=%s",
        [k for k in ctx.keys() if not k.startswith("_")],
    )
    return ctx


def _resolve_include_folders(
    root_raw: dict[str, Any],
    resolver_strs: dict[str, str],
    app_name: str | None,
    config_path: Path,
) -> list[Path]:
    """Return the ordered include folder list for the app.

    Reads ``config_loading.apps.<app_name>.include`` from the root config.
    Expands ``{token}`` placeholders using the preliminary path resolver.
    Raises ``ConfigError`` for any declared folder that does not exist.
    Falls back to the full config directory when no app-scoped block is found.
    """
    loading_cfg = root_raw.get("config_loading")
    if not isinstance(loading_cfg, dict):
        loading_cfg = {}

    apps_cfg = loading_cfg.get("apps")
    if not isinstance(apps_cfg, dict):
        apps_cfg = {}

    if app_name and app_name in apps_cfg:
        app_cfg = apps_cfg.get(app_name)
        include_list = app_cfg.get("include") or [] if isinstance(app_cfg, dict) else []
        folders: list[Path] = []
        for entry in include_list:
            expanded = str(entry).format_map(_SafePathFormat(resolver_strs))
            folder = Path(expanded).expanduser().resolve()
            if not folder.exists():
                raise ConfigError(
                    f"Config include path does not exist for app "
                    f"{app_name}: {folder}"
                )
            folders.append(folder)
        return folders

    # Default: rglob the config directory (backward-compatible behaviour).
    default = loading_cfg.get("default_behavior", "full_folder")
    if default == "full_folder":
        return [config_path.parent]
    return []


def _yaml_files_in_folder(folder: Path, exclude: Path) -> list[Path]:
    """Return sorted YAML files for a folder or one YAML file path.

    ``folder`` may be either:
    - a directory, in which case all ``*.yaml`` files are returned recursively
    - a single ``.yaml`` file, in which case only that file is returned

    The root ``config.yaml`` is excluded so it is never merged twice.
    """
    if folder.is_file():
        if folder.suffix.lower() == ".yaml" and folder.resolve() != exclude:
            return [folder]
        return []

    return [
        f for f in sorted(folder.rglob("*.yaml"))
        if f.resolve() != exclude
    ]


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


def _apply_compatibility_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """Expose current and canonical config sections without removing either.

    This is a structural bridge for the logical config reorganization. It keeps
    legacy/current keys working while allowing future YAML to use canonical
    names. Duplicate logical objects fail closed when definitions conflict.
    """
    result = deepcopy(raw)

    _alias_named_collection(
        result,
        current_key="db_connections",
        canonical_key="connections",
    )
    _alias_named_collection(
        result,
        current_key="llm_profiles",
        canonical_key="llm",
    )
    _alias_nested_mapping(
        result,
        nested_parent="pipeline_coordinator",
        nested_key="pipelines",
        canonical_key="pipelines",
    )

    return result


def _alias_named_collection(
    raw: dict[str, Any],
    *,
    current_key: str,
    canonical_key: str,
) -> None:
    current_exists = current_key in raw
    canonical_exists = canonical_key in raw

    if current_exists and canonical_exists:
        merged = _merge_compatible_collection(
            raw[current_key],
            raw[canonical_key],
            label=canonical_key,
        )
        raw[current_key] = deepcopy(merged)
        raw[canonical_key] = deepcopy(merged)
    elif current_exists:
        raw[canonical_key] = deepcopy(raw[current_key])
    elif canonical_exists:
        raw[current_key] = deepcopy(raw[canonical_key])


def _alias_nested_mapping(
    raw: dict[str, Any],
    *,
    nested_parent: str,
    nested_key: str,
    canonical_key: str,
) -> None:
    parent = raw.get(nested_parent)
    if parent is not None and not isinstance(parent, dict):
        raise ConfigError(
            f"Config section '{nested_parent}' must be a mapping to alias "
            f"'{nested_parent}.{nested_key}'."
        )

    nested_exists = isinstance(parent, dict) and nested_key in parent
    canonical_exists = canonical_key in raw

    if nested_exists and canonical_exists:
        merged = _merge_compatible_mapping(
            parent[nested_key],
            raw[canonical_key],
            label=canonical_key,
        )
        parent[nested_key] = deepcopy(merged)
        raw[canonical_key] = deepcopy(merged)
    elif nested_exists:
        raw[canonical_key] = deepcopy(parent[nested_key])
    elif canonical_exists:
        if parent is None:
            parent = {}
            raw[nested_parent] = parent
        parent[nested_key] = deepcopy(raw[canonical_key])


def _merge_compatible_collection(left: Any, right: Any, *, label: str) -> Any:
    if isinstance(left, list) and isinstance(right, list):
        return _merge_named_lists(left, right, label=label)
    if isinstance(left, dict) and isinstance(right, dict):
        return _merge_compatible_mapping(left, right, label=label)
    if left == right:
        return deepcopy(left)
    raise ConfigError(
        f"Conflicting compatibility aliases for '{label}': "
        f"values must use the same shape or match exactly."
    )


def _merge_named_lists(left: list[Any], right: list[Any], *, label: str) -> list[Any]:
    merged = deepcopy(left)
    name_to_index: dict[str, int] = {
        str(item["name"]): idx
        for idx, item in enumerate(merged)
        if isinstance(item, dict) and "name" in item
    }

    for item in right:
        if not isinstance(item, dict) or "name" not in item:
            if item not in merged:
                merged.append(deepcopy(item))
            continue

        name = str(item["name"])
        if name not in name_to_index:
            name_to_index[name] = len(merged)
            merged.append(deepcopy(item))
            continue

        existing = merged[name_to_index[name]]
        if existing != item:
            raise ConfigError(
                f"Conflicting duplicate '{label}' entry named '{name}'."
            )

    return merged


def _merge_compatible_mapping(left: Any, right: Any, *, label: str) -> Any:
    if not isinstance(left, dict) or not isinstance(right, dict):
        if left == right:
            return deepcopy(left)
        raise ConfigError(
            f"Conflicting compatibility aliases for '{label}': "
            "both values must be mappings."
        )

    merged = deepcopy(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = deepcopy(value)
            continue
        if merged[key] != value:
            raise ConfigError(
                f"Conflicting duplicate '{label}' entry named '{key}'."
            )
    return merged


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


def parse_yaml(text: str) -> Any:
    """Parse YAML text — the sanctioned YAML parser.

    Application code must not import ``yaml`` directly. Read the file with
    rey_lib.files (``read_text_file``) and parse the resulting text here.
    Returns the parsed value (usually a dict) and ``{}`` for blank text;
    callers that require a mapping should validate the result themselves.

    Parameters
    ----------
    text : str
        YAML document text (e.g. from ``read_text_file`` or markdown frontmatter).

    Returns
    -------
    Any
        The parsed value (``{}`` when the text is blank).

    Raises
    ------
    ConfigError
        When the text is not valid YAML.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML text: {exc}") from exc
    return {} if data is None else data


def parse_yaml_namespace(text: str) -> "Namespace":
    """Parse YAML text into an attribute-access :class:`Namespace` context.

    Files are read with rey_lib.files; the context is built here. Non-mapping
    documents yield an empty context.

    Parameters
    ----------
    text : str
        YAML document text.

    Returns
    -------
    Namespace
        Attribute-access view over the parsed mapping.
    """
    data = parse_yaml(text)
    return Namespace(data if isinstance(data, dict) else {})


def dump_yaml(data: Any, *, sort_keys: bool = False) -> str:
    """Serialize a Python object to YAML text — the sanctioned YAML writer.

    Application code must not import ``yaml`` directly. Serialize here, then
    write the text with rey_lib.files. Uses block style and preserves key order
    and unicode by default.

    Parameters
    ----------
    data : Any
        Object to serialize (typically a dict).
    sort_keys : bool
        Whether to sort mapping keys. Default False (preserve insertion order).

    Returns
    -------
    str
        YAML document text.
    """
    return yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=sort_keys,
        allow_unicode=True,
    )


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
            result[key] = _merge_named_lists(result[key], value, label=key)
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
    """Return True only for keys explicitly declared as filesystem paths."""
    return key in _PATH_KEYS


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
