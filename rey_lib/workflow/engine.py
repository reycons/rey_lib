"""
Generic in-process workflow/step engine.

Owns the orchestration mechanics — ordered, fail-closed execution of a registry
of callable steps, with dry-run/apply handling, a shared run context, and result
recording. Domain applications (e.g. rey_db_admin) own the step *handlers*, the
*registry*, and the workflow *definitions*; this engine owns *how* they run.

Engine-independent and domain-agnostic: it never parses domain config beyond the
ordered list of step names.

Public API
----------
RunContext      Shared, mutable context threaded through every step.
StepSpec        One step: name + handler callable (+ apply_only).
StepResult      One step's outcome.
WorkflowResult  Overall outcome with per-step results.
build_steps     Resolve an ordered list of step names against a registry.
run_steps       Execute steps in order, fail-closed, honoring dry-run/apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from rey_lib.logs import get_logger

__all__ = [
    "RunContext",
    "StepSpec",
    "StepResult",
    "WorkflowResult",
    "WorkflowError",
    "build_steps",
    "run_steps",
]

_logger = get_logger(__name__)


class WorkflowError(Exception):
    """Raised for engine-level problems (e.g. an unknown step name)."""


@dataclass
class RunContext:
    """Shared, mutable context passed to every step handler.

    Attributes
    ----------
    apply : bool
        False for dry-run (apply_only steps are skipped); True to run them.
    data : dict
        Cross-step values (handlers read/write, e.g. commit hashes, paths).
    metadata : dict
        Recorded run metadata (the engine records status + step results here).
    """

    apply:    bool            = False
    data:     dict[str, Any]  = field(default_factory=dict)
    metadata: dict[str, Any]  = field(default_factory=dict)


@dataclass
class StepResult:
    """Outcome of one step."""

    name:   str
    status: str            # "ok" | "skipped" | "failed"
    detail: str = ""


@dataclass
class StepSpec:
    """A single workflow step.

    Attributes
    ----------
    name : str
        Step name (matches the workflow definition's step list).
    handler : Callable[[RunContext], Optional[StepResult]]
        The step's work. Returns a StepResult or None (treated as ok). Raising
        fails the step (and, fail-closed, the workflow).
    apply_only : bool
        When True the step is skipped in dry-run mode (mutating steps).
    """

    name:       str
    handler:    Callable[["RunContext"], Optional["StepResult"]]
    apply_only: bool = False


@dataclass
class WorkflowResult:
    """Overall workflow outcome."""

    status:  str                          # "success" | "failed"
    results: list[StepResult]             = field(default_factory=list)
    error:   Optional[BaseException]      = None


def build_steps(
    step_names: Sequence[str],
    registry: Mapping[str, StepSpec],
) -> list[StepSpec]:
    """Resolve an ordered list of step names against a registry.

    Parameters
    ----------
    step_names : Sequence[str]
        Ordered step names from the workflow definition.
    registry : Mapping[str, StepSpec]
        Domain-provided step name -> StepSpec.

    Returns
    -------
    list[StepSpec]
        Steps in the given order.

    Raises
    ------
    WorkflowError
        When a name is not registered.
    """
    steps: list[StepSpec] = []
    for name in step_names:
        spec = registry.get(name)
        if spec is None:
            raise WorkflowError(
                f"unknown workflow step '{name}'. Registered: {sorted(registry)}."
            )
        steps.append(spec)
    return steps


def run_steps(
    steps: Sequence[StepSpec],
    context: RunContext,
    *,
    name: str = "workflow",
) -> WorkflowResult:
    """Execute steps in order, fail-closed, honoring dry-run/apply.

    Records per-step results and overall status into ``context.metadata``.
    Stops at the first failing step and returns a failed result (the exception
    is preserved on ``WorkflowResult.error``).
    """
    results: list[StepResult] = []
    for step in steps:
        if step.apply_only and not context.apply:
            _logger.info("%s: step '%s' skipped (dry-run).", name, step.name)
            results.append(StepResult(step.name, "skipped", "dry-run"))
            continue

        _logger.info("%s: step '%s' running.", name, step.name)
        try:
            outcome = step.handler(context)
        except Exception as exc:  # noqa: BLE001 — fail closed: record and stop
            _logger.error("%s: step '%s' failed: %s", name, step.name, exc)
            results.append(StepResult(step.name, "failed", str(exc)))
            context.metadata["status"] = "failed"
            context.metadata["steps"] = [vars(r) for r in results]
            return WorkflowResult(status="failed", results=results, error=exc)

        results.append(outcome if isinstance(outcome, StepResult)
                       else StepResult(step.name, "ok"))

    context.metadata["status"] = "success"
    context.metadata["steps"] = [vars(r) for r in results]
    return WorkflowResult(status="success", results=results)
