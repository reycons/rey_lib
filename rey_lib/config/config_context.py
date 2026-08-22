"""
Context assembly and effective-config construction for rey_lib.

Builds ctx from an installation ``config.yaml``: include-folder resolution,
ordered deep-merge, compatibility aliases, env-reference and env-block
injection, path resolution, provenance recording, and secret injection. Split
out of ``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split); loading
order, merge precedence, token resolution, and ctx shape are unchanged.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
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
from rey_lib.config.env_reference import ENV_REFERENCE_PREFIX, declaration_map
from rey_lib.config.provenance import (
    ConfigMetadata,
    get_config_file_references,
    layer_for_source,
)
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger, log_config_file_reference

_logger = get_logger(__name__)

# Named path an installation declares to say where it begins. It locates the
# installation's .env; every other path stays resolved as before.
_INSTALLATION_ROOT_PATH = "installation_root"

# Config precedence layers map to human-facing configuration roles. Roles come
# from recorded provenance (the layer a file contributed at), never from the
# file name or extension (SGC_Rey_Config_Utils_Run_Log_Config_File_Recording).
_LAYER_ROLE = {
    "installation": "Installation",
    "workflow": "Workflow",
    "runtime": "Runtime",
}

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

    started = datetime.now().astimezone()
    date = started.strftime("%Y%m%d")
    runtime_path_tokens = {
        "date": date,
        "yyyy": date[:4],
        "mm": date[4:6],
        "dd": date[6:8],
        "yyymm": date[:6],
        "yyymmdd": date,
    }

    # Step 1 — root config only; no rglob yet.
    root_raw: dict[str, Any] = _load_yaml(config_path)
    _logger.info("config_loader root=%s app=%s", config_path, app_name or "(none)")

    # Step 2 — preliminary resolver so include path tokens can be expanded.
    prelim_resolver = _build_path_resolver(
        root_raw.get("paths", []), runtime_path_tokens
    )
    resolver_strs: dict[str, str] = dict(runtime_path_tokens)
    resolver_strs.update({
        k: str(v) for k, v in prelim_resolver._paths.items()
    })

    # Step 3 — the installation's own .env, read after the root config because
    # the root config is what says where the installation begins.
    _load_env_file(_env_directory(prelim_resolver, config_dir) / _ENV_FILE_NAME)

    # Step 4 — determine the ordered list of include folders.
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

    raw_paths = getattr(ctx, "paths", None)
    if isinstance(raw_paths, list):
        path_resolver = _build_path_resolver(raw_paths, runtime_path_tokens)
        object.__setattr__(ctx, "paths", path_resolver)
        _apply_path_resolver(ctx, path_resolver)

        # Record final resolved values for provenance (runtime values unchanged).
        resolver_strs = dict(runtime_path_tokens)
        resolver_strs.update({
            name: str(resolved) for name, resolved in path_resolver._paths.items()
        })
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
    """Apply the remaining supported compatibility aliases.

    Database and LLM aliases remain structural bridges. Pipelines have one
    canonical top-level list and reject the retired nested representation.
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
    pipeline_coordinator = result.get("pipeline_coordinator")
    if (
        isinstance(pipeline_coordinator, dict)
        and "pipelines" in pipeline_coordinator
    ):
        raise ConfigError(
            "Config section 'pipeline_coordinator.pipelines' is retired; "
            "declare the canonical top-level 'pipelines' list instead."
        )
    pipelines = result.get("pipelines")
    if pipelines is not None and not isinstance(pipelines, list):
        raise ConfigError("Config section 'pipelines' must be a canonical list.")

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

def _role_for_layer(layer: str) -> str:
    """Return the configuration role for a provenance layer, defaulting cleanly."""
    return _LAYER_ROLE.get(layer.lower(), "Configuration")


def record_config_file_references(ctx: Namespace, run_log) -> None:
    """Emit one CONFIG_FILE_REFERENCE per config file that fed the effective ctx.

    Configuration files are recorded because they contributed to the effective
    execution context — from recorded provenance, not because they were read and
    not by filename inference
    (SGC_Rey_Config_Utils_Run_Log_Config_File_Recording). Each contributing file
    is emitted once, in load order, carrying its role/layer, the sections it
    supplied, and the paths it overrode. Called at run start, once the run log
    exists; a no-op when ``ctx`` carries no provenance metadata.

    Parameters
    ----------
    ctx : Namespace
        A context built by ``build_ctx_from_path`` with a live run log.

    Returns
    -------
    None
    """
    referenced_by = str(
        getattr(ctx, "workflow_name", "")
        or getattr(ctx, "pipeline_name", "")
        or getattr(ctx, "app_name", "")
    )
    for reference in get_config_file_references(ctx):
        layer = str(reference.get("configuration_layer") or "")
        role = _role_for_layer(layer)
        path = str(reference["path"])
        log_config_file_reference(run_log,
            path,
            file_role=role,
            config_name=Path(path).name,
            config_type=layer or role,
            configuration_layer=layer,
            load_order=reference.get("load_order"),
            variables_contributed=list(reference.get("variables_contributed") or []),
            overrides=list(reference.get("overrides") or []),
            referenced_by=referenced_by,
        )


def print_ctx(ctx: Namespace) -> None:
    """Log the full context hierarchy at DEBUG level for diagnostic use."""
    _logger.debug("=== ctx dump ===")
    _print_namespace(ctx, indent=0)
    _logger.debug("=== end ctx dump ===")

# ---------------------------------------------------------------------------
# Private — loading and merging
# ---------------------------------------------------------------------------
def _env_directory(prelim_resolver: Any, config_dir: Path) -> Path:
    """Return the directory holding this installation's ``.env``.

    An installation declares where it begins by naming an
    ``installation_root`` path; the ``.env`` is read from there. Installations
    lay their configuration out differently — one keeps it at the installation
    root, another nests it under ``config/install`` — so the root is declared
    rather than derived. Counting parent directories or matching directory
    names would be a guess that happens to hold for today's layouts.

    Without that declaration the file is read from the directory holding the
    root config, which is the long-standing behaviour and stays the default.

    Parameters
    ----------
    prelim_resolver : Any
        Preliminary PathResolver built from the root config's ``paths``.
    config_dir : Path
        Directory holding the root config file.

    Returns
    -------
    Path
        Directory to read ``.env`` from. Never leaves the installation: an
        undeclared or unusable root falls back to ``config_dir`` rather than
        searching upward, so one installation can never read another's file.
    """
    declared = getattr(prelim_resolver, "_paths", {}).get(_INSTALLATION_ROOT_PATH)
    if declared is None:
        return config_dir

    root = Path(str(declared)).expanduser()
    if not root.is_dir():
        _logger.warning(
            "installation_root '%s' is not a directory; reading .env from %s.",
            root, config_dir,
        )
        return config_dir
    return root


def _assemble_ctx_data(raw: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Apply the non-file transformations needed before Namespace wrapping."""
    # Checked first, against what the author wrote: the nested form names its
    # variable directly and has nothing to declare, so validating after the
    # rewrite would demand a declaration for it.
    raw = _check_env_references(raw)
    raw = _declare_env_references(raw)
    raw = _resolve_paths(raw, config_dir, parent_key="")
    return raw

def _check_env_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Check that every ``env.<name>`` reference names a declared entry.

    The reference itself is left exactly as written. Nothing here reads the
    environment: a value backed by an environment variable is resolved by the
    subsystem that uses it, at the moment it is used, so the finalized context
    holds the reference and never the value.

    That is what makes the context safe to serialize, log or hand to a caller:
    there is nothing resolved in it to expose. It also means a variable changed
    after startup is seen by the next consumer that asks for it.

    An undeclared reference is still a configuration error, exactly as before --
    that is a mistake in the configuration and has nothing to do with whether
    the variable is set.
    """
    env_map = _build_env_reference_map(raw)
    if not env_map:
        return raw
    _assert_env_references_declared(raw, env_map, is_root=True)
    return raw

def _build_env_reference_map(raw: dict[str, Any]) -> dict[str, str]:
    """Build key_name -> env_var map from top-level env config entries.

    Through the same reader the resolver uses, so a reference that validates
    here is one that resolves later, and a declaration cannot be understood two
    ways on either side of the build.
    """
    return declaration_map(raw.get("env", []))

def _assert_env_references_declared(
    value: Any,
    env_map: dict[str, str],
    *,
    is_root: bool = False,
) -> None:
    """Walk the raw configuration and refuse a reference nobody declared."""
    if isinstance(value, dict):
        for key, child in value.items():
            # The declaration block names the references; it is not one.
            if is_root and key == "env":
                continue
            _assert_env_references_declared(child, env_map, is_root=False)
        return

    if isinstance(value, list):
        for item in value:
            _assert_env_references_declared(item, env_map, is_root=False)
        return

    if isinstance(value, str) and value.startswith(ENV_REFERENCE_PREFIX):
        name = value[len(ENV_REFERENCE_PREFIX):]
        if name not in env_map:
            raise ConfigError(
                f"Unknown env reference '{value}' — no matching key name in top-level env block."
            )


def _declare_env_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn the nested ``env:`` mapping form into ordinary symbolic references.

    Two spellings exist in configuration. A value may be written directly::

        password: env.REY_APPS_PASSWORD

    or the containing block may carry a map of target attribute to variable::

        env:
          password: REY_APPS_PASSWORD

    Both mean the same thing, so both end up as the same symbolic string in the
    finalized context. Rewriting the second form here, before the context is
    built, is what keeps them uniform -- and what stops the context being
    modified after construction.

    The two forms name their variable differently, though. The direct form names
    a *declared entry*, which the top-level ``env`` block maps to a variable::

        env:
          - name: openai_api_key
            env_var: FIXTURE_OPENAI_API_KEY

    while the nested form names the variable itself. So the nested form's
    variable is declared here as well, under its own name. That leaves one rule
    for whoever resolves these later: every ``env.<name>`` is looked up in the
    declaration block, and there is no second way a reference can be read.
    """
    declared: dict[str, str] = {}
    result = _rewrite_env_blocks(raw, declared)
    if declared:
        _add_declarations(result, declared)
    return result


def _rewrite_env_blocks(raw: Any, declared: dict[str, str]) -> Any:
    """Rewrite nested ``env:`` maps, recording the variables they name."""
    if isinstance(raw, dict):
        block = raw.get("env")
        result = {key: _rewrite_env_blocks(child, declared) for key, child in raw.items()}
        # A list under `env` is the declaration block, which names references
        # rather than assigning them.
        if isinstance(block, dict):
            for attr, env_var in block.items():
                name = str(env_var).strip()
                if name:
                    result[attr] = f"{ENV_REFERENCE_PREFIX}{name}"
                    declared[name] = name
        return result

    if isinstance(raw, list):
        return [_rewrite_env_blocks(item, declared) for item in raw]

    return raw


def _add_declarations(raw: dict[str, Any], declared: dict[str, str]) -> None:
    """Add declarations for nested-form variables that have none yet."""
    entries = raw.get("env")
    if not isinstance(entries, list):
        # No declaration block, or the root itself uses the nested form. Either
        # way there is nothing written here to preserve.
        entries = []
    known = {
        str(entry.get("name", "")).strip()
        for entry in entries
        if isinstance(entry, dict)
    }
    raw["env"] = [
        *entries,
        *(
            {"name": name, "env_var": env_var, "generate": False}
            for name, env_var in declared.items()
            if name not in known
        ),
    ]


def _print_namespace(ns: Namespace, indent: int) -> None:
    """Recursively log a Namespace at DEBUG level.

    Every field is printed as it stands. A field that names an environment
    variable prints that name -- ``password: env.REY_APPS_PASSWORD`` -- which
    is what the context holds and is safe to read.

    There is no masking here, and none is needed. Masking guessed from a field
    name was protecting resolved values that the context no longer carries, and
    a guess is the wrong shape for the job: it hid an ordinary ``key`` while a
    secret in a field it did not recognise printed in full. What makes this
    safe now is that there is nothing resolved to print.
    """
    prefix = "  " * indent
    for key, value in ns.items():
        if isinstance(value, Namespace):
            _logger.debug("%s%s:", prefix, key)
            _print_namespace(value, indent + 1)
        elif isinstance(value, list):
            _logger.debug("%s%s: [%d item(s)]", prefix, key, len(value))
        else:
            _logger.debug("%s%s: %s", prefix, key, value)
