"""
Shared, domain-agnostic workflow/step engine for Rey Apps.

Owns orchestration mechanics (ordered, fail-closed execution, dry-run/apply,
shared run context, result recording, error propagation) over a registry of
callable steps. Domain apps own the step handlers, registry, and definitions.
"""

from rey_lib.workflow.coordinator import (
    ProcessHandler,
    StepOutcome,
    WorkflowRun,
    run_workflow,
)
from rey_lib.workflow.engine import (
    RunContext,
    StepResult,
    StepSpec,
    WorkflowError,
    WorkflowResult,
    build_steps,
    run_steps,
)

__all__ = [
    "RunContext",
    "StepSpec",
    "StepResult",
    "WorkflowResult",
    "WorkflowError",
    "build_steps",
    "run_steps",
    "ProcessHandler",
    "StepOutcome",
    "WorkflowRun",
    "run_workflow",
]
