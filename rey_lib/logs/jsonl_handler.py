"""
JSONL logging handler.

A standard ``logging.Handler`` that writes one JSON record per log event to
an append-only JSONL file. Drop it into any Python logging setup via
``logger.addHandler`` or ``logging.getLogger().addHandler`` — no application
code changes are required.

Each record contains:
  - envelope fields  (sequence, parent_sequence, depth, timestamp, level,
                      source, message)
  - session context  (static dict supplied at construction)
  - ctx snapshot     (named attributes read from a ctx object at emit time,
                      reflecting current runtime state)
  - per-event data   (any ``extra`` fields the caller passed to the log call)

Field names in the output can be renamed via ``field_map``.

Depth and parent tracking use ``ctx.log_depth`` — the same counter that
``log_enter`` / ``log_exit`` in ``log_utils`` already manage. No new
instrumentation is required in application code.

Public API
----------
JsonlHandler   logging.Handler subclass. Add to any logger or root logger.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["JsonlHandler"]

# LogRecord attributes that are always present — excluded from per-event extras
# so we don't duplicate envelope fields or emit internal Python logging noise.
_STANDARD_ATTRS: frozenset[str] = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "taskName",
})

# Extra keys considered SQL diagnostic data — excluded when dump_sql is false.
_SQL_EXTRA_KEYS: frozenset[str] = frozenset({
    "sql", "proc", "query", "params", "inputs", "sql_type",
})


class JsonlHandler(logging.Handler):
    """
    Logging handler that writes structured JSONL records to a file.

    Plugs into Python's logging system as a standard handler. Each call to
    any logger whose records propagate to this handler produces one JSON line
    appended to the JSONL file, flushed immediately.

    Depth and parent sequence are derived from ``ctx.log_depth``, which
    ``log_enter`` / ``log_exit`` in ``log_utils`` already maintain. No
    changes to application logging calls are needed.

    Parameters
    ----------
    jsonl_path : Path
        Destination JSONL file. Parent directories are created automatically.
        Opened in append mode — safe to reuse across restarts.
    context : dict[str, Any]
        Static key-value pairs stamped on every record (e.g. ``batch_id``,
        ``env``). Snapshot at construction — values are not re-read.
    ctx : Any, optional
        Application context object. Attributes named in ``ctx_fields`` are
        read at each emit call so the record reflects current runtime state.
    ctx_fields : tuple[str, ...], optional
        Names of ``ctx`` attributes to snapshot into each record.
    field_map : dict[str, str], optional
        Renames fields in the JSONL output. Maps canonical name to output
        name (e.g. ``{"batch_id": "RunID"}``). Applied after all other
        fields are assembled.
    """

    def __init__(
        self,
        jsonl_path: Path,
        context: dict[str, Any],
        ctx: Any = None,
        ctx_fields: tuple[str, ...] = (),
        field_map: Optional[dict[str, str]] = None,
    ) -> None:
        """Open the JSONL file and initialise tracking state."""
        super().__init__()
        self._context    = dict(context)
        self._ctx        = ctx
        self._ctx_fields = ctx_fields
        self._field_map  = field_map or {}
        self._sequence   = 0

        # Maps depth level → sequence number of the last record at that depth.
        # Used to resolve parent_sequence without requiring explicit span calls.
        self._depth_seq: dict[int, int] = {}

        resolved = Path(jsonl_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._fh = resolved.open("a", encoding="utf-8")

    # ------------------------------------------------------------------
    # logging.Handler interface
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """
        Write one JSON record to the JSONL file.

        Reads ``ctx.log_depth`` at call time for depth and parent resolution.
        Any ``extra`` fields the caller passed to the log call are included
        as per-event data. Standard ``LogRecord`` attributes are excluded to
        avoid duplication.

        Parameters
        ----------
        record : logging.LogRecord
            The log record produced by the logging framework.
        """
        try:
            self._sequence += 1
            seq   = self._sequence
            depth = self._current_depth()

            parent_seq = self._resolve_parent(depth)
            self._depth_seq[depth] = seq
            self._prune_depth_stack(depth)

            rec = self._build_record(record, seq, parent_seq, depth)
            self._write(rec)
        except Exception:  # noqa: BLE001 — handler must never raise
            self.handleError(record)

    def close(self) -> None:
        """Flush and close the JSONL file."""
        try:
            self._fh.flush()
            self._fh.close()
        finally:
            super().close()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _current_depth(self) -> int:
        """Return the current call depth from ctx, defaulting to zero."""
        if self._ctx is None:
            return 0
        return max(0, getattr(self._ctx, "log_depth", 0))

    def _resolve_parent(self, depth: int) -> Optional[int]:
        """Return the sequence number of the nearest ancestor record."""
        for d in range(depth - 1, -1, -1):
            if d in self._depth_seq:
                return self._depth_seq[d]
        return None

    def _prune_depth_stack(self, depth: int) -> None:
        """Remove tracking entries for depths deeper than current."""
        for d in list(self._depth_seq):
            if d > depth:
                del self._depth_seq[d]

    def _walk_ctx_path(self, path: str) -> Any:
        """Walk a dot-separated attribute path on ctx. Returns None if any segment is missing."""
        obj = self._ctx
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj

    def _diagnostics_level(self, level_key: str) -> Any:
        """Return the diagnostics Namespace for ``level_key``, or ``None``.

        Reads ``ctx.diagnostics.{level_key}`` at emit time so env-override
        changes take effect without restarting. Returns ``None`` when
        ``ctx.diagnostics`` is absent — all behaviour falls back to defaults.
        """
        if self._ctx is None:
            return None
        diag = getattr(self._ctx, "diagnostics", None)
        if diag is None:
            return None
        return getattr(diag, level_key, None)

    def _resolve_ctx_fields(self, diag_level: Any) -> tuple[str, ...]:
        """Return ctx fields to snapshot.

        When diagnostics config is present, reads ``ctx_fields`` from it.
        Falls back to the constructor-supplied ``ctx_fields`` tuple so
        existing callers that pass fields at construction keep working.
        """
        if diag_level is not None:
            fields = getattr(diag_level, "ctx_fields", None)
            if fields is not None:
                return tuple(fields)
        return self._ctx_fields

    def _build_record(
        self,
        record:     logging.LogRecord,
        seq:        int,
        parent_seq: Optional[int],
        depth:      int,
    ) -> dict[str, Any]:
        """Assemble the full JSON record and apply field renaming."""
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )

        out: dict[str, Any] = {
            "sequence":        seq,
            "parent_sequence": parent_seq,
            "depth":           depth,
            "timestamp":       ts,
            "level":           record.levelname,
            "source":          record.name,
            "message":         record.getMessage(),
        }

        # Session context
        out.update(self._context)

        # Resolve diagnostics config for this severity level.
        level_key  = record.levelname.lower()
        diag_level = self._diagnostics_level(level_key)
        ctx_fields = self._resolve_ctx_fields(diag_level)

        # ctx snapshot — whitelisted fields only.
        # When diagnostics config is present: nest under ctx_dump (structured).
        # When falling back to constructor ctx_fields: write flat (legacy behaviour).
        if self._ctx is not None and ctx_fields:
            if diag_level is not None and getattr(diag_level, "dump_ctx", False):
                ctx_dump = {
                    f: self._walk_ctx_path(f)
                    for f in ctx_fields
                    if self._walk_ctx_path(f) is not None
                }
                if ctx_dump:
                    out["ctx_dump"] = ctx_dump
            elif diag_level is None:
                # Legacy: flat fields from constructor ctx_fields.
                for field in ctx_fields:
                    out[field] = self._walk_ctx_path(field)

        # Per-event extras — caller-supplied extra={} fields.
        # SQL-related keys are suppressed when dump_sql is explicitly false.
        dump_sql = getattr(diag_level, "dump_sql", True) if diag_level is not None else True
        for key, val in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if not dump_sql and key in _SQL_EXTRA_KEYS:
                continue
            out.setdefault(key, val)

        # Exception info — controlled by dump_stack_trace (default true).
        dump_stack_trace = (
            getattr(diag_level, "dump_stack_trace", True)
            if diag_level is not None else True
        )
        if dump_stack_trace and record.exc_info and record.exc_info[0] is not None:
            out["exception"] = logging.Formatter().formatException(record.exc_info)

        # Field renaming
        for canonical, output_name in self._field_map.items():
            if canonical in out:
                out[output_name] = out.pop(canonical)

        return out

    def _write(self, record: dict[str, Any]) -> None:
        """Append one JSON line and flush immediately."""
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
