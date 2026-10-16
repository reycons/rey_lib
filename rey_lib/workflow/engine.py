"""
Shared workflow types: the error, the run context a step handler receives, and a
step's result.

What remains of the in-process step engine (backlog 920): its build_steps /
run_steps / StepSpec / WorkflowResult had no production consumer and were removed.
These three are still read by step handlers and the legacy coordinator.

Public API
----------
RunContext      Shared, mutable context threaded through every step.
StepResult      One step's outcome.
WorkflowError   A workflow-level error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rey_lib.errors.error_utils import AppError

__all__ = [
    "RunContext",
    "StepResult",
    "WorkflowError",
]


class WorkflowError(AppError):
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
