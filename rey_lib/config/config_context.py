"""
Context assembly and effective-config construction for rey_lib.

Builds ctx from an installation ``config.yaml``: include-folder resolution,
ordered deep-merge, compatibility aliases, env-reference and env-block
injection, path resolution, provenance recording, and secret injection. Split
out of ``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split); loading
order, merge precedence, token resolution, and ctx shape are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_loader import (
    _deep_merge,
    _find_parent_install_raw,
    _load_env_file,
    _load_yaml,
    _merge_compatible_mapping,
    _yaml_files_in_folder,
    declared_env_file,
)
from rey_lib.config.config_paths import (
    _SafePathFormat,
    _build_path_resolver,
)
from rey_lib.config.context_builder import ContextBuilder
from rey_lib.config.provenance import (
    ConfigMetadata,
    get_config_file_references,
    layer_for_source,
)
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger, log_config_file_reference

_logger = get_logger(__name__)

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

    # Step 3 — the environment file this installation declares, read after the
    # root config because the root config is what declares it. The declaration
    # is the whole answer: nothing is joined to it, no filename is assumed, and
    # an installation declaring none has none.
    declared_env = declared_env_file(config_path)
    if declared_env is not None:
        _load_env_file(declared_env)

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

    # Step 6 — hand the sourced state to the one builder. Assembly is not this
    # function's to do: a second place that finishes a context is how the
    # pipeline snapshot path drifted out of agreement with this one.
    return ContextBuilder(
        raw,
        config_dir=config_dir,
        config_path=config_path,
        runtime_path_tokens=runtime_path_tokens,
        metadata=metadata,
        app_name=app_name,
    ).build()

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
