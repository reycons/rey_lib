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

from rey_lib.config.applications import Application, ApplicationOperation

__all__ = ["prepared"]

#: Engine vocabulary, which no application publishes.
_ENGINE_KEYS = frozenset({"implementation", "apply_only", "always_run", "enabled", "name"})

#: The application a fixture belongs to when it names none.
_DEFAULT_OWNER = "fixture_app"


def prepared(workflow: dict[str, Any], ctx: Any = None) -> tuple[dict[str, Any], Any]:
    """Return the workflow with implementations bound, and a ctx publishing them.

    Each implementation is published with a contract (backlog 939) declaring
    exactly the top-level settings the fixture configures for it, any value
    allowed: the weakest publication under which the fixture is legal. Several
    processes binding one implementation publish it once, declaring every
    setting any of them configures.

    Args:
        workflow: The fixture definition.
        ctx: A context to publish onto. None builds one.

    Returns:
        The prepared workflow and the context to run it in.
    """
    prepared_workflow = deepcopy(workflow)
    owner = str(prepared_workflow.get("app") or _DEFAULT_OWNER)
    prepared_workflow["app"] = owner

    processes = prepared_workflow.get("processes") or {}
    settings: dict[str, set[str]] = {}
    for process, declared in processes.items():
        declared = declared if isinstance(declared, dict) else {}
        declared.setdefault("implementation", process)
        processes[process] = declared
        settings.setdefault(str(declared["implementation"]), set()).update(
            _configured(prepared_workflow, process, declared)
        )
    prepared_workflow["processes"] = processes

    operations = tuple(
        ApplicationOperation(
            name=name,
            contract_definition={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {one: {} for one in sorted(names)},
                "additionalProperties": False,
            },
            contract_sample={},
        )
        for name, names in settings.items()
    )
    application = Application(name=owner, workflow_operations=operations)
    if ctx is None:
        return prepared_workflow, SimpleNamespace(applications=(application,))
    try:
        setattr(ctx, "applications", (application,))
    except AttributeError:
        object.__setattr__(ctx, "applications", (application,))
    return prepared_workflow, ctx


def _configured(
    workflow: Mapping[str, Any],
    process: str,
    declared: Mapping[str, Any],
) -> set[str]:
    """Every top-level setting this process is configured with, defaults and steps."""
    names = {key for key in declared if key not in _ENGINE_KEYS}
    for step in workflow.get("steps") or []:
        if isinstance(step, dict) and step.get("process") == process:
            names.update(key for key in (step.get("config") or {}) if key not in _ENGINE_KEYS)
    return names

