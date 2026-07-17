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

``set_nest_level`` is not plain numeric assignment. It starts a new named semantic
scope at its fixed level: the anchor at that level and below is discarded, so the
first record written afterwards anchors the new scope, and the relative nesting floor
is rebased to ``parent_level + 1``. A set to the same level is therefore a sibling
scope, not a continuation, and a set is the only way to leave a relative context.

Relative nesting lives beneath the established base: ``next_nest_level`` enters or
descends unnamed child levels starting at ``minimum_nest_level``, and
``previous_nest_level`` returns upward but never past that floor. Parentage follows
each level's stable anchor — the first record committed there — so ordinary records
written at a level never re-parent the next deeper scope
(parent resolution: SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

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
from rey_lib.logs.run_state import CURRENT_NEST_LEVEL, MINIMUM_NEST_LEVEL

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
    # Read-modify-write the shared run state. A set always starts a new named scope at
    # this level: it clears the anchor there and below, so the first record written
    # afterwards anchors the scope, and it rebases the relative nesting floor.
    state, path = run_state.load(ctx)
    record_parenting.on_level_set(state, level)
    state[CURRENT_NEST_LEVEL] = level
    run_state.save(ctx, state, path)
    return level


def next_nest_level(ctx: Any) -> int:
    """Enter or descend the relative child hierarchy beneath the established base.

    The first descent from the base lands on ``minimum_nest_level`` (base + 1);
    further calls descend one level each. This establishes no new semantic base.

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
    # Relative nesting starts at the floor, so a descent from the base cannot land
    # on the base itself.
    level = max(current + 1, int(state[MINIMUM_NEST_LEVEL]))
    record_parenting.on_level_next(state, level)
    state[CURRENT_NEST_LEVEL] = level
    run_state.save(ctx, state, path)
    return level


def previous_nest_level(ctx: Any) -> int:
    """Return upward within the relative child hierarchy owned by the current base.

    A return never moves above ``minimum_nest_level``: relative nesting cannot escape
    into or below its own base, and only ``set_nest_level`` establishes a new base.
    A return also never moves deeper, so calling it while sitting on the base — where
    the floor is one level below the current position — leaves the level unchanged.

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
    floor = max(int(state[MINIMUM_NEST_LEVEL]), _MIN_LEVEL)
    level = min(current, max(current - 1, floor))
    if level == current and current >= floor:
        _logger.warning(
            "previous_nest_level at the relative floor (current=%d); holding at %d.",
            current, floor,
        )
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
