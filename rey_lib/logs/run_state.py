"""Authoritative per-run hierarchy state, persisted in a companion file
(SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction).

The complete logical hierarchy state for one physical run log lives in one
companion JSON file derived deterministically from that run log's path. Every
process that writes to the same run log — the pipeline and every app subprocess
it invokes — resolves the same companion path from the inherited ``run_log_path``
and therefore shares one authoritative state. This is what keeps ``record_id``
sequencing and parent linkage continuous across the subprocess boundary that
process-local ctx attributes could not cross.

No authoritative hierarchy state is kept on ctx: ctx carries only ``run_log_path``,
from which the companion path is derived. When no durable run log can be resolved
(for example a bare test context), an in-memory dict on ctx is the sole store for
that context — there is never a file plus a second authoritative ctx copy.

Companion file naming (deterministic, owned here): ``<run_log_path>.hstate.json``.

State shape (``level_anchors`` keys are integers in memory, serialized as strings):

    last_record_id            global record sequence only; never determines parentage
    current_nest_level        active semantic/relative nest level (starts 0)
    parent_level              semantic base scope; established only by set_nest_level
    minimum_nest_level        relative nesting floor; always parent_level + 1
    current_parent_record_id  parent id stamped at the active level (starts 0)
    level_anchors             {level: first record committed at that level}; seeded {0: 0}

Persistence goes through the shared low-level primitive file layer already used by
the run-log writer; this module adds no parallel I/O layer. Concurrency is out of
scope: the model assumes sequential writers to one physical run log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "LAST_RECORD_ID",
    "CURRENT_NEST_LEVEL",
    "PARENT_LEVEL",
    "MINIMUM_NEST_LEVEL",
    "CURRENT_PARENT_RECORD_ID",
    "LEVEL_ANCHORS",
    "companion_path",
    "initial_state",
    "load",
    "save",
]

# State-file naming convention (deterministic; report in completion notes).
_STATE_SUFFIX = ".hstate.json"

# Synthetic root: the parent of records with no active lower semantic level.
_SYNTHETIC_ROOT = 0

# In-memory fallback field, used only when no durable run log resolves; it is the
# sole store in that case, never a cache beside a file.
_MEM_FIELD = "_rey_run_state"

LAST_RECORD_ID = "last_record_id"
CURRENT_NEST_LEVEL = "current_nest_level"
PARENT_LEVEL = "parent_level"
MINIMUM_NEST_LEVEL = "minimum_nest_level"
CURRENT_PARENT_RECORD_ID = "current_parent_record_id"
LEVEL_ANCHORS = "level_anchors"


def initial_state() -> dict[str, Any]:
    """Return a fresh hierarchy state with the documented initial values."""
    return {
        LAST_RECORD_ID: 0,
        CURRENT_NEST_LEVEL: 0,
        PARENT_LEVEL: 0,
        MINIMUM_NEST_LEVEL: 1,
        CURRENT_PARENT_RECORD_ID: _SYNTHETIC_ROOT,
        LEVEL_ANCHORS: {_SYNTHETIC_ROOT: _SYNTHETIC_ROOT},
    }


def companion_path(run_log_path: str) -> Path:
    """Return the deterministic companion hierarchy-state path for a run log."""
    return Path(str(run_log_path) + _STATE_SUFFIX)


def load(ctx: Any) -> tuple[dict[str, Any], Path | None]:
    """Return the authoritative hierarchy state and the path backing it.

    File-backed (create-if-absent) when a durable run log resolves; otherwise an
    in-memory dict on ctx, with a ``None`` path. An existing state file is never
    overwritten during resolution — a subprocess app finds the pipeline's file and
    continues it rather than reinitializing.
    """
    path = _resolve_path(ctx)
    if path is None:
        state = getattr(ctx, _MEM_FIELD, None)
        if not isinstance(state, dict):
            state = initial_state()
            _store_mem(ctx, state)
        return state, None
    if path.exists():
        return _read(path), path
    state = initial_state()
    _write(path, state)  # create-if-absent initialization only
    return state, path


def save(ctx: Any, state: dict[str, Any], path: Path | None) -> None:
    """Persist the updated hierarchy state to its backing store."""
    if path is None:
        _store_mem(ctx, state)
    else:
        _write(path, state)


# -- path resolution ----------------------------------------------------------

def _resolve_path(ctx: Any) -> Path | None:
    """Resolve the companion state path, establishing the run-log path if needed.

    Uses the authoritative ``open_run_log`` so a creator resolves and caches its
    ``run_log_path`` while an inheritor (subprocess app, whose ``run_log_path`` is
    already set) returns the same path. Returns None when no durable run log can be
    resolved, so the caller falls back to the in-memory store.
    """
    try:
        from rey_lib.logs.record_enrichment import open_run_log

        run_log_path = open_run_log(ctx)
    except Exception:  # noqa: BLE001 — no durable run log; use in-memory fallback.
        run_log_path = getattr(ctx, "run_log_path", None)
    if not run_log_path:
        return None
    return companion_path(str(run_log_path))


# -- persistence --------------------------------------------------------------

def _read(path: Path) -> dict[str, Any]:
    """Read and normalize the state file, tolerating a malformed/partial file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return initial_state()
    if not isinstance(raw, dict):
        return initial_state()
    return _normalize(raw)


def _write(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist state through the shared primitive file layer."""
    from rey_lib.files import primitive_file_io

    primitive_file_io.atomic_write_text(path, json.dumps(_serializable(state)))


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a loaded state dict to the in-memory shape (int level_anchors keys)."""
    anchors_raw = raw.get(LEVEL_ANCHORS) or {}
    try:
        anchors = {int(k): int(v) for k, v in anchors_raw.items()}
    except (TypeError, ValueError):
        anchors = {}
    if not anchors:
        anchors = {_SYNTHETIC_ROOT: _SYNTHETIC_ROOT}
    parent_level = _as_int(raw.get(PARENT_LEVEL), 0)
    return {
        LAST_RECORD_ID: _as_int(raw.get(LAST_RECORD_ID), 0),
        CURRENT_NEST_LEVEL: _as_int(raw.get(CURRENT_NEST_LEVEL), 0),
        PARENT_LEVEL: parent_level,
        # The floor is derived, so a state file predating it still normalizes correctly.
        MINIMUM_NEST_LEVEL: _as_int(raw.get(MINIMUM_NEST_LEVEL), parent_level + 1),
        CURRENT_PARENT_RECORD_ID: _as_int(raw.get(CURRENT_PARENT_RECORD_ID), _SYNTHETIC_ROOT),
        LEVEL_ANCHORS: anchors,
    }


def _serializable(state: dict[str, Any]) -> dict[str, Any]:
    """Render state for JSON: level_anchors keys become strings."""
    anchors = state.get(LEVEL_ANCHORS) or {}
    parent_level = _as_int(state.get(PARENT_LEVEL), 0)
    return {
        LAST_RECORD_ID: _as_int(state.get(LAST_RECORD_ID), 0),
        CURRENT_NEST_LEVEL: _as_int(state.get(CURRENT_NEST_LEVEL), 0),
        PARENT_LEVEL: parent_level,
        MINIMUM_NEST_LEVEL: _as_int(state.get(MINIMUM_NEST_LEVEL), parent_level + 1),
        CURRENT_PARENT_RECORD_ID: _as_int(state.get(CURRENT_PARENT_RECORD_ID), _SYNTHETIC_ROOT),
        LEVEL_ANCHORS: {str(int(k)): int(v) for k, v in anchors.items()},
    }


def _as_int(value: Any, default: int) -> int:
    """Best-effort int coercion with a default (state must never raise on read)."""
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _store_mem(ctx: Any, state: dict[str, Any]) -> None:
    """Store the in-memory fallback state, tolerating contexts that reject attrs."""
    try:
        object.__setattr__(ctx, _MEM_FIELD, state)
    except (AttributeError, TypeError):
        pass
