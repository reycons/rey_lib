"""
Context assembly and effective-config construction for rey_lib.

Builds ctx from an installation ``config.yaml``: include-folder resolution,
ordered deep-merge, compatibility aliases, env-reference and env-block
injection, path resolution, provenance recording, and secret injection. Split
out of ``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split); loading
order, merge precedence, token resolution, and ctx shape are unchanged.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_loader import (
    _ENV_FILE_NAME,
    _deep_merge,
    _find_parent_install_raw,
    _load_env_file,
    _load_yaml,
    _merge_compatible_collection,
    _merge_compatible_mapping,
    _yaml_files_in_folder,
)
from rey_lib.config.config_paths import (
    _SafePathFormat,
    _apply_path_resolver,
    _build_path_resolver,
    _resolve_paths,
)
from rey_lib.config.provenance import ConfigMetadata, layer_for_source
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

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
    full_installation: bool = False,
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
       has no include block (``default_behavior: full_folder``).  When
       ``full_installation`` is set the app-scoped include list and
       ``default_behavior`` are bypassed and the entire config directory is
       merged, yielding the authoritative installation-wide context.
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
        select the include list from ``config_loading.apps`` and recorded as
        ``ctx.app_name``.  Preserved even when ``full_installation`` is set.
    project_root : Path | None
        Defaults to ``Path.cwd()``.
    full_installation : bool
        When ``True`` build an explicit installation-wide context: every
        ``*.yaml`` under the config directory is deep-merged regardless of the
        app-scoped include list or ``default_behavior``.  Used by
        installation-wide consumers (console diagnostics, workflow inventory)
        that must see every app's resolved configuration.  ``app_name`` still
        records the requesting app's identity.  Defaults to ``False``.

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
        root_raw, resolver_strs, app_name, config_path, full_installation
    )

    # Provenance metadata is recorded in the same merge order as the config
    # values, so a later layer replacing a value carries the prior entry in its
    # override history. Recording is additive and never alters ``raw``.
    metadata = ConfigMetadata()
    metadata.record_tree(root_raw, source_file=str(config_path), layer="installation")

    # Steps 4–5 — walk each folder and merge files in declared order.
    raw: dict[str, Any] = root_raw
    for folder in include_folders:
        folder_files = _yaml_files_in_folder(folder, config_path)
        for yaml_file in folder_files:
            file_raw = _stamp_workflow_ownership(_load_yaml(yaml_file))
            raw = _deep_merge(raw, file_raw)
            metadata.record_tree(
                file_raw,
                source_file=str(yaml_file),
                layer=layer_for_source(yaml_file, config_dir),
            )
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

        # Record final resolved values for provenance (runtime values unchanged).
        resolver_strs = {
            name: str(resolved) for name, resolved in path_resolver._paths.items()
        }
        metadata.resolve_values(resolver_strs)
        for name, resolved in path_resolver._paths.items():
            metadata.set_resolved(f"paths.{name}", str(resolved))

    object.__setattr__(ctx, "config_path", str(config_path))
    if app_name:
        object.__setattr__(ctx, "app_name", app_name)
    object.__setattr__(ctx, "log_level", "INFO")
    object.__setattr__(ctx, "log_depth", 0)
    # Provenance is stored separately under a private attribute so it never
    # appears in ctx.keys() and never shadows a real config value.
    object.__setattr__(ctx, "_config_metadata", metadata)

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
    full_installation: bool = False,
) -> list[Path]:
    """Return the ordered include folder list for the app.

    Reads ``config_loading.apps.<app_name>.include`` from the root config.
    Expands ``{token}`` placeholders using the preliminary path resolver.
    Raises ``ConfigError`` for any declared folder that does not exist.
    Falls back to the full config directory when no app-scoped block is found.
    When ``full_installation`` is set the app-scoped block and
    ``default_behavior`` are bypassed and the entire config directory is
    returned, so every app's YAML is merged into one authoritative context.
    """
    if full_installation:
        return [config_path.parent]

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

def _stamp_workflow_ownership(file_raw: dict[str, Any]) -> dict[str, Any]:
    """Stamp each workflow with its file-root ``app`` owner before merging.

    Workflow YAML files declare the owning app once at the file root and list
    their workflows without a per-item ``app``.  The deep-merge concatenates
    ``workflows`` lists across files and collapses the scalar root ``app`` to a
    single value, which would erase per-file ownership.  Copying the root
    ``app`` onto every workflow item (list or mapping shape) before the merge
    keeps ownership on each resolved workflow, so consumers can filter by
    ``workflow.app`` without depending on which file merged last.

    Parameters
    ----------
    file_raw : dict[str, Any]
        Parsed contents of a single YAML file about to be merged.

    Returns
    -------
    dict[str, Any]
        The same mapping, with workflow items stamped in place when the file
        declares a root ``app`` and a ``workflows`` block.
    """
    # Only workflow-owning files carry a root app and a workflows block.
    app = file_raw.get("app")
    workflows = file_raw.get("workflows")
    if not isinstance(app, str) or not app:
        return file_raw

    if isinstance(workflows, list):
        items = workflows
    elif isinstance(workflows, dict):
        items = list(workflows.values())
    else:
        return file_raw

    for workflow in items:
        if isinstance(workflow, dict) and not workflow.get("app"):
            workflow["app"] = app
    return file_raw

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
