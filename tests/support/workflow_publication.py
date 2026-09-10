"""Preparing a workflow fixture for the published-contract coordinator.

A process binds a name to an operation its application publishes, and the
coordinator validates a step's configuration against that publication before
dispatching. A fixture written before that contract existed declares neither,
so this supplies both from what the fixture already says.

It publishes exactly what the workflow uses -- every configured key becomes a
declared parameter of the implementation it is configured for. That is the
weakest publication under which the fixture is legal, so a test still proves
what it was written to prove and nothing is accidentally allowed through.

A test about refusal declares its own publication instead. This is for the
fixtures whose subject is something else: selection, dry-run, nesting, run-log
evidence.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Mapping

from rey_lib.config.applications import (
    Application,
    ApplicationCommand,
    ApplicationCommandParameter,
)

__all__ = ["prepared"]

#: Engine vocabulary, which no application publishes.
_ENGINE_KEYS = frozenset({"implementation", "apply_only", "enabled", "name"})


def prepared(workflow: dict[str, Any], ctx: Any = None) -> tuple[dict[str, Any], Any]:
    """Return the workflow with implementations bound, and a ctx publishing them.

    Args:
        workflow: The fixture definition.
        ctx: A context to publish onto. None builds one.

    Returns:
        The prepared workflow and the context to run it in.
    """
    prepared_workflow = deepcopy(workflow)
    owner = str(prepared_workflow.get("app") or "fixture_app")
    prepared_workflow["app"] = owner

    processes = prepared_workflow.get("processes") or {}
    operations: list[ApplicationCommand] = []
    for process, declared in processes.items():
        declared = declared if isinstance(declared, dict) else {}
        declared.setdefault("implementation", process)
        processes[process] = declared
        operations.append(
            ApplicationCommand(
                name=str(declared["implementation"]),
                parameters=tuple(
                    ApplicationCommandParameter(name=name)
                    for name in _configured(prepared_workflow, process, declared)
                ),
            )
        )
    prepared_workflow["processes"] = processes

    application = Application(name=owner, workflow_operations=tuple(operations))
    if ctx is None:
        return prepared_workflow, SimpleNamespace(applications=(application,))
    setattr(ctx, "applications", (application,))
    return prepared_workflow, ctx


def _configured(
    workflow: Mapping[str, Any],
    process: str,
    declared: Mapping[str, Any],
) -> list[str]:
    """Every setting this process is configured with, defaults and steps."""
    names: set[str] = set(_keys(declared))
    for step in workflow.get("steps") or []:
        if isinstance(step, dict) and step.get("process") == process:
            names.update(_keys(step.get("config") or {}))
    return sorted(names)


def _keys(config: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Configured keys, nested ones written as ``parent.child``."""
    names: list[str] = []
    for key, value in config.items():
        if not prefix and key in _ENGINE_KEYS:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            names.extend(_keys(value, prefix=f"{name}."))
        else:
            names.append(name)
    return names
