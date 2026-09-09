"""The workflow run action, and the one way configuration becomes plain data.

Two responsibilities, and deliberately no third. This module used to also build
an ``InstallationInventory`` -- a whole-installation aggregate of apps,
workflows, pipelines, contracts, connections and their derived relationships.
Nothing in the runtime ever called it. It was removed because a second answer
to "what is the runtime model of an installation?" is not harmless: the
canonical answer is the objects on the context, and an unused aggregate beside
them makes obsolete architecture look supported.

Whatever needs one workflow's run action asks for that one action. Whatever
needs configuration as plain data asks ``to_plain_data``. Neither reconstructs
an installation.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "resolve_workflow_run_action",
    "to_plain_data",
]


def resolve_workflow_run_action(
    ctx: Any,
    app_name: str,
    workflow_name: str,
) -> dict[str, Any]:
    """Return the canonical run action for one configured workflow.

    This is the public execution-capability boundary for consumers that need
    one workflow action. It delegates to the same normalization and
    capability logic the workflow rows carry, and it constructs nothing
    beyond the one action asked for.
    """
    requested_app = str(app_name or "").strip()
    requested_workflow = str(workflow_name or "").strip()
    if not requested_app or not requested_workflow:
        raise ConfigError("Workflow app and workflow name are required.")

    workflows = [
        workflow
        for workflow in _workflow_entries(ctx)
        if str(workflow.get("app") or "") == requested_app
        and str(workflow.get("name") or "") == requested_workflow
    ]
    if not workflows:
        raise ConfigError(
            f"Workflow run action not found: {requested_app}/{requested_workflow}"
        )

    actions = _workflow_run_actions(
        ctx,
        _named_entries(getattr(ctx, "apps", None)),
        workflows,
    )
    if not actions:
        raise ConfigError(
            f"Workflow run action not found: {requested_app}/{requested_workflow}"
        )
    return actions[0]

def _workflow_entries(ctx: Any) -> list[dict[str, Any]]:
    """Return normalized workflow rows from ctx.workflows."""
    rows: list[dict[str, Any]] = []
    workflows = to_plain_data(getattr(ctx, "workflows", None))
    root_app = to_plain_data(getattr(ctx, "app", None))
    root_app = root_app if isinstance(root_app, str) else ""
    source_config = str(getattr(ctx, "config_path", "") or "")

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
                "relevant_files": workflow.get("relevant_files") or {},
                # Workflow-declared execution contract (ADR-007): the nested
                # execution:{full,dry_run,step,range} block. Defaults keep a
                # workflow runnable but never step/range selectable unless it
                # opts in (unsafe partial execution never enabled by omission).
                "execution": _workflow_execution(workflow),
                "llm_profile": workflow.get("llm_profile"),
                "execution_profile": workflow.get("execution_profile"),
                "connection": workflow.get("connection"),
                "target_connection": workflow.get("target_connection"),
                "source_config_file": source_config,
                "source_section": "workflows",
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
    source_config = str(getattr(ctx, "config_path", "") or "")
    rows: list[dict[str, Any]] = []

    for workflow in workflows:
        app_name = str(workflow.get("app") or "")
        app_entry = app_by_name.get(app_name)
        if not app_entry:
            rows.append(
                {
                    "app": app_name,
                    "workflow": workflow["name"],
                    # Resolved execution contract (ADR-007). Unknown owner app -> the
                    # workflow cannot run in any mode.
                    "execution": {"full": False, "dry_run": False, "step": False, "range": False},
                    "reason": "not executable: unknown owner app",
                    "source_config_file": workflow.get("source_config_file") or source_config,
                    "source_section": "workflows",
                }
            )
            continue

        command = _workflow_command(app_entry, workflow["name"], config_path)
        # Resolve the workflow's declared execution contract (ADR-007) against
        # what the app CLI can actually honour, and surface the SAME nested
        # execution: {full, dry_run, step, range} shape (no duplicate flat
        # capability fields). Full run and dry-run require both the workflow's
        # declaration and app support; step/range are purely the workflow's
        # contract (the shared coordinator supports them for all).
        declared = _workflow_execution(workflow)
        resolved = {
            "full": declared["full"],
            "dry_run": _supports_dry_run(app_entry) and declared["dry_run"],
            "step": declared["step"],
            "range": declared["range"],
        }
        rows.append(
            {
                "app": app_name,
                "workflow": workflow["name"],
                "display_label": f"Run {workflow['name']}",
                "command_type": "cli",
                "cli_command": command,
                "command_preview": " ".join(command),
                "copyable_command": " ".join(command),
                "app_name": app_name,
                "workflow_name": workflow["name"],
                "entry_point": str(app_entry.get("entry_point") or "main.py"),
                "app_path": str(app_entry.get("app_path") or ""),
                "config_path": config_path,
                "required_arguments": ["workflow"],
                "optional_arguments": ["dry-run"] if resolved["dry_run"] else [],
                "default_execution_flags": [],
                "execution": resolved,
                "confirmation_required": not resolved["dry_run"],
                "source_config_file": workflow.get("source_config_file") or source_config,
                "source_section": "workflows",
            }
        )

    return rows

def _workflow_execution(workflow: dict[str, Any]) -> dict[str, bool]:
    """Return the normalized execution contract for a workflow (ADR-007).

    Reads the declared ``execution: {full, dry_run, step, range}`` block. A
    workflow defaults to full-run and dry-run enabled but step/range disabled, so
    unsafe partial execution is never enabled by omission.
    """
    raw = workflow.get("execution")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "full": bool(raw.get("full", True)),
        "dry_run": bool(raw.get("dry_run", True)),
        "step": bool(raw.get("step", False)),
        "range": bool(raw.get("range", False)),
    }

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

def _named_entries(value: Any) -> list[dict[str, Any]]:
    """Return normalized named entries from list or mapping config sections."""
    raw = to_plain_data(value)
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

def to_plain_data(value: Any) -> Any:
    """Return configuration as plain JSON-safe data.

    The one answer to "serialize this config object", for every surface that
    hands configuration to something outside the process -- an API response, a
    Tree payload, a run action. A second normalizer would be a second
    answer, and the two would drift.

    The conversion is structural and nothing more. It reads no environment and
    resolves nothing, so a value naming an environment variable comes out as
    the name it went in as. That is what makes these surfaces safe by
    construction rather than by a redaction rule applied afterwards: there is
    no resolved value in a context to leak.

    Parameters
    ----------
    value : Any
        Namespace, mapping, sequence, Path, an object exposing ``to_dict()``,
        or a scalar.

    Returns
    -------
    Any
        Dicts, lists, strings and scalars only. Sequences become lists so the
        result is JSON-safe; callers wanting immutability freeze it themselves.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_plain_data(to_dict())
    if isinstance(value, Namespace):
        return {k: to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (dict, MappingProxyType)):
        return {k: to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
