"""Read-only installation inventory built from a resolved config context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.config.config_utils import Namespace
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "InstallationInventory",
    "build_installation_inventory",
]


@dataclass(frozen=True)
class InstallationInventory:
    """Immutable installation inventory derived from an already-loaded ctx."""

    apps: tuple[Mapping[str, Any], ...]
    workflows: tuple[Mapping[str, Any], ...]
    pipelines: tuple[Mapping[str, Any], ...]
    contracts: tuple[Mapping[str, Any], ...]
    llm_profiles: tuple[Mapping[str, Any], ...]
    connections: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    paths: Mapping[str, str]
    logging: Mapping[str, Any]
    artifact_settings: Mapping[str, Any]
    workflow_run_actions: tuple[Mapping[str, Any], ...]
    workflows_by_app: Mapping[str, tuple[str, ...]]
    validation_errors: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe plain dict copy of the inventory."""
        return _thaw(self)


def build_installation_inventory(ctx: Any) -> InstallationInventory:
    """Build an immutable inventory from a context loaded by config_utils."""
    apps = _named_entries(getattr(ctx, "apps", None))
    app_names = {str(app.get("name")) for app in apps if app.get("name")}
    workflows = _workflow_entries(ctx)
    pipelines = _pipeline_entries(ctx)
    contracts = _contract_entries(ctx)
    llm_profiles = _named_entries(
        getattr(ctx, "llm_profiles", None) or getattr(ctx, "llm_configs", None)
    )
    connections = _connection_entries(ctx)
    tools = _named_entries(getattr(ctx, "tools", None))
    paths = _path_entries(ctx)
    logging = _to_plain(getattr(ctx, "logging", None))
    artifact_settings = _to_plain(getattr(ctx, "artifact_processing", None))
    run_actions = _workflow_run_actions(ctx, apps, workflows)
    workflows_by_app = _workflows_by_app(workflows)

    errors = _validate_inventory(app_names, workflows, contracts)
    if errors:
        details = "; ".join(str(error) for error in errors)
        raise ConfigError(f"Installation inventory validation failed: {details}")

    return InstallationInventory(
        apps=_freeze(apps),
        workflows=_freeze(workflows),
        pipelines=_freeze(pipelines),
        contracts=_freeze(contracts),
        llm_profiles=_freeze(llm_profiles),
        connections=_freeze(connections),
        tools=_freeze(tools),
        paths=_freeze(paths),
        logging=_freeze(logging),
        artifact_settings=_freeze(artifact_settings),
        workflow_run_actions=_freeze(run_actions),
        workflows_by_app=_freeze(workflows_by_app),
        validation_errors=(),
    )


def _workflow_entries(ctx: Any) -> list[dict[str, Any]]:
    """Return normalized workflow rows from ctx.workflows."""
    rows: list[dict[str, Any]] = []
    workflows = _to_plain(getattr(ctx, "workflows", None))
    root_app = _to_plain(getattr(ctx, "app", None))
    root_app = root_app if isinstance(root_app, str) else ""

    if isinstance(workflows, dict):
        items = workflows.items()
    elif isinstance(workflows, list):
        items = ((str(item.get("name", "")), item) for item in workflows if isinstance(item, dict))
    else:
        items = []

    for name, workflow in items:
        if not name or not isinstance(workflow, dict):
            continue
        owner = str(workflow.get("app") or workflow.get("owner_app") or root_app)
        rows.append(
            {
                "name": str(name),
                "app": owner,
                "kind": str(workflow.get("kind") or "workflow"),
                "description": str(workflow.get("description") or ""),
                "steps": workflow.get("steps") or [],
                "source_section": "workflows",
            }
        )

    return rows


def _pipeline_entries(ctx: Any) -> list[dict[str, Any]]:
    """Return normalized pipeline rows from ctx.pipelines."""
    rows: list[dict[str, Any]] = []
    pipelines = _to_plain(getattr(ctx, "pipelines", None))
    if not isinstance(pipelines, dict):
        pc = getattr(ctx, "pipeline_coordinator", None)
        pipelines = _to_plain(getattr(pc, "pipelines", None) if pc else None)
    if not isinstance(pipelines, dict):
        return rows

    for name, pipeline in pipelines.items():
        if not isinstance(pipeline, dict):
            continue
        rows.append(
            {
                "name": str(pipeline.get("name") or name),
                "steps": pipeline.get("steps") or [],
                "source_section": "pipelines",
            }
        )
    return rows


def _workflow_run_actions(
    ctx: Any,
    apps: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return normalized executable action metadata for workflows."""
    app_by_name = {str(app.get("name")): app for app in apps if app.get("name")}
    config_path = str(getattr(ctx, "config_path", ""))
    rows: list[dict[str, Any]] = []

    for workflow in workflows:
        app_name = str(workflow.get("app") or "")
        app_entry = app_by_name.get(app_name)
        if not app_entry:
            rows.append(
                {
                    "app": app_name,
                    "workflow": workflow["name"],
                    "executable": False,
                    "reason": "not executable: unknown owner app",
                }
            )
            continue

        command = _workflow_command(app_entry, workflow["name"], config_path)
        dry_run = _supports_dry_run(app_entry)
        rows.append(
            {
                "app": app_name,
                "workflow": workflow["name"],
                "display_label": f"Run {workflow['name']}",
                "command_type": "cli",
                "cli_command": command,
                "command_preview": " ".join(command),
                "copyable_command": " ".join(command),
                "entry_point": str(app_entry.get("entry_point") or "main.py"),
                "required_arguments": ["workflow"],
                "optional_arguments": ["dry-run"] if dry_run else [],
                "default_execution_flags": [],
                "dry_run_capable": dry_run,
                "confirmation_required": not dry_run,
                "source_section": "workflows",
                "executable": True,
            }
        )

    return rows


def _workflow_command(
    app_entry: dict[str, Any],
    workflow_name: str,
    config_path: str,
) -> list[str]:
    """Build the diagnostic CLI command for a workflow action."""
    app_name = str(app_entry.get("name") or "")
    if app_name == "rey_loader":
        return [
            app_name,
            "run-workflow",
            "--workflow",
            workflow_name,
            "--config-path",
            config_path,
        ]

    return [
        app_name,
        "run-workflow",
        "--workflow",
        workflow_name,
        "--config-path",
        config_path,
    ]


def _supports_dry_run(app_entry: dict[str, Any]) -> bool:
    """Return true when app CLI metadata exposes a dry-run flag."""
    cli = app_entry.get("cli")
    if not isinstance(cli, dict):
        return False

    for parameter in cli.get("parameters") or []:
        if isinstance(parameter, dict) and parameter.get("name") == "dry-run":
            return True
    for command in cli.get("commands") or []:
        if not isinstance(command, dict):
            continue
        for parameter in command.get("parameters") or []:
            if isinstance(parameter, dict) and parameter.get("name") == "dry-run":
                return True
    return False


def _workflows_by_app(workflows: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    rows: dict[str, list[str]] = {}
    for workflow in workflows:
        app = str(workflow.get("app") or "")
        if not app:
            continue
        rows.setdefault(app, []).append(str(workflow["name"]))
    return {app: tuple(sorted(names)) for app, names in rows.items()}


def _contract_entries(ctx: Any) -> list[dict[str, Any]]:
    """Find explicit contract_file references already present on the ctx."""
    rows: list[dict[str, Any]] = []
    _collect_contracts(_to_plain(getattr(ctx, "workflows", None)), "workflows", rows)
    _collect_contracts(_to_plain(getattr(ctx, "analysis_configs", None)), "analysis_configs", rows)
    _collect_contracts(_to_plain(getattr(ctx, "data_sources", None)), "data_sources", rows)
    return rows


def _collect_contracts(value: Any, section: str, rows: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if "contract_file" in value:
            rows.append(
                {
                    "section": section,
                    "field": "contract_file",
                    "contract_file": value["contract_file"],
                }
            )
        for child in value.values():
            _collect_contracts(child, section, rows)
    elif isinstance(value, list):
        for child in value:
            _collect_contracts(child, section, rows)


def _named_entries(value: Any) -> list[dict[str, Any]]:
    """Return normalized named entries from list or mapping config sections."""
    raw = _to_plain(value)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        rows: list[dict[str, Any]] = []
        for name, item in raw.items():
            if isinstance(item, dict):
                item.setdefault("name", str(name))
                rows.append(item)
        return rows
    return []


def _connection_entries(ctx: Any) -> list[dict[str, Any]]:
    rows = _named_entries(getattr(ctx, "connections", None))
    rows.extend(_named_entries(getattr(ctx, "db_connections", None)))
    return rows


def _path_entries(ctx: Any) -> dict[str, str]:
    paths = getattr(ctx, "paths", None)
    internal = getattr(paths, "_paths", None)
    if not isinstance(internal, dict):
        return {}
    return {str(name): str(path) for name, path in internal.items()}


def _validate_inventory(
    app_names: set[str],
    workflows: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    seen_workflows: dict[str, str] = {}

    for workflow in workflows:
        name = str(workflow.get("name") or "")
        app = str(workflow.get("app") or "")
        if not app:
            errors.append(_error("workflows", name, "app", app, "workflow owner app is required"))
        elif app not in app_names:
            errors.append(_error("workflows", name, "app", app, "known app name"))

        previous = seen_workflows.get(name)
        if previous and previous != app:
            errors.append(_error("workflows", name, "name", name, "unique workflow name or explicit app"))
        else:
            seen_workflows[name] = app

    for contract in contracts:
        value = contract.get("contract_file")
        if isinstance(value, str):
            path = Path(value)
        elif isinstance(value, Path):
            path = value
        else:
            continue
        if not path.exists():
            errors.append(
                _error(
                    str(contract.get("section") or "contracts"),
                    "contract_file",
                    "contract_file",
                    str(value),
                    "existing contract file",
                )
            )

    return errors


def _error(
    section: str,
    item: str,
    field: str,
    value: Any,
    expected: str,
) -> dict[str, Any]:
    return {
        "config_file": "",
        "section": section,
        "item": item,
        "field": field,
        "bad_value": value,
        "expected": expected,
    }


def _to_plain(value: Any) -> Any:
    if isinstance(value, Namespace):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_to_plain(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, InstallationInventory):
        return {
            "apps": _thaw(value.apps),
            "workflows": _thaw(value.workflows),
            "pipelines": _thaw(value.pipelines),
            "contracts": _thaw(value.contracts),
            "llm_profiles": _thaw(value.llm_profiles),
            "connections": _thaw(value.connections),
            "tools": _thaw(value.tools),
            "paths": _thaw(value.paths),
            "logging": _thaw(value.logging),
            "artifact_settings": _thaw(value.artifact_settings),
            "workflow_run_actions": _thaw(value.workflow_run_actions),
            "workflows_by_app": _thaw(value.workflows_by_app),
            "validation_errors": _thaw(value.validation_errors),
        }
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
