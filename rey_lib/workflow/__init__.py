"""
Shared, domain-agnostic workflow/step engine for Rey Apps.

Owns orchestration mechanics (ordered, fail-closed execution, dry-run/apply,
shared run context, result recording, error propagation) over a registry of
callable steps. Domain apps own the step handlers, registry, and definitions.

The exports below resolve on first access rather than at import, so importing
one submodule -- rey_lib.workflow.workflow, .workflow_process, .workflow_step --
loads no other (backlog 950: the workflow objects run with the legacy
coordinator and engine unavailable). Every name is exported exactly as before.
"""

from importlib import import_module
from typing import Any

#: Each exported name and the module that owns it.
_EXPORTS = {
    "ProcessHandler": "rey_lib.workflow.coordinator",
    "StepOutcome": "rey_lib.workflow.coordinator",
    "WorkflowRun": "rey_lib.workflow.coordinator",
    "run_workflow": "rey_lib.workflow.coordinator",
    "RunContext": "rey_lib.workflow.engine",
    "StepResult": "rey_lib.workflow.engine",
    "WorkflowError": "rey_lib.workflow.engine",
}

__all__ = [
    "RunContext",
    "StepResult",
    "WorkflowError",
    "ProcessHandler",
    "StepOutcome",
    "WorkflowRun",
    "run_workflow",
]


def __getattr__(name: str) -> Any:
    """Resolve an exported name from its owning module on first access."""
    owner = _EXPORTS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(owner), name)
    globals()[name] = value
    return value
