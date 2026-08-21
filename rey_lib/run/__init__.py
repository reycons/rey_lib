"""
The shared run subsystem: identity, lifecycle, state and the canonical run
response.

It composes the existing shared subsystems rather than reimplementing them.
Logging, errors and files keep their own ownership; this binds their outputs to
a run and exposes them as one shape.
"""

from rey_lib.run.identity import establish_run_identity, mint_run_id
from rey_lib.run.state import (
    SUPPORTED_STATES,
    current_step,
    execution_records,
    execution_state,
    log_reference,
    metrics,
    plan_app,
    plan_index,
    progress,
    run_sections,
    scoped_plan,
    status_of,
)

__all__ = [
    "SUPPORTED_STATES",
    "establish_run_identity",
    "current_step",
    "execution_records",
    "execution_state",
    "log_reference",
    "metrics",
    "mint_run_id",
    "plan_app",
    "plan_index",
    "progress",
    "run_sections",
    "scoped_plan",
    "status_of",
]
