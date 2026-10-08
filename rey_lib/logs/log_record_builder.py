"""The structured-record builder: one construction for every logged record.

``LogRecordBuilder`` builds the common run envelope once, for both kinds of
record the logging writer receives:

* a typed run record -- RUN_START, STEP_*, RUN_COMPLETE, file and evidence
  records -- whose type and fields ``RunLog`` decides, and
* an application log event, from a stdlib ``LogRecord``.

The envelope is the run's identity and context: ``run_id``, ``run_timestamp``,
the app, workflow and pipeline, lineage, the bound step and correlation,
``nest_level`` and ``parent_run_log_id``. Where a run log is bound, its run
context is used; where none is, the builder's own run identity is used at
``nest_level`` 0 with no parent.

An application event is built into two views of the one record. The JSONL line
is the full structured record: the envelope, the ``record_type`` its level maps
to (ERROR, WARNING, otherwise INFO), the actual ``level``, ``sequence``,
``parent_sequence``, ``depth`` (from ``record.log_depth``), the caller's extras
and context fields. The database row keeps today's content -- the envelope,
message, logger, the ERROR payload and the shared sensitivity column -- and not
the whole structured envelope (row logging.call_depth_never_reaches_the_run_log
holds the schema question).

The builder chooses no output. ``LogWriter`` does.

Backlog: logging.app_logger.s8_json_output_cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = ["LogRecordBuilder"]

# LogRecord attributes that are always present -- excluded from per-event extras
# so envelope fields are not duplicated and internal logging noise is not emitted.
_STANDARD_ATTRS: frozenset[str] = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "taskName",
    # Stamped by the configured pipeline and written as the record's depth.
    "log_depth",
})

# Extra keys considered SQL diagnostic data -- excluded when dump_sql is false.
_SQL_EXTRA_KEYS: frozenset[str] = frozenset({
    "sql", "proc", "query", "params", "inputs", "sql_type",
})

#: Run-context keys a run supplies to the envelope, beyond identity and nesting.
_RUN_CONTEXT_KEYS: tuple[str, ...] = (
    "app", "workflow_name", "pipeline_name",
    # Correlation only: the governed step the record was written inside.
    "batch_step_id",
)


def _record_type(levelno: int) -> str:
    """The run-record type an application event at ``levelno`` is written as."""
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    return "INFO"


class LogRecordBuilder:
    """Builds every structured record the logging writer receives.

    Args:
        run_id: The run's identity, established at the launch boundary.
        run_timestamp: The run's filing timestamp.
        app: The owning application, for events written with no run bound.
        context: Static fields stamped on every application-event JSONL line
            (for example ``run_started_at``).
        ctx: The application context, read at build time for the configured
            context fields and diagnostics.
        ctx_fields: Names of ``ctx`` attributes to snapshot onto each
            application-event JSONL line when no diagnostics config names them.
    """

    def __init__(
        self,
        *,
        run_id: Any,
        run_timestamp: str,
        app: str = "",
        context: Optional[dict[str, Any]] = None,
        ctx: Any = None,
        ctx_fields: tuple[str, ...] = (),
    ) -> None:
        self._run_id = run_id
        self._run_timestamp = str(run_timestamp)
        self._app = str(app or "")
        self._context = dict(context or {})
        self._ctx = ctx
        self._ctx_fields = tuple(ctx_fields)
        self._sequence = 0
        # Maps depth level -> sequence number of the last event at that depth.
        # Used to resolve parent_sequence without explicit span calls.
        self._depth_seq: dict[int, int] = {}

    # -- the run envelope -----------------------------------------------------

    def run_fields(self) -> dict[str, Any]:
        """The run context of an event written while no run log is bound."""
        fields: dict[str, Any] = {
            "run_id": self._run_id,
            "run_timestamp": self._run_timestamp,
            "nest_level": 0,
            "parent_run_log_id": None,
        }
        if self._app:
            fields["app"] = self._app
        return fields

    def run_record(
        self,
        record_type: str,
        message: str,
        fields: Optional[dict[str, Any]],
        *,
        run: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one record: the run envelope, then the caller's fields.

        Args:
            record_type: The record's type.
            message: The record's message; omitted when empty.
            fields: The record's own fields. They may override an ambient step
                or correlation field.
            run: The run context -- ``run_id``, ``run_timestamp``, optional
                ``app`` / ``workflow_name`` / ``pipeline_name``, ``lineage``,
                ``nest_level`` and ``parent_run_log_id``.

        Returns:
            The sanitized, validated record.
        """
        from rey_lib.logs.record_enrichment import (
            FILES_RECORD_SUBGROUP, _context_fields, _record_group,
            _RUN_RECORD_SCHEMA_VERSION, sanitize_log_value,
        )
        from rey_lib.logs.record_validation import _validate_run_record

        record: dict[str, Any] = {
            "record_type": record_type,
            "record_group": _record_group(record_type),
            "run_id": run.get("run_id"),
            "run_timestamp": str(run.get("run_timestamp") or ""),
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
        for key in _RUN_CONTEXT_KEYS:
            if run.get(key):
                record[key] = run[key]
        for key, value in (run.get("lineage") or {}).items():
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
        record["nest_level"] = run.get("nest_level", 0)
        record["parent_run_log_id"] = run.get("parent_run_log_id")
        return record

    # -- application log events -----------------------------------------------

    def log_record(
        self,
        record: logging.LogRecord,
        *,
        run: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build an application log event once, as its JSONL and database views.

        Args:
            record: The stdlib record.
            run: The bound run log's run context, or None when no run is bound.

        Returns:
            ``(record, database_record)``: the full structured record, which is
            the JSONL line, and the row the database keeps for it.
        """
        from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD

        run = run if run is not None else self.run_fields()
        record_type = _record_type(record.levelno)
        message = record.getMessage()
        exception = None
        if record.exc_info and record.exc_info[0] is not None:
            exception = logging.Formatter().formatException(record.exc_info)

        level_key = record.levelname.lower()
        diag_level = self._diagnostics_level(level_key)
        dump_stack_trace = (
            getattr(diag_level, "dump_stack_trace", True) if diag_level is not None else True
        )

        fields: dict[str, Any]
        if record_type == "ERROR":
            from rey_lib.errors.error_utils import build_error_record_payload

            error_fields: dict[str, Any] = {"logger": record.name}
            if record.exc_info and record.exc_info[0] is not None:
                error_fields["error_type"] = record.exc_info[0].__name__
            if dump_stack_trace and exception is not None:
                error_fields["sanitized_exception"] = exception
            built = build_error_record_payload(message=message, **error_fields)
            message = built["message"]
            fields = {"error_message": built["error_message"]}
        else:
            fields = {"logger": record.name}
        sensitive = getattr(record, SENSITIVE_FIELD, None)
        if sensitive is not None:
            fields[SENSITIVE_FIELD] = sensitive
        database_record = self.run_record(record_type, message, fields, run=run)

        full = dict(database_record)
        self._sequence += 1
        depth = max(0, getattr(record, "log_depth", 0))
        parent_sequence = self._resolve_parent(depth)
        self._depth_seq[depth] = self._sequence
        self._prune_depth_stack(depth)
        full["sequence"] = self._sequence
        full["parent_sequence"] = parent_sequence
        full["depth"] = depth
        full["timestamp"] = getattr(record, "timestamp", None) or datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
        full["level"] = record.levelname
        full["source"] = record.name
        for key, value in self._context.items():
            full.setdefault(key, value)
        self._snapshot_ctx(full, diag_level)

        # Per-event extras -- caller-supplied extra={} fields. SQL-related keys
        # are suppressed when dump_sql is explicitly false.
        dump_sql = getattr(diag_level, "dump_sql", True) if diag_level is not None else True
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if not dump_sql and key in _SQL_EXTRA_KEYS:
                continue
            full.setdefault(key, value)
        if dump_stack_trace and exception is not None:
            full["exception"] = exception
        return full, database_record

    def _resolve_parent(self, depth: int) -> Optional[int]:
        """Return the sequence number of the nearest ancestor event."""
        for level in range(depth - 1, -1, -1):
            if level in self._depth_seq:
                return self._depth_seq[level]
        return None

    def _prune_depth_stack(self, depth: int) -> None:
        """Remove tracking entries for depths deeper than current."""
        for level in list(self._depth_seq):
            if level > depth:
                del self._depth_seq[level]

    def _walk_ctx_path(self, path: str) -> Any:
        """Walk a dot-separated attribute path on ctx. None if any segment is missing."""
        obj = self._ctx
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj

    def _diagnostics_level(self, level_key: str) -> Any:
        """Return the diagnostics Namespace for ``level_key``, or None.

        Reads ``ctx.diagnostics.{level_key}`` at build time so env-override
        changes take effect without restarting.
        """
        if self._ctx is None:
            return None
        diag = getattr(self._ctx, "diagnostics", None)
        if diag is None:
            return None
        return getattr(diag, level_key, None)

    def _snapshot_ctx(self, out: dict[str, Any], diag_level: Any) -> None:
        """Add the configured ctx fields to an application-event line.

        With diagnostics config present they nest under ``ctx_dump`` when
        ``dump_ctx`` is set; without it the constructor's ``ctx_fields`` are
        written flat.
        """
        if self._ctx is None:
            return
        ctx_fields = self._ctx_fields
        if diag_level is not None:
            configured = getattr(diag_level, "ctx_fields", None)
            if configured is not None:
                ctx_fields = tuple(configured)
        if not ctx_fields:
            return
        if diag_level is not None and getattr(diag_level, "dump_ctx", False):
            ctx_dump = {
                field: self._walk_ctx_path(field)
                for field in ctx_fields
                if self._walk_ctx_path(field) is not None
            }
            if ctx_dump:
                out["ctx_dump"] = ctx_dump
        elif diag_level is None:
            for field in ctx_fields:
                out[field] = self._walk_ctx_path(field)
