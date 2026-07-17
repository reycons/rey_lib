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

Parentage follows stable per-level anchors: the first record committed at a level,
after that level's anchor has been cleared, becomes that level's anchor, and later
records at the same level never replace it. Entering a deeper level parents to the
nearest anchored level below it. ``last_record_id`` is the global record sequence
only and never determines parentage, so writing additional records at a level cannot
silently move the anchor of the next deeper scope.

Runtime state (keys on the loaded dict):

    last_record_id            global record sequence only; never determines parentage
    parent_level              semantic base scope; established only by set_nest_level
    minimum_nest_level        relative nesting floor; always parent_level + 1
    current_parent_record_id  parent stamped on records at the active level
    level_anchors             {level: first record committed at that level}; seeded {0: 0}

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
    LEVEL_ANCHORS,
    MINIMUM_NEST_LEVEL,
    PARENT_LEVEL,
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


def _largest_anchor_below(anchors: dict[int, int], target: int) -> int:
    """Return the anchor record id at the largest anchored level strictly below target.

    Falls back to the synthetic root when no lower anchored level exists — a direct
    app or workflow run remains valid even with lower semantic levels absent.
    """
    lower = [level for level in anchors if level < target]
    if not lower:
        return _SYNTHETIC_ROOT
    return anchors[max(lower)]


def _clear_from(anchors: dict[int, int], level: int) -> None:
    """Remove the anchor at ``level`` and every deeper anchor, in place."""
    for key in [key for key in anchors if key >= level]:
        del anchors[key]


def _clear_deeper_than(anchors: dict[int, int], level: int) -> None:
    """Remove every anchor deeper than ``level`` in place, keeping ``level`` itself."""
    for key in [key for key in anchors if key > level]:
        del anchors[key]


# -- nest-level transition consumers ------------------------------------------

def on_level_set(state: dict[str, Any], new_level: int) -> None:
    """Consume a semantic base set: start a new named scope at ``new_level``.

    A set always establishes a new scope, so the anchor at ``new_level`` and every
    deeper anchor are cleared: the first record committed at ``new_level`` afterwards
    becomes the new scope's anchor. Records at that level parent to the nearest
    anchored level below it (synthetic root when none), which is what makes a set to
    the same level a sibling scope rather than a continuation.
    """
    anchors = state[LEVEL_ANCHORS]
    _clear_from(anchors, new_level)
    state[PARENT_LEVEL] = new_level
    state[MINIMUM_NEST_LEVEL] = new_level + 1
    state[CURRENT_PARENT_RECORD_ID] = _largest_anchor_below(anchors, new_level)


def on_level_next(state: dict[str, Any], new_level: int) -> None:
    """Consume a nest increase: enter an unnamed relative child level.

    The relative level is entered fresh, so its own and any deeper anchor are cleared
    and records there parent to the nearest anchored level below — the enclosing
    scope's stable anchor, never whichever record happened to be written last.
    """
    anchors = state[LEVEL_ANCHORS]
    _clear_from(anchors, new_level)
    state[CURRENT_PARENT_RECORD_ID] = _largest_anchor_below(anchors, new_level)


def on_level_previous(state: dict[str, Any], new_level: int) -> None:
    """Consume a nest decrease: return upward within the relative hierarchy.

    Only deeper anchors are cleared; the anchor at ``new_level`` survives, so a level
    re-entered after a return keeps the record that originally anchored it.
    """
    anchors = state[LEVEL_ANCHORS]
    _clear_deeper_than(anchors, new_level)
    state[CURRENT_PARENT_RECORD_ID] = _largest_anchor_below(anchors, new_level)


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


def commit_record(ctx: Any, record_id: int, nest_level: int) -> None:
    """Advance the sequence and anchor ``nest_level`` if it is not anchored yet.

    The first record committed at an unanchored level establishes that level's stable
    anchor; later records at the same level leave it untouched, which is what stops
    ordinary informational writes from re-parenting the next deeper scope.
    """
    state, path = run_state.load(ctx)
    state[LAST_RECORD_ID] = int(record_id)
    anchors = state[LEVEL_ANCHORS]
    level = int(nest_level)
    if level not in anchors:
        anchors[level] = int(record_id)
    run_state.save(ctx, state, path)
