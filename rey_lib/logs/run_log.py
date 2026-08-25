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

from contextlib import contextmanager

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["RunLog"]

_MIN_LEVEL = 0

#: Semantic nesting bases, unchanged from nest_level.
SEMANTIC_BASES: dict[str, int] = {
    "pipeline": 1,
    "pipeline_step": 2,
    "app": 3,
    "workflow": 4,
    "workflow_step": 5,
}

#: What the writer stamps on every record, and therefore what has a column.
#:
#: Read against ``_record`` and ``append``: anything here is passed to the
#: control writer by name, and everything else in a record is what the caller
#: supplied for that record type. ``timestamp`` is included because the database
#: stamps ``recorded_at`` itself and will not take one.
_ENVELOPE_FIELDS: frozenset[str] = frozenset({
    "record_type", "record_group", "record_subgroup", "message",
    "run_id", "run_timestamp", "timestamp", "record_schema_version",
    "app", "workflow_name", "pipeline_name",
    "step_id", "step_name", "step_sequence", "correlation_id",
    "parent_run_id", "subject_type", "subject_id", "subject_name",
    "pipeline_run_id", "workflow_run_id", "pipeline_id", "workflow_id",
    "run_log_id", "parent_run_log_id", "nest_level",
})

#: Facts any record type may state, each with a column of its own.
#:
#: These are not stamped on every record -- a record states them when it has
#: them -- but they are shared and queried across record types, so they are
#: columns rather than something buried in a per-type payload.
_SHARED_FIELDS: frozenset[str] = frozenset({
    "status", "path", "file_id", "source_name", "checksum_sha256",
    "size_bytes", "exists", "modified_at",
    # The failure object. A failure is the one thing a reader queries for.
    "error_message",
})

#: record type -> the one jsonb column that may carry its structure.
#:
#: Every record type this estate writes appears here except ERROR and
#: STEP_FAILURE, whose whole payload is the ``error_message`` object. A record
#: type missing from this map cannot be persisted, which is deliberate: the
#: alternative is a generic bucket, and a generic bucket is what this replaced.
#:
#: Generated from the same scan that generated the table, so the schema and
#: this map describe one vocabulary.
#:
#: Public because a reader needs the same answer as the writer: which column
#: holds this record type's payload. A reader that kept its own copy would be a
#: second vocabulary, and the two would drift the first time a record type was
#: added here alone.
TYPE_PAYLOAD_COLUMNS: dict[str, str] = {
    "APP_EXECUTION": "app_execution",
    "ARTIFACT_REFERENCE": "artifact_reference",
    "CONFIG_FILE_MANIFEST": "config_file_manifest",
    "CONFIG_FILE_REFERENCE": "config_file_reference",
    "EXECUTION_PLAN": "execution_plan",
    "FILE_OPERATION": "file_operation",
    "INFO": "info",
    "INPUT_DISCOVERED": "input_discovered",
    "INPUT_FILE_REFERENCE": "input_file_reference",
    "LLM_ANALYSIS_FAILURE": "llm_analysis_failure",
    "LLM_ANALYSIS_PACKAGE": "llm_analysis_package",
    "LLM_ANALYSIS_RESULT": "llm_analysis_result",
    "LLM_CONTEXT": "llm_context",
    "LLM_CONTRACT": "llm_contract",
    "LLM_EVALUATION_PAYLOAD": "llm_evaluation_payload",
    "LLM_EVALUATION_RUN": "llm_evaluation_run",
    "LLM_INTERPRETATION": "llm_interpretation",
    "LLM_PACKAGE": "llm_package",
    "RESULTS_SUMMARY": "results_summary",
    "ROW_COUNT": "row_count",
    "RUN_COMPLETE": "run_complete",
    "RUN_START": "run_start",
    "RUN_SUMMARY": "run_summary",
    "SOURCE_FILE_CLASSIFICATION": "source_file_classification",
    "SOURCE_FILE_CLASSIFICATION_DELETION": "source_file_classification_deletion",
    "SOURCE_FILE_INVENTORY": "source_file_inventory",
    "SOURCE_FILE_MUTATION": "source_file_mutation",
    "SOURCE_FILE_PROFILE": "source_file_profile",
    "SOURCE_FILE_ROLLBACK": "source_file_rollback",
    "SQL_EXECUTION": "sql_execution",
    "STEP_END": "step_end",
    "STEP_START": "step_start",
    "VALIDATION_RESULT": "validation_result",
    "WARNING": "warning",
}

#: The canonical run lineage every durable record carries.
LINEAGE_FIELDS: tuple[str, ...] = (
    "parent_run_id", "subject_type", "subject_id", "subject_name",
)

#: Domain metadata, classified separately from lineage.
DOMAIN_FIELDS: tuple[str, ...] = (
    "pipeline_run_id", "workflow_run_id", "pipeline_id", "workflow_id",
)


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
        new_batch: bool = True,
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
        # Held as given. It is the manifest's integer key, and a string copy
        # of it would be a second representation of the one value -- the
        # records would then disagree with the context they came from.
        self.run_id = run_id
        self.run_timestamp = str(run_timestamp)
        self.destination = str(destination or "jsonl").strip().lower()
        # Launch states whether this execution starts a batch or continues one.
        # Held here and never inferred: deciding from whether batch_id happens
        # to be set would make a leftover value silently mean "reuse", which is
        # how an execution joins a batch it has nothing to do with.
        self.new_batch = bool(new_batch)
        self.control = control
        if control is not None:
            # Control is subordinate: it persists on this run log's behalf, so
            # it is told which run log it serves rather than finding one.
            control.run_log = self
        # True while this run log is writing a record to a destination. The DB
        # sink runs SQL, and SQL execution is itself a logged event, so without
        # this a persisted record would persist the record of its own write.
        self._persisting = False

        self._log_dir = log_dir
        self._path = path
        self._workflow = workflow
        self._pipeline = pipeline
        self._step_name: Optional[str] = None
        self._pipeline_step_name: Optional[str] = None
        self._lineage: dict[str, str] = dict(lineage or {})
        # The nesting state, owned here for the life of the object.
        #
        # It was a JSON file beside the log, because the identities in it were
        # file-local and had to survive a process. They are database keys now:
        # control.run_log owns identity and parentage durably, and this is only
        # the cursor into it while the run is executing.
        self._current_nest_level = 0
        self._parent_level = 0
        self._minimum_nest_level = 1
        #: The row every record written now is a child of. None at the root.
        self._current_parent_run_log_id: Optional[int] = None
        #: nest level -> the run_log_id of the record that opened that level.
        self._level_anchors: dict[int, Optional[int]] = {}
        #: Whether any record has been written, for the rebind guard alone.
        self._has_records = False
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
        if self._path and str(self._path) != resolved and self._has_records:
            from rey_lib.errors.error_utils import StateError

            raise StateError(
                f"Cannot rebind the run log to {resolved!r}: records are "
                f"already written to {self._path!r}. One run is one log."
            )
        self._path = resolved

    def set_nest_level(self, semantic: str) -> int:
        """Establish a semantic base, or take a relative step.

        Ported unchanged from ``nest_level.set_nest_level``: a set always starts
        a new named scope, clearing the anchor at that level and below, so the
        first record written afterwards anchors it.
        """
        if semantic == "next":
            level = max(self._minimum_nest_level, _MIN_LEVEL)
            self._on_level_next(level)
            self._current_nest_level = level
            return level
        if semantic == "sibling":
            level = max(self._current_nest_level, _MIN_LEVEL)
            self._on_level_next(level)
            self._current_nest_level = level
            return level
        if semantic not in SEMANTIC_BASES:
            raise ValueError(
                f"Unknown semantic nest level: {semantic!r}. "
                f"Known bases: {sorted(SEMANTIC_BASES)}; relative operations: "
                "'next', 'sibling'."
            )
        level = SEMANTIC_BASES[semantic]
        self._on_level_set(level)
        self._current_nest_level = level
        return level

    def nest_level(self) -> int:
        """The nesting level records are currently written at."""
        return self._current_nest_level

    def enter(self) -> int:
        """Descend the relative child hierarchy beneath the established base.

        The first descent lands on ``minimum_nest_level`` (base + 1), so a
        descent from the base cannot land on the base itself.
        """
        current = self._current_nest_level
        level = max(current + 1, self._minimum_nest_level)
        self._on_level_next(level)
        self._current_nest_level = level
        return level

    def exit(self) -> int:
        """Return upward within the relative hierarchy owned by the current base.

        Never rises above ``minimum_nest_level`` and never moves deeper, so
        calling it on the base leaves the level unchanged.
        """
        current = self._current_nest_level
        floor = max(self._minimum_nest_level, _MIN_LEVEL)
        level = min(current, max(current - 1, floor))
        self._on_level_previous(level)
        self._current_nest_level = level
        return level

    # -- anchors: ported from record_parenting ------------------------------

    @staticmethod
    def _largest_anchor_below(anchors: dict, target: int) -> Optional[int]:
        """The anchor at the largest anchored level strictly below ``target``.

        None when nothing is anchored above it: the record is a root, and its
        ``parent_run_log_id`` is NULL. There is no synthetic root row to point
        at, because a parent is now an actual row.
        """
        lower = [level for level in anchors if int(level) < target]
        if not lower:
            return None
        return anchors[max(lower, key=int)]

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

    def _on_level_set(self, new_level: int) -> None:
        """A semantic base set starts a new named scope at ``new_level``."""
        self._clear_from(self._level_anchors, new_level)
        self._parent_level = new_level
        self._minimum_nest_level = new_level + 1
        self._current_parent_run_log_id = self._largest_anchor_below(
            self._level_anchors, new_level)

    def _on_level_next(self, new_level: int) -> None:
        """A descent enters an unnamed relative child level fresh."""
        self._clear_from(self._level_anchors, new_level)
        self._current_parent_run_log_id = self._largest_anchor_below(
            self._level_anchors, new_level)

    def _on_level_previous(self, new_level: int) -> None:
        """A return keeps the anchor at ``new_level`` and clears deeper ones."""
        self._clear_deeper_than(self._level_anchors, new_level)
        self._current_parent_run_log_id = self._largest_anchor_below(
            self._level_anchors, new_level)

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
            # A record that states its own grouping value is believed over the
            # per-type default. Every ARTIFACT_REFERENCE was stamped
            # "artifacts" because the type map was asked and the record was
            # not, which collapsed output_files, profiles, analysis_context
            # and analysis_results into one indistinguishable group. The
            # override is scoped to types the map already places in `files`,
            # so a subgroup exists exactly where the group is `files`.
            supplied = (fields or {}).get("artifact_group")
            record["record_subgroup"] = str(supplied) if supplied else subgroup
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
        """Append one typed record, and return its ``control.run_log`` id.

        The database mints the identity, so the row is written first and the
        JSONL serializes the key it was given. This is the one API for "what
        run-log record did I just write": what comes back is
        ``control.run_log.run_log_id`` and a governed record may point at it.

        Never raises: logging must not mask application execution. A record
        that could not be committed to every selected destination returns
        ``None``.
        """
        from rey_lib.logs.record_validation import _validate_run_record_fields

        if self._persisting:
            # Raised by this run log's own persistence. Not run evidence; see
            # _persistence.
            return None
        _validate_run_record_fields(record_type, fields)
        try:
            record = self._record(record_type, message, fields)

            nest_level = self._current_nest_level
            record["nest_level"] = nest_level
            record["parent_run_log_id"] = self._current_parent_run_log_id

            # The database first: it mints the identity, and the JSONL is an
            # output format that serializes it rather than a second source of
            # one.
            run_log_id: Optional[int] = None
            if self.writes_db:
                with self._persistence():
                    run_log_id = self._persist_to_control(
                        record_type, message, record)
                record["run_log_id"] = run_log_id

            if self.writes_jsonl:
                from rey_lib.files import primitive_file_io

                primitive_file_io.append_jsonl(self.path(), record)

            self._has_records = True
            if nest_level not in self._level_anchors:
                self._level_anchors[nest_level] = run_log_id
            return run_log_id
        except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
            from rey_lib.logs.logging_setup import get_logger

            get_logger(__name__).warning(
                "run log: could not append %s record: %s", record_type, exc
            )
            return None

    # -- the database destination -------------------------------------------
    #
    # Control is this run log's persistence mechanism, not a second logging
    # owner. Every decision about whether to reach the database, and what to do
    # when reaching it fails, is made here; Control executes the routine and
    # owns only its own DB state -- batch_id, batch_step_id, owns_batch.

    def require_structural_record(self, run_log_id: Optional[int],
                                  record_type: str) -> None:
        """Escalate a lost structural record when every destination is required.

        Under ``both`` a missing record means the run log is half written. The
        record writer has already warned and returned None on its own terms;
        this is the separate durability contract on top of that.

        Structural records only -- run start, step start, step end, run
        complete. Losing one leaves a run log that does not describe a run.
        Evidence records degrade as they always have, because logging must not
        mask execution and an errored row count is not worth failing a run over.

        Under ``jsonl`` nothing is raised: that is the historical behaviour.
        """
        if run_log_id is not None or self.destination != "both":
            return
        from rey_lib.errors.error_utils import StateError

        raise StateError(
            f"run_store is 'both' but the {record_type} record was not committed "
            "to every destination. Both are required; the run log now describes "
            "this run in one place and not the other."
        )

    def open_batch(self, batch_name: str) -> None:
        """Establish the control batch this run belongs to.

        Honours the declared intent and nothing else. ``new_batch`` true starts
        one and records that this execution owns it; false requires an existing
        ``batch_id`` to continue and starts none -- a batch is never
        manufactured to satisfy a reuse request.

        Called before the first record, because every persisted record carries
        ``batch_id`` and the column is NOT NULL.
        """
        if not self.writes_db:
            return
        from rey_lib.errors.error_utils import ConfigError, StateError

        control = self._require_control()
        if self.new_batch:
            with self._persistence():
                control.start_batch(batch_name=batch_name or self.app or "run",
                                    required=True)
            if not control.batch_id:
                raise StateError(
                    "control start_batch returned no batch_id. The run store "
                    "cannot record steps or events without the batch that "
                    "groups them."
                )
            control.owns_batch = True
        else:
            if not control.batch_id:
                raise ConfigError(
                    "newBatch is false but no batch_id is bound to reuse. A "
                    "batch is never manufactured to satisfy a reuse request."
                )
            control.owns_batch = False

    def close_batch(self, status: str, message: str = "") -> None:
        """End the control batch, but only if this run began it.

        A batch may contain several runs. Ending it because one of them
        finished would close it under the others.

        Called after the completion record, so the record lands before the
        batch it belongs to is closed.
        """
        if not self.writes_db:
            return
        control = self._require_control()
        if control.owns_batch:
            with self._persistence():
                control.end_batch(
                    status=status,
                    error_message=None if status == "success" else (message or status),
                    required=True,
                )

    def open_step(self, step_name: str, step_sequence: int,
                  step_type: str = "") -> None:
        """Open a control step for this run."""
        if not self.writes_db:
            return
        with self._persistence():
            self._require_control().start_step(
                step_name=step_name, step_sequence=step_sequence,
                step_type=step_type or None, required=True,
            )

    def close_step(self, status: str, message: str = "") -> None:
        """Close the open control step; later events belong to the run."""
        if not self.writes_db:
            return
        control = self._require_control()
        with self._persistence():
            control.end_step(status=status, message=message or None, required=True)
        control.batch_step_id = None

    @contextmanager
    def _persistence(self) -> Any:
        """Mark the block as this run log writing itself to a destination.

        The database destination executes SQL, and SQL execution is a logged
        event, so a control call made on this run log's behalf produces records
        while it runs. Those are not run evidence -- they describe the run log
        writing, not the application working -- and persisting them would call
        the database to record the call that was recording something, without
        end.

        So a record raised inside this block is not written anywhere. The
        boundary is the run log's own persistence, which is why it is marked
        here rather than guessed at from record types.
        """
        outer = self._persisting
        self._persisting = True
        try:
            yield
        finally:
            self._persisting = outer

    def _require_control(self) -> Any:
        """The Control this run log persists through, or a refusal."""
        if self.control is None:
            from rey_lib.errors.error_utils import StateError

            raise StateError(
                f"run_store is '{self.destination}' but no Control was supplied "
                "to this run log. The database destination has no mechanism."
            )
        return self.control

    def _persist_to_control(self, record_type: str, message: str,
                            record: dict[str, Any]) -> Optional[int]:
        """Persist one record to the control database as a run-log row.

        A record's contents divide three ways, and nothing is written twice:

        * the envelope, stamped by this writer, each with its own column
        * the shared facts -- status, path, the governed file, its size and
          checksum -- which any record type may state and which are columns
          because they are queried across types
        * whatever is left, which is that record type's own structure and goes
          into the one jsonb column ``record_type`` selects

        A record type with nothing left writes no payload at all, and every
        other payload column stays NULL. There is no generic bucket: a key
        arriving for a record type with no payload column is refused rather
        than dropped somewhere unqueryable.

        ``timestamp`` is not sent. The database stamps ``created_ts``.

        Returns the minted ``run_log_id``, which is the row's identity and the
        value a governed record stores.
        """
        if self.control is None:
            raise ValueError(
                "run_store selects the control database but no Control was "
                "supplied to this run log."
            )
        payload = {key: value for key, value in record.items()
                   if key not in _ENVELOPE_FIELDS and key not in _SHARED_FIELDS}
        payload_column = TYPE_PAYLOAD_COLUMNS.get(str(record_type).upper())
        if payload and payload_column is None:
            raise ValueError(
                f"{record_type} records have no typed payload column, but "
                f"{sorted(payload)} was supplied. Either the value is a shared "
                "fact with a column of its own, or this record type needs a "
                "payload column declared in TYPE_PAYLOAD_COLUMNS."
            )
        return self.control.write_run_log_record(
            run_id=record.get("run_id"),
            parent_run_log_id=record.get("parent_run_log_id"),
            nest_level=int(record["nest_level"]),
            record_type=str(record_type),
            record_group=str(record.get("record_group") or ""),
            record_subgroup=record.get("record_subgroup"),
            message=str(message or record.get("message") or "") or None,
            app=record.get("app"),
            workflow_name=record.get("workflow_name"),
            pipeline_name=record.get("pipeline_name"),
            step_id=record.get("step_id"),
            step_name=record.get("step_name"),
            step_sequence=record.get("step_sequence"),
            correlation_id=record.get("correlation_id"),
            parent_run_id=record.get("parent_run_id"),
            subject_type=record.get("subject_type"),
            subject_id=record.get("subject_id"),
            subject_name=record.get("subject_name"),
            pipeline_id=record.get("pipeline_id"),
            workflow_id=record.get("workflow_id"),
            record_schema_version=int(record["record_schema_version"]),
            status=record.get("status"),
            path=record.get("path"),
            file_id=record.get("file_id"),
            source_name=record.get("source_name"),
            checksum_sha256=record.get("checksum_sha256"),
            size_bytes=record.get("size_bytes"),
            exists=record.get("exists"),
            modified_at=record.get("modified_at"),
            error_message=record.get("error_message"),
            payloads={column: (payload or None) if column == payload_column
                      else None
                      for column in TYPE_PAYLOAD_COLUMNS.values()},
            required=True,
        )
