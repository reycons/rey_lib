"""The run log, as its owner.

``RunLog`` owns a run's typed records: the identity every record carries, the
mutable execution state that identity changes with, which records are
structural, the nesting, and the lifecycle. It takes no application context
and reads none — everything it needs it holds, and everything that changes it
changes through its own methods.

It decides no output. A record it builds goes to the logging writer
(``rey_lib.logs.log_writer.LogWriter``), which writes it to the outputs
``logging.run_store`` selects; the common run envelope is built by
``LogRecordBuilder`` (backlog logging.app_logger.s8_json_output_cleanup).

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

import time

from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
from rey_lib.logs.phase_timeline import PhaseTimeline

from pathlib import Path
from typing import Any, Optional
from rey_lib.errors.error_utils import ConfigError

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
    # The governed step a record was written inside; correlation only.
    "batch_step_id",
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
    # What a record says about its own content. A shared fact rather than a
    # payload key: an access control asks it of every record type at once, and
    # a key buried in whichever jsonb column a type happens to use could not be
    # asked that way.
    SENSITIVE_FIELD,
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
    "RUN_PHASES": "run_phases",
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

#: The record types whose whole payload is the shared ``error_message`` object,
#: and which therefore have no entry in TYPE_PAYLOAD_COLUMNS.
FAILURE_RECORD_TYPES: frozenset[str] = frozenset({"ERROR", "STEP_FAILURE"})


def payload_column(record_type: str) -> str | None:
    """Return the column that holds one record type's payload, for a reader.

    The writer's answer, stated once: a failure record's payload is its
    ``error_message`` object; every other type's is the column
    TYPE_PAYLOAD_COLUMNS names. None for a type that carries no payload.

    Args:
        record_type: The record's type, in any case.

    Returns:
        The column name, or None.
    """
    kind = str(record_type or "").upper()
    if kind in FAILURE_RECORD_TYPES:
        return "error_message"
    return TYPE_PAYLOAD_COLUMNS.get(kind)

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
        phase_started: float | None = None,
        app: str,
        run_id: str,
        run_timestamp: str,
        writer: Any,
        builder: Any,
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
        writer : LogWriter
            The process's logging writer. Every record built here is handed to
            it; it alone selects the outputs.
        builder : LogRecordBuilder
            Builds the common run envelope of each record.
        control : Any, optional
            The Control the run's steps are opened and closed on. Referenced,
            not owned: the runtime owns its lifecycle because it also serves
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
        self.writer = writer
        self._builder = builder
        # Launch states whether this execution starts a batch or continues one.
        # Held here and never inferred: deciding from whether batch_id happens
        # to be set would make a leftover value silently mean "reuse", which is
        # how an execution joins a batch it has nothing to do with.
        self.new_batch = bool(new_batch)
        self.control = control
        if control is not None:
            # Control is subordinate: the SQL it runs on this run's behalf is
            # recorded through this run log, so it is told which run log it
            # serves rather than finding one.
            control.run_log = self

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
        self._closed = False
        #: The run's phases, as a contiguous partition of its elapsed time.
        #:
        #: Built once, here, and never replaced: every participant already
        #: holds this object -- the coordinator that runs the steps, and the
        #: log processing that follows them -- so one run has one timeline for
        #: its whole life. A second, even briefly, would make correctness
        #: depend on nobody marking in the gap between them.
        #:
        #: ``phase_started`` is the runtime boundary, captured before context
        #: composition and carried in. Without it the partition would begin
        #: wherever this object happened to be built, which is after the work
        #: it is meant to account for. The launch path always supplies it; a
        #: caller that does not is measuring from construction and says so by
        #: omission.
        self.phases = PhaseTimeline(
            started=time.monotonic() if phase_started is None else phase_started,
            phase="bootstrap",
        )

    def __repr__(self) -> str:
        return f"<RunLog {self.app} {self.run_id} {self.path() or 'no JSONL'}>"

    # -- identity ------------------------------------------------------------

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
            raise ConfigError(
                f"Unknown semantic nest level: {semantic!r}. "
                f"Known bases: {sorted(SEMANTIC_BASES)}; relative operations: "
                "'next', 'sibling'."
            )
        level = SEMANTIC_BASES[semantic]
        self._on_level_set(level)
        self._current_nest_level = level
        return level

    def enter_phase(self, phase: str) -> None:
        """Close the run's active phase and open ``phase`` at the same instant.

        A method rather than reaching through to the timeline, for the reason
        every other run-log capability is one: callers hold a run log, not its
        internals, and a caller that walked into ``run_log.phases`` would be
        coupled to how this object records time rather than to the fact that it
        does. It also keeps the estate's test stubs working -- they absorb
        method calls, and an attribute holding an object is not one.

        Args:
            phase: The phase being entered.
        """
        self.phases.enter(phase)

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

    def path(self) -> Optional[Path]:
        """The JSONL file the logging writer appends to, or None without JSONL.

        A compatibility surface for the readers that open the file; the path
        is the JSONL writer's.
        """
        return self.writer.json_path()

    def run_fields(self) -> dict[str, Any]:
        """This run's context for a record built now: identity, names, lineage, nesting."""
        fields: dict[str, Any] = {
            "run_id": self.run_id,
            "run_timestamp": self.run_timestamp,
            "lineage": {key: self._lineage.get(key)
                        for key in (*LINEAGE_FIELDS, *DOMAIN_FIELDS)},
            "nest_level": self._current_nest_level,
            "parent_run_log_id": self._current_parent_run_log_id,
            # Correlation only: the governed step open on this run's Control.
            "batch_step_id": getattr(self.control, "batch_step_id", None),
        }
        if self.app:
            fields["app"] = self.app
        if self._workflow:
            fields["workflow_name"] = self._workflow
        if self._pipeline:
            fields["pipeline_name"] = self._pipeline
        return fields

    def recorded(self, nest_level: int, run_log_id: Optional[int]) -> None:
        """Note that a record at ``nest_level`` was written as ``run_log_id``.

        The first record written at a level anchors it: later records one level
        deeper take it as their parent.
        """
        if nest_level not in self._level_anchors:
            self._level_anchors[nest_level] = run_log_id

    def close(self) -> None:
        """Close the run log. Idempotent; called by runtime collection."""
        self._closed = True

    @property
    def is_closed(self) -> bool:
        """Whether this run log has been collected."""
        return self._closed

    # -- writing -------------------------------------------------------------

    def append(self, record_type: str, *, message: str = "",
               **fields: Any) -> int | None:
        """Build one typed record and hand it to the logging writer.

        Returns the ``control.run_log`` id the database minted -- the one API
        for "what run-log record did I just write", which a governed record may
        point at -- or None when the database is not a selected output.

        Never raises: logging must not mask application execution. A record
        that could not be committed to every selected output returns ``None``.
        """
        from rey_lib.logs.record_validation import _validate_run_record_fields

        if self.writer.persisting:
            # Raised by the logging output's own persistence. Not run evidence.
            return None
        _validate_run_record_fields(record_type, fields)
        try:
            run = self.run_fields()
            record = self._builder.run_record(record_type, message, fields, run=run)
            run_log_id = self.writer.write(record)
            self.recorded(run["nest_level"], run_log_id)
            return run_log_id
        except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
            from rey_lib.logs.app_logger import AppLogger

            AppLogger(__name__).warning(
                "run log: could not append %s record: %s", record_type, exc
            )
            return None

    # -- structural records and steps ----------------------------------------

    def require_structural_record(self, run_log_id: Optional[int],
                                  record_type: str) -> None:
        """Require the configured persistence of a structural record.

        Structural records only -- run start, step start, step end, run
        complete. Losing one leaves a run log that does not describe a run.
        Evidence records degrade as they always have, because logging must not
        mask execution and an errored row count is not worth failing a run over.

        This run log decides which records are structural; the logging writer
        decides whether their write succeeded (``LogWriter.require_persisted``).
        """
        self.writer.require_persisted(run_log_id, record_type)

    def open_batch(self, batch_name: str) -> None:
        """No longer starts a batch. Kept so existing callers do not break.

        THE BATCH BELONGS TO THE CONTROL LIFECYCLE. It is started where the
        run-starting Control is created, because every governed routine opens
        a step beneath a parent and needs one to exist before it runs. Deciding
        that here made the batch depend on ``run_store``, which answers a
        different question -- where run-log RECORDS go -- and left four of six
        installations doing database work with no batch at all.

        ``new_batch`` reuse is honoured in the same place now: a Control that
        arrives with a ``batch_id`` already bound continues that batch.
        """
        del batch_name

    def close_batch(self, status: str, message: str = "") -> None:
        """No longer ends the batch. Kept so existing callers do not break.

        Ending it belongs with starting it, in the Control lifecycle, after the
        steps beneath it are closed.
        """
        del status, message

    def open_step(self, step_name: str, step_sequence: int,
                  step_type: str = "") -> None:
        """Forward the step-start event to Control.

        Not gated on the destination. A step is opened because there is a
        Control to open it on, not because this log writes its records to the
        database -- those are different questions, and conflating them is what
        left work unattributed.
        """
        if self.control is None:
            return
        with self.writer.persistence():
            self.control.start_step(
                step_name=step_name, step_sequence=step_sequence,
                step_type=step_type or None, required=True,
            )

    def close_step(self, status: str, message: str = "") -> None:
        """Forward the step-end event to Control; later events belong to the run.

        Gated on having a Control, not on the destination, for the same reason
        as :meth:`open_step`.
        """
        if self.control is None:
            return
        control = self.control
        with self.writer.persistence():
            control.end_step(status=status, message=message or None, required=True)
        # REALIGN TO THE ROOT, do not blank it. batch_root_step_id is the
        # permanent anchor for the life of the batch; between steps, work still
        # has somewhere to hang. Nulling it left governed routines called
        # outside a step with no parent at all, and f_batch_step_begin refuses
        # when it is given neither a batch nor a parent.
        control.batch_step_id = control.batch_root_step_id
