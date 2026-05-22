"""Creates the standard Rey Apps installation skeleton.

Contracts:
    rey_apps_installation_folder_contract.md  — config root app layout
    installer_bootstrap_contract.md           — environment and installation layout

Rules:
- Create missing folders and placeholder files
- Never delete existing folders
- Never overwrite existing config files unless force=True
- Create .gitkeep in empty leaf folders
- Report created vs already-existed
- Return non-zero on failure (via result.success)

Filesystem levels:
    Level 1 — root:         ~/rey_apps/
    Level 2 — environment:  ~/rey_apps/<environment>/
    Level 3 — installation: ~/rey_apps/<environment>/installations/<name>/

Environment layout:
    <env>/
        apps/
        installations/
        logs/installer/
        python/
        etc/

Installation layout:
    <env>/installations/<name>/
        configs/<config_version>/       app config layout (folder contract)
        llm_contracts/<contract_version>/
        data/inbox|processing|converted|loaded|rejected|archive|temp/
        logs/
        runtime/
        .env
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from rey_lib.logs import get_logger

_logger = get_logger(__name__)

__all__ = [
    "FolderMakerResult",
    "scaffold_config_root",
    "scaffold_environment",
    "scaffold_installation",
]

_DEFAULT_CONFIG_VERSION   = "v01"
_DEFAULT_CONTRACT_VERSION = "v01"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class FolderMakerResult:
    """Tracks what the folder maker created, what already existed, and any errors."""

    created: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    errors:  list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when no errors were recorded."""
        return not self.errors

    def merge(self, other: "FolderMakerResult") -> None:
        """Merge another result into this one in-place."""
        self.created.extend(other.created)
        self.existed.extend(other.existed)
        self.errors.extend(other.errors)


# ---------------------------------------------------------------------------
# Skeleton definitions
# ---------------------------------------------------------------------------

# Per-installation data subfolders (bootstrap contract, data rule).
_DATA_SUBFOLDERS = [
    "inbox", "processing", "converted", "loaded", "rejected", "archive", "temp",
]

# Lifecycle folders written directly under each config root (folder contract).
_LIFECYCLE_FOLDERS = ["_draft", "_approved", "_archive"]

# App folder definitions: subfolders and extra placeholder files per app.
_APP_DEFS: dict[str, dict] = {
    "ftp_sync": {
        "subfolders": ["data_feeds"],
        "extra_files": [],
    },
    "rey_loader": {
        "subfolders": ["data_sources", "sql_configs", "diagnostics"],
        "extra_files": [],
    },
    "rey_analyzer": {
        "subfolders": ["analysis_configs", "data_sources", "llm_configs", "contracts", "schemas"],
        "extra_files": [],
    },
    "pipeline_coordinator": {
        "subfolders": ["pipelines"],
        "extra_files": ["app_registry.yaml"],
    },
    "rey_console": {
        "subfolders": [],
        "extra_files": [],
    },
    "file_redactor": {
        "subfolders": ["redact"],
        "extra_files": [],
    },
}

# Template written to a missing .env file — never overwrites an existing one by default.
_ENV_TEMPLATE = (
    "# .env — {installation_name}\n"
    "# Fill in secrets before running applications.\n"
    "# Do not commit this file to version control.\n"
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _ensure_dir(path: Path, result: FolderMakerResult) -> None:
    """Create *path* if it does not exist; record the outcome in *result*."""
    if path.exists():
        result.existed.append(str(path))
    else:
        try:
            path.mkdir(parents=True, exist_ok=True)
            result.created.append(str(path))
            _logger.debug("created dir: %s", path)
        except OSError as exc:
            result.errors.append(f"mkdir failed: {path} — {exc}")


def _ensure_gitkeep(path: Path, result: FolderMakerResult) -> None:
    """Touch a .gitkeep inside *path* so empty folders survive git."""
    gk = path / ".gitkeep"
    if not gk.exists():
        try:
            gk.touch()
            result.created.append(str(gk))
        except OSError as exc:
            result.errors.append(f"touch failed: {gk} — {exc}")


def _ensure_yaml(
    path: Path,
    content: dict,
    result: FolderMakerResult,
    force: bool = False,
) -> None:
    """Write *content* as YAML to *path* unless the file already exists and *force* is False."""
    if path.exists() and not force:
        result.existed.append(str(path))
        return
    try:
        with path.open("w") as fh:
            yaml.dump(content, fh, default_flow_style=False, sort_keys=False)
        result.created.append(str(path))
        _logger.debug("created file: %s", path)
    except OSError as exc:
        result.errors.append(f"write failed: {path} — {exc}")


def _ensure_env_template(
    path: Path,
    installation_name: str,
    result: FolderMakerResult,
    force: bool = False,
) -> None:
    """Write a minimal .env template to *path* unless the file already exists."""
    if path.exists() and not force:
        result.existed.append(str(path))
        return
    try:
        path.write_text(_ENV_TEMPLATE.format(installation_name=installation_name))
        result.created.append(str(path))
        _logger.debug("created file: %s", path)
    except OSError as exc:
        result.errors.append(f"write failed: {path} — {exc}")


def _app_yaml(app_name: str) -> dict:
    """Return a minimal app.yaml placeholder for *app_name*."""
    return {"name": app_name, "enabled": True, "version": "v01"}


def _diagnostics_default_yaml() -> dict:
    """Return a minimal diagnostics/default.yaml placeholder."""
    return {
        "diagnostics": {
            "info":    {"enabled": True,  "dump_ctx": False},
            "warning": {"enabled": True,  "dump_ctx": True},
            "error":   {"enabled": True,  "dump_ctx": True, "dump_stack_trace": True},
        }
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scaffold_environment(
    root: Path,
    environment: str,
) -> FolderMakerResult:
    """Create the shared environment-level folders under *root*/<environment>/.

    Creates:
        apps/
        installations/
        logs/installer/
        python/
        etc/

    Args:
        root:        Rey Apps root path, e.g. ~/rey_apps.
        environment: Environment name, e.g. "development", "test", "production".

    Returns:
        FolderMakerResult summarising what was created and what already existed.
    """
    result = FolderMakerResult()
    env_root = root.expanduser().resolve() / environment
    _ensure_dir(env_root, result)

    _ensure_dir(env_root / "apps", result)
    _ensure_dir(env_root / "installations", result)

    logs_root = env_root / "logs"
    _ensure_dir(logs_root, result)
    _ensure_dir(logs_root / "installer", result)

    _ensure_dir(env_root / "python", result)
    _ensure_dir(env_root / "etc", result)

    return result


def scaffold_installation(
    root: Path,
    environment: str,
    installation_name: str,
    config_version: str = _DEFAULT_CONFIG_VERSION,
    contract_version: str = _DEFAULT_CONTRACT_VERSION,
    force: bool = False,
) -> FolderMakerResult:
    """Create the full skeleton for one installation within an environment.

    Creates the shared environment folders (idempotent) then the
    installation-specific folders:

        <env>/installations/<name>/configs/<config_version>/
        <env>/installations/<name>/llm_contracts/<contract_version>/
        <env>/installations/<name>/data/inbox|processing|converted|loaded|rejected|archive|temp/
        <env>/installations/<name>/logs/
        <env>/installations/<name>/runtime/
        <env>/installations/<name>/.env

    Args:
        root:              Rey Apps root path, e.g. ~/rey_apps.
        environment:       Environment name, e.g. "development".
        installation_name: Installation name, e.g. "ccc".
        config_version:    Versioned config folder name, default "v01".
        contract_version:  Versioned LLM contract folder name, default "v01".
        force:             Overwrite existing placeholder YAML and .env files.

    Returns:
        FolderMakerResult summarising what was created and what already existed.
    """
    result = FolderMakerResult()
    root = root.expanduser().resolve()

    # Environment-level folders (idempotent).
    result.merge(scaffold_environment(root, environment))

    env_root  = root / environment
    inst_root = env_root / "installations" / installation_name
    _ensure_dir(inst_root, result)

    # configs/<version>/ — versioned app config root (folder contract layout).
    _ensure_dir(inst_root / "configs", result)
    result.merge(scaffold_config_root(inst_root / "configs" / config_version, force=force))

    # llm_contracts/<version>/ — versioned LLM contract root.
    _ensure_dir(inst_root / "llm_contracts", result)
    contracts_dir = inst_root / "llm_contracts" / contract_version
    _ensure_dir(contracts_dir, result)
    _ensure_gitkeep(contracts_dir, result)

    # data/<subfolder>/ — installation runtime data.
    data_root = inst_root / "data"
    _ensure_dir(data_root, result)
    for sub in _DATA_SUBFOLDERS:
        sub_path = data_root / sub
        _ensure_dir(sub_path, result)
        _ensure_gitkeep(sub_path, result)

    # logs/ — installation app logs.
    _ensure_dir(inst_root / "logs", result)

    # runtime/ — locks, manifests, run markers, execution metadata.
    _ensure_dir(inst_root / "runtime", result)

    # .env — starter template; never overwrites an existing file by default.
    _ensure_env_template(inst_root / ".env", installation_name, result, force=force)

    return result


def scaffold_config_root(config_root: Path, force: bool = False) -> FolderMakerResult:
    """Create the standard app folder layout inside a single config root.

    Creates (folder contract):
        app.yaml
        _draft/  _approved/  _archive/
        <app_name>/app.yaml
        <app_name>/<subfolder>/
        rey_loader/diagnostics/default.yaml
        pipeline_coordinator/app_registry.yaml

    Args:
        config_root: Path to the config root, e.g. installations/ccc/configs/v01.
        force:       Overwrite existing placeholder YAML files.

    Returns:
        FolderMakerResult summarising what was created and what already existed.
    """
    result = FolderMakerResult()
    config_root = config_root.resolve()
    _ensure_dir(config_root, result)

    # Root-level app.yaml.
    _ensure_yaml(
        config_root / "app.yaml",
        {"name": config_root.name, "version": "v01"},
        result,
        force=force,
    )

    # Lifecycle folders.
    for lf in _LIFECYCLE_FOLDERS:
        lf_path = config_root / lf
        _ensure_dir(lf_path, result)
        _ensure_gitkeep(lf_path, result)

    # App folders.
    for app_name, app_def in _APP_DEFS.items():
        app_path = config_root / app_name
        _ensure_dir(app_path, result)
        _ensure_yaml(app_path / "app.yaml", _app_yaml(app_name), result, force=force)

        for sub in app_def["subfolders"]:
            sub_path = app_path / sub
            _ensure_dir(sub_path, result)
            _ensure_gitkeep(sub_path, result)

        # rey_loader diagnostics placeholder.
        if app_name == "rey_loader":
            diag_default = app_path / "diagnostics" / "default.yaml"
            _ensure_yaml(diag_default, _diagnostics_default_yaml(), result, force=force)

        # Extra placeholder files (e.g. pipeline_coordinator/app_registry.yaml).
        for ef in app_def["extra_files"]:
            _ensure_yaml(
                app_path / ef,
                {"name": ef.replace(".yaml", ""), "version": "v01"},
                result,
                force=force,
            )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m rey_lib.installation.folder_maker",
        description="Create a standard Rey Apps installation skeleton.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "scaffold-installation",
        help="Create environment + installation folders for a named installation.",
    )
    p_install.add_argument(
        "--root",
        required=True,
        help="Rey Apps root path (e.g. ~/rey_apps).",
    )
    p_install.add_argument(
        "--environment",
        required=True,
        metavar="ENV",
        help="Environment name (development | test | production).",
    )
    p_install.add_argument(
        "--installation",
        required=True,
        metavar="NAME",
        help="Installation name (e.g. ccc).",
    )
    p_install.add_argument(
        "--config-version",
        default=_DEFAULT_CONFIG_VERSION,
        metavar="VER",
        help=f"Config folder version. Default: {_DEFAULT_CONFIG_VERSION}.",
    )
    p_install.add_argument(
        "--contract-version",
        default=_DEFAULT_CONTRACT_VERSION,
        metavar="VER",
        help=f"LLM contract folder version. Default: {_DEFAULT_CONTRACT_VERSION}.",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing placeholder YAML and .env files.",
    )

    p_env = sub.add_parser(
        "scaffold-environment",
        help="Create only the shared environment-level folders.",
    )
    p_env.add_argument("--root", required=True, help="Rey Apps root path.")
    p_env.add_argument("--environment", required=True, metavar="ENV", help="Environment name.")

    p_cr = sub.add_parser(
        "scaffold-config-root",
        help="Create the standard app folders inside an existing config root.",
    )
    p_cr.add_argument("--path", required=True, help="Config root path.")
    p_cr.add_argument("--force", action="store_true", help="Overwrite existing placeholder files.")

    return parser


def _print_result(result: FolderMakerResult) -> None:
    """Print a human-readable summary of *result* to stdout/stderr."""
    if result.created:
        print(f"\nCreated ({len(result.created)}):")
        for p in result.created:
            print(f"  + {p}")
    if result.existed:
        print(f"\nAlready existed ({len(result.existed)}):")
        for p in result.existed:
            print(f"  = {p}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):", file=sys.stderr)
        for e in result.errors:
            print(f"  ! {e}", file=sys.stderr)


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()

    if args.command == "scaffold-installation":
        result = scaffold_installation(
            root=Path(args.root),
            environment=args.environment,
            installation_name=args.installation,
            config_version=args.config_version,
            contract_version=args.contract_version,
            force=args.force,
        )
    elif args.command == "scaffold-environment":
        result = scaffold_environment(
            root=Path(args.root),
            environment=args.environment,
        )
    else:
        result = scaffold_config_root(
            config_root=Path(args.path),
            force=args.force,
        )

    _print_result(result)
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
