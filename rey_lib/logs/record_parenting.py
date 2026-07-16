"""
Logical record identity and parent resolution (SGC_Rey_Log_Record_Parenting_Phase_2).

The shared run-log writer stamps every newly written semantic record with a
logical ``record_id``, a ``parent_record_id``, and the current ``nest_level``.
Callers remain unaware of record IDs and parent IDs; they declare only semantic
nesting through the Phase 1 nest-level utility, and this module derives the
hierarchy from those transitions.

Runtime state lives per run-log context on ctx:

    last_record_id            last logical id successfully written (starts 0)
    current_parent_record_id  parent stamped on records at the active level
    level_parents             {level: record_id} restore points; seeded {0: 0}

Record id 0 is a synthetic root used only for parent resolution; no physical
record with id 0 is written.

Concurrency is out of scope for this phase. This model assumes sequential
mutation of the shared hierarchy state. Existing parallel pipeline step-group
logging is not made hierarchy-safe here and remains outside Phase 2 scope; no
locking, thread-local, token, or concurrency mechanism is introduced.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "commit_record",
    "on_level_next",
    "on_level_previous",
    "on_level_set",
    "stamp_record",
]

_LAST_FIELD = "_rey_last_record_id"
_PARENT_FIELD = "_rey_current_parent_id"
_LEVELS_FIELD = "_rey_level_parents"

# Synthetic root: the parent of records with no active lower semantic level.
_SYNTHETIC_ROOT = 0


def _store(ctx: Any, field: str, value: Any) -> None:
    """Store per-run state on ctx, tolerating contexts that reject attributes."""
    try:
        object.__setattr__(ctx, field, value)
    except (AttributeError, TypeError):
        pass


def _last_record_id(ctx: Any) -> int:
    try:
        return int(getattr(ctx, _LAST_FIELD, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _current_parent(ctx: Any) -> int:
    try:
        return int(getattr(ctx, _PARENT_FIELD, _SYNTHETIC_ROOT) or _SYNTHETIC_ROOT)
    except (TypeError, ValueError):
        return _SYNTHETIC_ROOT


def _level_parents(ctx: Any) -> dict[int, int]:
    value = getattr(ctx, _LEVELS_FIELD, None)
    if isinstance(value, dict) and value:
        return {int(k): int(v) for k, v in value.items()}
    return {_SYNTHETIC_ROOT: _SYNTHETIC_ROOT}


def _largest_parent_below(levels: dict[int, int], target: int) -> int:
    """Return the record id at the largest active level strictly below target.

    Falls back to the synthetic root when no lower active level exists — a direct
    app or workflow run remains valid even with lower semantic levels absent.
    """
    lower = [level for level in levels if level < target]
    if not lower:
        return _SYNTHETIC_ROOT
    return levels[max(lower)]


def _clear_deeper_than(levels: dict[int, int], level: int) -> dict[int, int]:
    """Remove all stored levels deeper than ``level`` (prevents stale children)."""
    return {k: v for k, v in levels.items() if k <= level}


# -- nest-level transition consumers ------------------------------------------

def on_level_set(ctx: Any, new_level: int, prior_level: int) -> None:
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
    if new_level > prior_level:
        last = _last_record_id(ctx)
        levels = dict(_level_parents(ctx))
        levels[prior_level] = last
        levels = _clear_deeper_than(levels, new_level)
        _store(ctx, _LEVELS_FIELD, levels)
        _store(ctx, _PARENT_FIELD, last)
        return
    levels = _clear_deeper_than(_level_parents(ctx), new_level)
    _store(ctx, _LEVELS_FIELD, levels)
    _store(ctx, _PARENT_FIELD, _largest_parent_below(levels, new_level))


def on_level_next(ctx: Any, current_level: int) -> None:
    """Consume a nest increase: the last written record parents the deeper level.

    The most recently successfully written record (``last_record_id``) becomes the
    active parent for the current level and the parent for records subsequently
    written one level deeper.
    """
    last = _last_record_id(ctx)
    levels = dict(_level_parents(ctx))
    levels[current_level] = last
    levels = _clear_deeper_than(levels, current_level + 1)
    _store(ctx, _LEVELS_FIELD, levels)
    _store(ctx, _PARENT_FIELD, last)


def on_level_previous(ctx: Any, new_level: int) -> None:
    """Consume a nest decrease: return to an existing higher parent context.

    A decrease never uses the newest deeper record as a parent. Deeper levels are
    cleared and the parent is restored from the largest active level strictly
    below the new level.
    """
    levels = _clear_deeper_than(_level_parents(ctx), new_level)
    _store(ctx, _LEVELS_FIELD, levels)
    _store(ctx, _PARENT_FIELD, _largest_parent_below(levels, new_level))


# -- record writer hooks ------------------------------------------------------

def stamp_record(ctx: Any, record: dict[str, Any], nest_level: int) -> int:
    """Stamp record_id, parent_record_id, and nest_level; return the record_id.

    record_id = last_record_id + 1. The record is stamped before the append; the
    id is committed only after a successful append (see ``commit_record``), so a
    failed write does not advance the sequence.
    """
    record_id = _last_record_id(ctx) + 1
    record["record_id"] = record_id
    record["parent_record_id"] = _current_parent(ctx)
    record["nest_level"] = int(nest_level)
    return record_id


def commit_record(ctx: Any, record_id: int) -> None:
    """Advance last_record_id after a successful append (REQ-007)."""
    _store(ctx, _LAST_FIELD, int(record_id))
