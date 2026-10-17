"""
Shared workflow types: the run context a step handler receives, and a step's
result.

What remains of the in-process step engine (backlog 920): its build_steps /
run_steps / StepSpec / WorkflowResult had no production consumer and were removed,
and WorkflowError went with the legacy coordinator (backlog 1014). These two are
read by every application's step handlers.

Public API
----------
RunContext      Shared, mutable context threaded through every step.
StepResult      One step's outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RunContext",
    "StepResult",
]


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
