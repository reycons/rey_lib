"""
Shared semantic nest-level utility (SGC_Rey_Log_Nest_Level_Phase_1).

Execution code declares *semantic* boundaries — pipeline, pipeline step, app,
workflow, workflow step, and relative nested sections — while this utility owns the
numeric nest state. Callers never compute base levels, mutate the underlying field
directly, or manage record IDs, parent IDs, or any tree mechanics. A later phase
consumes this state to build explicit parent-child log relationships without callers
changing again.

Fixed semantic bases (independent of how execution was invoked). Pipelines
orchestrate apps; workflows execute inside an app, so workflow sits below app
(SGC_Rey_Log_Nest_Level_Hierarchy_Correction):

    pipeline      = 1
    pipeline_step = 2
    app           = 3
    workflow      = 4
    workflow_step = 5

``set_nest_level`` is not plain numeric assignment. Setting a semantic base also
resets or descends the active nesting: a deeper base nests under the most recent
record, while a same/shallower base discards deeper nesting and returns to that
base (parent resolution: SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

The authoritative nest and parent state is the per-run companion file owned by
``run_state`` (SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction), derived from the
run log path so every process writing the same run log shares one state. Each
function here reads that state, applies its transition, and persists it — no
authoritative nest state is kept on ctx. When no durable run log resolves (a bare
test context) ``run_state`` uses an in-memory store, which is then the sole store.
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs.logging_setup import get_logger
from rey_lib.logs import record_parenting, run_state
from rey_lib.logs.run_state import CURRENT_NEST_LEVEL

__all__ = [
    "get_nest_level",
    "next_nest_level",
    "previous_nest_level",
    "set_nest_level",
]

_logger = get_logger(__name__)

# Fixed semantic base levels. These do not vary with invocation path. Ordering
# reflects the real execution hierarchy: pipeline -> pipeline step -> app ->
# workflow -> workflow step (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
_SEMANTIC_BASES: dict[str, int] = {
    "pipeline": 1,
    "pipeline_step": 2,
    "app": 3,
    "workflow": 4,
    "workflow_step": 5,
}

# Level 0 means "no semantic base established". The level never goes below it.
_MIN_LEVEL = 0


def set_nest_level(ctx: Any, semantic_level: str) -> int:
    """Establish a fixed semantic base level.

    Resolves ``semantic_level`` (one of "pipeline", "pipeline_step", "app",
    "workflow", "workflow_step") to its fixed numeric level and sets the current
    level to it. A set to a level deeper than the current one is a semantic descent
    that nests under the most recent record; a set to the same or a shallower level
    discards any deeper nesting and returns to that base — so a new boundary is
    self-correcting even if a prior nested section exited abnormally
    (parent resolution: SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

    Parameters
    ----------
    ctx : Any
        The execution context carrying nest state.
    semantic_level : str
        A known semantic base name.

    Returns
    -------
    int
        The numeric level established.

    Raises
    ------
    ValueError
        If ``semantic_level`` is not a known semantic base.
    """
    if semantic_level not in _SEMANTIC_BASES:
        raise ValueError(
            f"Unknown semantic nest level: {semantic_level!r}. "
            f"Known bases: {sorted(_SEMANTIC_BASES)}."
        )
    level = _SEMANTIC_BASES[semantic_level]
    # Read-modify-write the shared run state. A set to a deeper level is a semantic
    # descent that parents the deeper level to the last written record; a set to the
    # same or a shallower level clears deeper levels and restores the lower parent
    # (SGC_Rey_Log_Parent_Resolver_Semantic_Descent).
    state, path = run_state.load(ctx)
    prior_level = int(state[CURRENT_NEST_LEVEL])
    record_parenting.on_level_set(state, level, prior_level)
    state[CURRENT_NEST_LEVEL] = level
    run_state.save(ctx, state, path)
    return level


def next_nest_level(ctx: Any) -> int:
    """Enter a nested semantic section: increase the current level by exactly one.

    Parameters
    ----------
    ctx : Any
        The execution context carrying nest state.

    Returns
    -------
    int
        The new level.
    """
    state, path = run_state.load(ctx)
    current = int(state[CURRENT_NEST_LEVEL])
    # The last written record parents the new deeper level.
    record_parenting.on_level_next(state, current)
    level = current + 1
    state[CURRENT_NEST_LEVEL] = level
    run_state.save(ctx, state, path)
    return level


def previous_nest_level(ctx: Any) -> int:
    """Leave a nested semantic section: decrease the current level by exactly one.

    Never produces a negative level; a decrement at or below the floor is clamped
    and reported rather than corrupting state.

    Parameters
    ----------
    ctx : Any
        The execution context carrying nest state.

    Returns
    -------
    int
        The new level.
    """
    state, path = run_state.load(ctx)
    current = int(state[CURRENT_NEST_LEVEL])
    level = current - 1
    if level < _MIN_LEVEL:
        _logger.warning(
            "previous_nest_level below floor (current=%d); clamping to %d.",
            current, _MIN_LEVEL,
        )
        level = _MIN_LEVEL
    # Return to an existing higher parent context.
    record_parenting.on_level_previous(state, level)
    state[CURRENT_NEST_LEVEL] = level
    run_state.save(ctx, state, path)
    return level


def get_nest_level(ctx: Any) -> int:
    """Return the current numeric nest level, or 0 when none is established.

    Parameters
    ----------
    ctx : Any
        The execution context carrying nest state.

    Returns
    -------
    int
        The current level.
    """
    state, _ = run_state.load(ctx)
    try:
        return int(state[CURRENT_NEST_LEVEL])
    except (TypeError, ValueError, KeyError):
        return _MIN_LEVEL
