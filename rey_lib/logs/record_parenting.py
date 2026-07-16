"""Logical record identity and parent resolution (SGC_Rey_Log_Record_Parenting_Phase_2,
descent by SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

The shared run-log writer stamps every newly written semantic record with a
logical ``record_id``, a ``parent_record_id``, and the current ``nest_level``.
Callers remain unaware of record IDs and parent IDs; they declare only semantic
nesting through the Phase 1 nest-level utility, and this module derives the
hierarchy from those transitions.

The authoritative hierarchy state is the per-run companion file owned by
``run_state`` (SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction). The transition
consumers here mutate a loaded state dict in place; the nest-level utility and the
record writer own loading and persisting that state, so a single read-modify-write
surrounds each hierarchy operation and subprocess apps continue the same state.

Runtime state (keys on the loaded dict):

    last_record_id            last logical id successfully written (starts 0)
    current_parent_record_id  parent stamped on records at the active level
    level_parents             {level: record_id} restore points; seeded {0: 0}

Record id 0 is a synthetic root used only for parent resolution; no physical
record with id 0 is written.

Concurrency is out of scope: the model assumes sequential mutation of the shared
hierarchy state. Existing parallel pipeline step-group logging is not made
hierarchy-safe here; no locking, thread-local, token, or concurrency mechanism is
introduced.
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs import run_state
from rey_lib.logs.run_state import (
    CURRENT_PARENT_RECORD_ID,
    LAST_RECORD_ID,
    LEVEL_PARENTS,
)

__all__ = [
    "commit_record",
    "on_level_next",
    "on_level_previous",
    "on_level_set",
    "stamp_record",
]

# Synthetic root: the parent of records with no active lower semantic level.
_SYNTHETIC_ROOT = 0


def _largest_parent_below(levels: dict[int, int], target: int) -> int:
    """Return the record id at the largest active level strictly below target.

    Falls back to the synthetic root when no lower active level exists — a direct
    app or workflow run remains valid even with lower semantic levels absent.
    """
    lower = [level for level in levels if level < target]
    if not lower:
        return _SYNTHETIC_ROOT
    return levels[max(lower)]


def _clear_deeper_than(levels: dict[int, int], level: int) -> None:
    """Remove all stored levels deeper than ``level`` in place (prevents stale children)."""
    for key in [key for key in levels if key > level]:
        del levels[key]


# -- nest-level transition consumers ------------------------------------------

def on_level_set(state: dict[str, Any], new_level: int, prior_level: int) -> None:
    """Consume a semantic base set: descent creates a parent link, else reset/return.

    A set to a level deeper than ``prior_level`` is a semantic descent: the most
    recently written record (``last_record_id``) becomes the parent for records
    written at the new deeper level, mirroring a ``next_nest_level`` descent so that
    base boundaries (pipeline -> pipeline_step, workflow -> workflow_step) nest under
    their orchestrator record rather than the synthetic root
    (SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

    A set to the same or a shallower level is a reset/return: all stored levels deeper
    than ``new_level`` are removed and the parent is restored from the largest active
    level strictly below it (synthetic root when none).
    """
    levels = state[LEVEL_PARENTS]
    if new_level > prior_level:
        last = state[LAST_RECORD_ID]
        levels[prior_level] = last
        _clear_deeper_than(levels, new_level)
        state[CURRENT_PARENT_RECORD_ID] = last
        return
    _clear_deeper_than(levels, new_level)
    state[CURRENT_PARENT_RECORD_ID] = _largest_parent_below(levels, new_level)


def on_level_next(state: dict[str, Any], current_level: int) -> None:
    """Consume a nest increase: the last written record parents the deeper level.

    The most recently successfully written record (``last_record_id``) becomes the
    active parent for the current level and the parent for records subsequently
    written one level deeper.
    """
    last = state[LAST_RECORD_ID]
    levels = state[LEVEL_PARENTS]
    levels[current_level] = last
    _clear_deeper_than(levels, current_level + 1)
    state[CURRENT_PARENT_RECORD_ID] = last


def on_level_previous(state: dict[str, Any], new_level: int) -> None:
    """Consume a nest decrease: return to an existing higher parent context.

    A decrease never uses the newest deeper record as a parent. Deeper levels are
    cleared and the parent is restored from the largest active level strictly below
    the new level.
    """
    levels = state[LEVEL_PARENTS]
    _clear_deeper_than(levels, new_level)
    state[CURRENT_PARENT_RECORD_ID] = _largest_parent_below(levels, new_level)


# -- record writer hooks ------------------------------------------------------

def stamp_record(ctx: Any, record: dict[str, Any], nest_level: int) -> int:
    """Stamp record_id, parent_record_id, and nest_level; return the record_id.

    record_id = last_record_id + 1 read from the shared run state. The record is
    stamped before the append; the id is committed only after a successful append
    (see ``commit_record``), so a failed write does not advance the sequence.
    """
    state, _ = run_state.load(ctx)
    record_id = state[LAST_RECORD_ID] + 1
    record["record_id"] = record_id
    record["parent_record_id"] = state[CURRENT_PARENT_RECORD_ID]
    record["nest_level"] = int(nest_level)
    return record_id


def commit_record(ctx: Any, record_id: int) -> None:
    """Advance last_record_id in the shared run state after a successful append."""
    state, path = run_state.load(ctx)
    state[LAST_RECORD_ID] = int(record_id)
    run_state.save(ctx, state, path)
