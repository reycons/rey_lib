"""The run log, as its owner.

``RunLog`` owns a run's durable record writing: the identity every record
carries, the mutable execution state that identity changes with, the record
sequence, the destination, and the lifecycle. It takes no application context
and reads none — everything it needs it holds, and everything that changes it
changes through its own methods.

That is the distinction from the three attempts before it. A class constructed
with ``ctx`` and reading fields off it per record is a namespace around
functions: ``record_parenting``, ``nest_level`` and ``run_state`` still worked
afterwards, so no ownership had moved. Here their state is this object's, and
they are deleted.

Mutable, not snapshot
---------------------
``workflow``, ``pipeline``, lineage and the current step are set *during* a run,
not at construction. They are held here and changed through ``bind_workflow``,
``bind_pipeline``, ``bind_step`` and the nesting methods, so there is one
mutation path rather than a context that anything can write to.

Sequencing is unchanged
-----------------------
The record sequence remains backed by the companion state file, read and
written per record. That file is how a pipeline step in a separate process
continues its parent's sequence, which is proven and required. Its allocation
is also unsynchronised, which is proven and *not* fixed here: this migration
transfers ownership, and an atomic-allocation redesign riding along inside it
would be a behaviour change hidden in a refactor. The recorded xfail in
``test_run_log_writer_concurrency`` stays the contract for that defect.
"""

from __future__ import annotations

import json

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["RunLog"]

_SYNTHETIC_ROOT = 0
_MIN_LEVEL = 0

LAST_RECORD_ID = "last_record_id"
CURRENT_NEST_LEVEL = "current_nest_level"
PARENT_LEVEL = "parent_level"
MINIMUM_NEST_LEVEL = "minimum_nest_level"
CURRENT_PARENT_RECORD_ID = "current_parent_record_id"
LEVEL_ANCHORS = "level_anchors"

#: Semantic nesting bases, unchanged from nest_level.
SEMANTIC_BASES: dict[str, int] = {
    "pipeline": 1,
    "pipeline_step": 2,
    "app": 3,
    "workflow": 4,
    "workflow_step": 5,
}

#: The canonical run lineage every durable record carries.
LINEAGE_FIELDS: tuple[str, ...] = (
    "parent_run_id", "subject_type", "subject_id", "subject_name",
)

#: Domain metadata, classified separately from lineage.
DOMAIN_FIELDS: tuple[str, ...] = (
    "pipeline_run_id", "workflow_run_id", "pipeline_id", "workflow_id",
)


def _initial_state() -> dict[str, Any]:
    """A fresh hierarchy state with the documented initial values."""
    return {
        LAST_RECORD_ID: 0,
        CURRENT_NEST_LEVEL: 0,
        PARENT_LEVEL: 0,
        MINIMUM_NEST_LEVEL: 1,
        CURRENT_PARENT_RECORD_ID: _SYNTHETIC_ROOT,
        LEVEL_ANCHORS: {_SYNTHETIC_ROOT: _SYNTHETIC_ROOT},
    }


class RunLog:
    """One run's durable record writer, and the owner of its state."""

    def __init__(
        self,
        *,
        app: str,
        run_id: str,
        run_timestamp: str,
        log_dir: Optional[str] = None,
        path: Optional[str] = None,
        destination: str = "jsonl",
        control: Any = None,
        workflow: Optional[str] = None,
        pipeline: Optional[str] = None,
        lineage: Optional[dict[str, str]] = None,
    ) -> None:
        """Hold everything a record needs. No application context is taken.

        Parameters
        ----------
        app : str
            The owning application, written as the record's ``app``.
        run_id, run_timestamp : str
            Execution identity, established at the launch boundary through
            ``rey_lib.run`` and passed in. Never created here.
        log_dir : str, optional
            Directory the run log is written into. The filename is derived from
            the execution name and ``run_timestamp``.
        path : str, optional
            An already-resolved run-log path. A subprocess step receives its
            parent's path this way and continues the same log.
        destination : str
            ``jsonl``, ``db`` or ``both``.
        control : Any, optional
            The Control object used for database persistence. Referenced, not
            owned: the runtime owns its lifecycle because it also serves
            artifacts, contracts and config snapshots.
        workflow, pipeline : str, optional
            Starting values. Both change during a run through their bind methods.
        lineage : dict, optional
            Starting lineage and domain values, changed through ``bind_lineage``.
        """
        # No manufactured default: a run log with no app stamps no app field.
        self.app = str(app or "")
        self.run_id = str(run_id)
        self.run_timestamp = str(run_timestamp)
        self.destination = str(destination or "jsonl").strip().lower()
        self.control = control

        self._log_dir = log_dir
        self._path = path
        self._workflow = workflow
        self._pipeline = pipeline
        self._step_name: Optional[str] = None
        self._pipeline_step_name: Optional[str] = None
        self._lineage: dict[str, str] = dict(lineage or {})
        self._memory_state: Optional[dict[str, Any]] = None
        self._closed = False

    def __repr__(self) -> str:
        return f"<RunLog {self.app} {self.run_id} {self._path or 'unopened'}>"

    # -- identity and destination -------------------------------------------

    @property
    def writes_jsonl(self) -> bool:
        """Whether the JSONL run log is a selected destination."""
        return self.destination in ("jsonl", "both")

    @property
    def writes_db(self) -> bool:
        """Whether the control database is a selected destination."""
        return self.destination in ("db", "both")

    @property
    def workflow(self) -> Optional[str]:
        """The workflow currently executing, if any."""
        return self._workflow

    @property
    def pipeline(self) -> Optional[str]:
        """The pipeline currently executing, if any."""
        return self._pipeline

    @property
    def lineage(self) -> dict[str, str]:
        """A copy of the current lineage; mutate through bind_lineage."""
        return dict(self._lineage)

    # -- state transitions ---------------------------------------------------

    def bind_workflow(self, name: Optional[str]) -> None:
        """Set the workflow every later record is written under."""
        self._workflow = str(name) if name else None

    def bind_pipeline(self, name: Optional[str]) -> None:
        """Set the pipeline every later record is written under."""
        self._pipeline = str(name) if name else None

    def bind_step(self, step_name: Optional[str] = None,
                  pipeline_step_name: Optional[str] = None) -> None:
        """Set the step context later records carry."""
        if step_name is not None:
            self._step_name = str(step_name) or None
        if pipeline_step_name is not None:
            self._pipeline_step_name = str(pipeline_step_name) or None

    def clear_step(self) -> None:
        """Drop the step context; later records belong to the run again."""
        self._step_name = None
        self._pipeline_step_name = None

    def bind_lineage(self, **values: Any) -> None:
        """Merge lineage and domain values. Absent values are left untouched."""
        for key, value in values.items():
            if value:
                self._lineage[key] = str(value)

    def clear_lineage(self) -> None:
        """Drop the bound lineage."""
        self._lineage.clear()

    def bind_path(self, path: Any) -> None:
        """Name the log this run writes to, before anything is written.

        The launch boundary resolves a path for the process, but a pipeline
        only learns its own log directory once the pipeline is resolved -- after
        launch. Without this the pipeline's typed records and the pipeline's
        logger events end up in two different files, which is the single-file
        authority this owner exists to hold.

        Raises
        ------
        StateError
            When records have already been written. The path is the identity of
            the log; moving it mid-run splits one run across two files.
        """
        resolved = str(path)
        if self._path and str(self._path) != resolved:
            state, _ = self._load_state()
            if int(state[LAST_RECORD_ID]) > 0:
                from rey_lib.errors.error_utils import StateError

                raise StateError(
                    f"Cannot rebind the run log to {resolved!r}: "
                    f"{state[LAST_RECORD_ID]} record(s) are already written to "
                    f"{self._path!r}. One run is one log."
                )
        self._path = resolved

    def set_nest_level(self, semantic: str) -> int:
        """Establish a semantic base, or take a relative step.

        Ported unchanged from ``nest_level.set_nest_level``: a set always starts
        a new named scope, clearing the anchor at that level and below, so the
        first record written afterwards anchors it.
        """
        if semantic == "next":
            state, path = self._load_state()
            level = max(int(state[MINIMUM_NEST_LEVEL]), _MIN_LEVEL)
            self._on_level_next(state, level)
            state[CURRENT_NEST_LEVEL] = level
            self._save_state(state, path)
            return level
        if semantic == "sibling":
            state, path = self._load_state()
            level = max(int(state[CURRENT_NEST_LEVEL]), _MIN_LEVEL)
            self._on_level_next(state, level)
            state[CURRENT_NEST_LEVEL] = level
            self._save_state(state, path)
            return level
        if semantic not in SEMANTIC_BASES:
            raise ValueError(
                f"Unknown semantic nest level: {semantic!r}. "
                f"Known bases: {sorted(SEMANTIC_BASES)}; relative operations: "
                "'next', 'sibling'."
            )
        level = SEMANTIC_BASES[semantic]
        state, path = self._load_state()
        self._on_level_set(state, level)
        state[CURRENT_NEST_LEVEL] = level
        self._save_state(state, path)
        return level

    def nest_level(self) -> int:
        """The nesting level records are currently written at."""
        state, _ = self._load_state()
        return int(state[CURRENT_NEST_LEVEL])

    def enter(self) -> int:
        """Descend the relative child hierarchy beneath the established base.

        The first descent lands on ``minimum_nest_level`` (base + 1), so a
        descent from the base cannot land on the base itself.
        """
        state, path = self._load_state()
        current = int(state[CURRENT_NEST_LEVEL])
        level = max(current + 1, int(state[MINIMUM_NEST_LEVEL]))
        self._on_level_next(state, level)
        state[CURRENT_NEST_LEVEL] = level
        self._save_state(state, path)
        return level

    def exit(self) -> int:
        """Return upward within the relative hierarchy owned by the current base.

        Never rises above ``minimum_nest_level`` and never moves deeper, so
        calling it on the base leaves the level unchanged.
        """
        state, path = self._load_state()
        current = int(state[CURRENT_NEST_LEVEL])
        floor = max(int(state[MINIMUM_NEST_LEVEL]), _MIN_LEVEL)
        level = min(current, max(current - 1, floor))
        self._on_level_previous(state, level)
        state[CURRENT_NEST_LEVEL] = level
        self._save_state(state, path)
        return level

    # -- anchors: ported from record_parenting ------------------------------

    @staticmethod
    def _largest_anchor_below(anchors: dict, target: int) -> int:
        """The anchor at the largest anchored level strictly below ``target``."""
        lower = [level for level in anchors if int(level) < target]
        if not lower:
            return _SYNTHETIC_ROOT
        return int(anchors[max(lower, key=int)])

    @staticmethod
    def _clear_from(anchors: dict, level: int) -> None:
        """Remove the anchor at ``level`` and every deeper one, in place."""
        for key in [k for k in list(anchors) if int(k) >= level]:
            del anchors[key]

    @staticmethod
    def _clear_deeper_than(anchors: dict, level: int) -> None:
        """Remove every anchor deeper than ``level``, keeping ``level`` itself."""
        for key in [k for k in list(anchors) if int(k) > level]:
            del anchors[key]

    def _on_level_set(self, state: dict, new_level: int) -> None:
        """A semantic base set starts a new named scope at ``new_level``."""
        anchors = state[LEVEL_ANCHORS]
        self._clear_from(anchors, new_level)
        state[PARENT_LEVEL] = new_level
        state[MINIMUM_NEST_LEVEL] = new_level + 1
        state[CURRENT_PARENT_RECORD_ID] = self._largest_anchor_below(anchors, new_level)

    def _on_level_next(self, state: dict, new_level: int) -> None:
        """A descent enters an unnamed relative child level fresh."""
        anchors = state[LEVEL_ANCHORS]
        self._clear_from(anchors, new_level)
        state[CURRENT_PARENT_RECORD_ID] = self._largest_anchor_below(anchors, new_level)

    def _on_level_previous(self, state: dict, new_level: int) -> None:
        """A return keeps the anchor at ``new_level`` and clears deeper ones."""
        anchors = state[LEVEL_ANCHORS]
        self._clear_deeper_than(anchors, new_level)
        state[CURRENT_PARENT_RECORD_ID] = self._largest_anchor_below(anchors, new_level)

    # -- the log itself ------------------------------------------------------

    def path(self) -> Path:
        """Resolve the run-log path, once.

        A path supplied at construction is used as given, which is how a
        subprocess step continues its parent's log.
        """
        if self._path:
            return Path(self._path)
        if not self._log_dir:
            raise ValueError(
                "Cannot open run log: no durable log path. A RunLog is built "
                "with either a resolved path or the directory to write into."
            )
        name = self._pipeline or self._workflow or self.app or "app"
        resolved = Path(self._log_dir) / f"{name}.{self.run_timestamp}.jsonl"
        self._path = str(resolved)
        return resolved

    def close(self) -> None:
        """Close the run log. Idempotent; called by runtime collection."""
        self._closed = True

    @property
    def is_closed(self) -> bool:
        """Whether this run log has been collected."""
        return self._closed

    # -- state persistence ---------------------------------------------------

    def _state_path(self) -> Optional[Path]:
        """The companion state file, or None when there is no durable log."""
        try:
            return companion_path(str(self.path()))
        except Exception:  # noqa: BLE001 — no durable log; use the in-memory store.
            return None

    def _load_state(self) -> tuple[dict[str, Any], Optional[Path]]:
        """Read the hierarchy state, file-backed when a durable log resolves.

        Read per operation rather than cached, deliberately: this is what lets a
        separate process continue the sequence rather than restart it.
        """
        path = self._state_path()
        if path is None:
            if self._memory_state is None:
                self._memory_state = _initial_state()
            return self._memory_state, None
        if path.exists():
            return _read(path), path
        state = _initial_state()
        _write(path, state)
        return state, path

    def _save_state(self, state: dict[str, Any], path: Optional[Path]) -> None:
        """Persist the hierarchy state to its backing store."""
        if path is None:
            self._memory_state = state
        else:
            _write(path, state)

    # -- writing -------------------------------------------------------------

    def _record(self, record_type: str, message: str,
                fields: dict[str, Any]) -> dict[str, Any]:
        """Build the enriched record from owned state and the supplied fields."""
        from rey_lib.logs.record_enrichment import (
            FILES_RECORD_SUBGROUP, _context_fields, _record_group,
            _RUN_RECORD_SCHEMA_VERSION, sanitize_log_value,
        )
        from rey_lib.logs.record_validation import _validate_run_record

        record: dict[str, Any] = {
            "record_type": record_type,
            "record_group": _record_group(record_type),
            "run_id": self.run_id,
            "run_timestamp": self.run_timestamp,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_schema_version": _RUN_RECORD_SCHEMA_VERSION,
        }
        subgroup = FILES_RECORD_SUBGROUP.get(record_type)
        if subgroup:
            record["record_subgroup"] = subgroup
        if self.app:
            record["app"] = self.app
        if self._workflow:
            record["workflow_name"] = self._workflow
        if self._pipeline:
            record["pipeline_name"] = self._pipeline
        for key in (*LINEAGE_FIELDS, *DOMAIN_FIELDS):
            value = self._lineage.get(key)
            if value:
                record[key] = value
        if message:
            record["message"] = message
        # Step and correlation are bound ambiently around a block of work rather
        # than passed per record, so they are stamped here and can still be
        # overridden by an explicit field on the call.
        record.update(_context_fields())
        record.update(fields or {})
        record = sanitize_log_value(record)
        _validate_run_record(record)
        return record

    def append(self, record_type: str, *, message: str = "",
               **fields: Any) -> int | None:
        """Append one typed record to every selected destination.

        Never raises: logging must not mask application execution. A record that
        could not be committed to every selected destination returns ``None``,
        and the sequence does not advance.
        """
        from rey_lib.logs.record_validation import _validate_run_record_fields

        _validate_run_record_fields(record_type, fields)
        try:
            record = self._record(record_type, message, fields)

            state, state_path = self._load_state()
            record_id = int(state[LAST_RECORD_ID]) + 1
            nest_level = int(state[CURRENT_NEST_LEVEL])
            record["record_id"] = record_id
            record["parent_record_id"] = int(state[CURRENT_PARENT_RECORD_ID])
            record["nest_level"] = nest_level

            if self.writes_jsonl:
                from rey_lib.files import primitive_file_io

                primitive_file_io.append_jsonl(self.path(), record)

            if self.writes_db:
                self._persist_to_control(record_type, message, record)

            state[LAST_RECORD_ID] = record_id
            anchors = state[LEVEL_ANCHORS]
            if nest_level not in anchors and str(nest_level) not in anchors:
                anchors[nest_level] = record_id
            self._save_state(state, state_path)
            return record_id
        except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
            from rey_lib.logs.logging_setup import get_logger

            get_logger(__name__).warning(
                "run log: could not append %s record: %s", record_type, exc
            )
            return None

    def _persist_to_control(self, record_type: str, message: str,
                            record: dict[str, Any]) -> None:
        """Persist one record to the control database as a log event."""
        from rey_lib.logs.run_store import _severity_of

        if self.control is None:
            raise ValueError(
                "run_store selects the control database but no Control was "
                "supplied to this run log."
            )
        self.control.log_event(
            severity=_severity_of(record_type),
            event_name=str(record_type),
            message=str(message or record.get("message") or record_type),
            event_jsonb=record,
            required=True,
        )


# ---------------------------------------------------------------------------
# The companion state file
# ---------------------------------------------------------------------------
#
# The record sequence, parent linkage and nest level for one physical run log
# live in one JSON file derived deterministically from that log's path. Every
# process writing the same run log resolves the same companion path, which is
# what keeps sequencing continuous across the subprocess boundary a
# process-local attribute could not cross. Concurrency is out of scope: the
# model assumes sequential writers to one physical log.
#
# Naming: ``<run_log_path>.hstate.json``.

_STATE_SUFFIX = ".hstate.json"


def companion_path(run_log_path: str) -> Path:
    """Return the deterministic companion hierarchy-state path for a run log."""
    return Path(str(run_log_path) + _STATE_SUFFIX)

def _read(path: Path) -> dict[str, Any]:
    """Read and normalize the state file, tolerating a malformed/partial file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _initial_state()
    if not isinstance(raw, dict):
        return _initial_state()
    return _normalize(raw)

def _write(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist state through the shared primitive file layer."""
    from rey_lib.files.json import write_json_file

    write_json_file(path, _serializable(state), mode="compact", newline=False)

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
