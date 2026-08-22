"""Nesting API, delegating to the run log that owns the state.

The level, its minimum, the parent anchors and the sequence all live on
``RunLog`` now. These remain as the names callers already use; each hands the
call to the owner.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "get_nest_level",
    "next_nest_level",
    "previous_nest_level",
    "set_nest_level",
]


def set_nest_level(run_log: Any, semantic: str) -> int:
    """Establish the semantic base level for the scope now executing."""
    return run_log.set_nest_level(semantic)


def get_nest_level(run_log: Any) -> int:
    """Return the level records are currently written at."""
    return run_log.nest_level()


def next_nest_level(run_log: Any) -> int:
    """Descend one relative level within the current semantic scope."""
    return run_log.enter()


def previous_nest_level(run_log: Any) -> int:
    """Ascend one relative level, never above the established minimum."""
    return run_log.exit()
