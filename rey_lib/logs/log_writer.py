"""The logging writer: the one place a built record's outputs are chosen.

A record is built once -- by ``LogRecordBuilder`` for an application log event,
or by ``RunLog`` for a typed run record -- and handed to ``LogWriter``, which
writes it to the outputs ``logging.run_store`` selects:

    db     -> DatabaseLogWriter -> control.run_log
    jsonl  -> JsonLogWriter     -> primitive_file_io.append_jsonl
    both   -> both, the database first

Unset means ``db``. Whether a run happens to be bound decides nothing here:
binding supplies a record's run context, never its outputs. JSONL is an
optional output; nothing writes it unless it is selected, and with it
unselected there is no JSONL path at all.

The database mints ``run_log_id`` when it inserts the row. Under ``both`` the
row is written first and its id is added to the record before the JSONL line is
written, so the line and the row name each other. Under ``jsonl`` there is no
``run_log_id``.

The self-persistence guard lives here too. The database output executes SQL,
and SQL execution is itself a logged event, so a record produced while a record
is being persisted would persist the record of its own write, without end.
Anything produced inside the guard is dropped from every output.

Backlog: logging.app_logger.s8_json_output_cleanup.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = ["DatabaseLogWriter", "JsonLogWriter", "LogWriter", "build_log_writer"]


class JsonLogWriter:
    """The JSONL output: one process file, its path, and the only writes to it.

    Args:
        path: The resolved JSONL path. A subprocess step receives its parent's
            path and continues the same file.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._written = False

    def path(self) -> Path:
        """The JSONL file this writer appends to."""
        return Path(self._path)

    def bind_path(self, path: Path | str) -> None:
        """Name the file before anything is written to it.

        A pipeline only learns its own log directory after launch, so it names
        the file then.

        Raises:
            StateError: When lines have already been written to a different
                path. The path is the identity of the log; moving it mid-run
                would split one run across two files.
        """
        resolved = str(path)
        if self._written and resolved != self._path:
            from rey_lib.errors.error_utils import StateError

            raise StateError(
                f"Cannot rebind the run log to {resolved!r}: records are "
                f"already written to {self._path!r}. One run is one log."
            )
        self._path = resolved

    def write(self, record: dict[str, Any]) -> None:
        """Append one record as one JSON line."""
        from rey_lib.files import primitive_file_io

        primitive_file_io.append_jsonl(self.path(), record)
        self._written = True


#: The columns a run-log row is written to, besides the one payload column its
#: record type selects. Envelope and shared facts only; ``timestamp`` and
#: ``run_timestamp`` are not columns -- the database stamps ``created_ts`` and the
#: run's launch stamp belongs to ``control.run_manifest``.
_ROW_COLUMNS: tuple[str, ...] = (
    "run_id", "parent_run_log_id", "nest_level", "record_type", "record_group",
    "record_subgroup", "message", "app", "workflow_name", "pipeline_name",
    "step_id", "step_name", "step_sequence", "correlation_id", "parent_run_id",
    "subject_type", "subject_id", "subject_name", "pipeline_id", "workflow_id",
    "record_schema_version", "status", "path", "file_id",
    "source_name", "checksum_sha256", "size_bytes", "exists", "modified_at",
    "error_message",
)


class DatabaseLogWriter:
    """The database output: one ``control.run_log`` row per record.

    ``control.run_log`` is a governed control table, written through its
    governed routine: the record is translated into its row and handed to
    ``Control.write_run_log_record`` (``control.p_run_log_ins``, SECURITY
    DEFINER), which inserts it, attaches the open batch step and returns the
    ``run_log_id`` the database generated. It resolves no connection and runs
    no statement of its own.

    Args:
        control: The launch Control the rows are written through, handed over
            by whoever assembled the writer. Referenced, not owned.
    """

    def __init__(self, control: Any) -> None:
        self._control = control

    def row(self, record: dict[str, Any]) -> dict[str, Any]:
        """Translate one record into its ``control.run_log`` row.

        A record's contents divide three ways, and nothing is written twice:

        * the envelope, each with its own column
        * the shared facts -- status, path, the governed file, its size and
          checksum -- which any record type may state and which are columns
          because they are queried across types
        * whatever is left, which is that record type's own structure and goes
          into the one jsonb column ``record_type`` selects

        A record type with nothing left writes no payload at all. There is no
        generic bucket: a key arriving for a record type with no payload column
        is refused rather than dropped somewhere unqueryable. A column the
        record does not state is not written, and is NULL.

        Raises:
            DataStructureError: When a record type with no payload column
                carries fields that belong in one.
        """
        from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
        from rey_lib.logs.run_log import (
            _ENVELOPE_FIELDS, _SHARED_FIELDS, TYPE_PAYLOAD_COLUMNS,
        )

        record_type = str(record.get("record_type") or "")
        payload = {key: value for key, value in record.items()
                   if key not in _ENVELOPE_FIELDS and key not in _SHARED_FIELDS}
        payload_column = TYPE_PAYLOAD_COLUMNS.get(record_type.upper())
        if payload and payload_column is None:
            from rey_lib.data.errors import DataStructureError  # noqa: PLC0415 -- import cycle
            raise DataStructureError(
                f"{record_type} records have no typed payload column, but "
                f"{sorted(payload)} was supplied. Either the value is a shared "
                "fact with a column of its own, or this record type needs a "
                "payload column declared in TYPE_PAYLOAD_COLUMNS."
            )
        row = {column: record.get(column) for column in _ROW_COLUMNS}
        row["record_type"] = record_type
        row["record_group"] = str(record.get("record_group") or "")
        row["message"] = str(record.get("message") or "") or None
        row["nest_level"] = int(record["nest_level"])
        row["record_schema_version"] = int(record["record_schema_version"])
        # Absent means unclassified, which is not the same as non-sensitive and
        # is written as NULL rather than as false.
        row["contains_sensitive_data"] = record.get(SENSITIVE_FIELD)
        if payload:
            row[payload_column] = payload
        return {column: value for column, value in row.items() if value is not None}

    def write(self, record: dict[str, Any]) -> Any:
        """Write one record as a ``control.run_log`` row; the ``run_log_id`` it was given.

        Raises:
            ConfigError: When no Control was handed to this writer.
        """
        from rey_lib.errors.error_utils import ConfigError
        from rey_lib.logs.run_log import TYPE_PAYLOAD_COLUMNS

        if self._control is None:
            raise ConfigError(
                "run_store selects the control database but no Control was handed "
                "to the database output."
            )
        row = self.row(record)
        return self._control.write_run_log_record(
            **{column: row.get(column) for column in _ROW_COLUMNS},
            contains_sensitive_data=row.get("contains_sensitive_data"),
            payloads={column: row.get(column) for column in TYPE_PAYLOAD_COLUMNS.values()},
            required=True,
        )


class LogWriter:
    """Selects a record's outputs from ``logging.run_store`` and writes them.

    Args:
        mode: ``db``, ``jsonl`` or ``both``.
        database: The database output; required when the mode includes it.
        json: The JSONL output; required when the mode includes it, and absent
            otherwise -- there is no JSONL path unless JSONL is selected.
    """

    def __init__(
        self,
        mode: str,
        *,
        database: Optional[DatabaseLogWriter] = None,
        json: Optional[JsonLogWriter] = None,
    ) -> None:
        from rey_lib.errors.error_utils import ConfigError
        from rey_lib.logs.run_store import RUN_STORE_MODES

        self.mode = str(mode).strip().lower()
        if self.mode not in RUN_STORE_MODES:
            raise ConfigError(
                f"logging.run_store is '{mode}'. Use one of {', '.join(RUN_STORE_MODES)}."
            )
        if self.writes_db and database is None:
            raise ConfigError(f"run_store '{self.mode}' needs a database output.")
        if self.writes_jsonl and json is None:
            raise ConfigError(f"run_store '{self.mode}' needs a JSONL output.")
        self._database = database if self.writes_db else None
        self._json = json if self.writes_jsonl else None
        self._persisting = False

    @property
    def writes_db(self) -> bool:
        """Whether the control database is a selected output."""
        return self.mode in ("db", "both")

    @property
    def writes_jsonl(self) -> bool:
        """Whether the JSONL file is a selected output."""
        return self.mode in ("jsonl", "both")

    def json_path(self) -> Optional[Path]:
        """The JSONL file, or None when JSONL is not selected."""
        return self._json.path() if self._json is not None else None

    def bind_path(self, path: Path | str) -> None:
        """Name the JSONL file before anything is written; nothing to name without JSONL."""
        if self._json is not None:
            self._json.bind_path(path)

    @property
    def persisting(self) -> bool:
        """Whether a record is being persisted now; records produced meanwhile are dropped."""
        return self._persisting

    @contextmanager
    def persistence(self) -> Iterator[None]:
        """Mark a block as the logging output's own persistence work.

        The database output executes SQL, and SQL execution is a logged event,
        so a call made on the output's behalf produces records while it runs.
        Those are not run evidence -- they describe the log writing, not the
        application working -- and persisting them would call the database to
        record the call that was recording something, without end. A record
        produced inside this block is not written anywhere.
        """
        outer = self._persisting
        self._persisting = True
        try:
            yield
        finally:
            self._persisting = outer

    def write(
        self,
        record: dict[str, Any],
        *,
        database_record: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """Write one built record to every selected output.

        Args:
            record: The record as built; the JSONL line is this record.
            database_record: What the database row holds, when it differs from
                ``record`` -- an application event's row keeps today's content,
                not the whole structured envelope.

        Returns:
            The ``run_log_id`` the database minted, or None when the database is
            not selected or the record was produced inside the guard.

        Raises:
            Whatever an output raises. The caller decides how a failed write is
            reported; logging must not mask execution.
        """
        if self._persisting:
            return None
        run_log_id: Optional[int] = None
        if self._database is not None:
            row = database_record if database_record is not None else record
            with self.persistence():
                run_log_id = self._database.write(row)
            record["run_log_id"] = run_log_id
        if self._json is not None:
            self._json.write(record)
        return run_log_id

    def require_persisted(self, run_log_id: Optional[int], record_type: str) -> None:
        """Escalate a record the caller requires that was not written everywhere.

        Under ``both`` a missing ``run_log_id`` means the record reached one
        output and not the other. The caller decides which records are
        required; this decides whether their write succeeded. Under ``db`` and
        ``jsonl`` nothing is raised, as before.

        Raises:
            StateError: Under ``both``, when ``run_log_id`` is None.
        """
        if run_log_id is not None or self.mode != "both":
            return
        from rey_lib.errors.error_utils import StateError

        raise StateError(
            f"run_store is 'both' but the {record_type} record was not committed "
            "to every destination. Both are required; the run log now describes "
            "this run in one place and not the other."
        )


def build_log_writer(ctx: Any, operation: str = "app") -> LogWriter:
    """Assemble the process's logging writer from resolved configuration.

    Called by the launch boundary, which already holds the run identity and
    the launch Control, and by the frozen trade_analyzer's configuration
    points. The result is handed to ``AppLogger._configure``; AppLogger knows
    nothing about which outputs it holds.

    Args:
        ctx: The launch context, with run identity established.
        operation: The operation name, substituted into the JSONL path.

    Raises:
        ConfigError: When ``logging.run_store`` is not a known mode, or selects
            the database and the launch has no Control.
    """
    from rey_lib.logs.run_store import run_store_mode, validate_run_store

    validate_run_store(ctx)
    mode = run_store_mode(ctx)
    json = None
    if mode in ("jsonl", "both"):
        json = JsonLogWriter(_resolve_jsonl_path(ctx, operation))
    database = None
    if mode in ("db", "both"):
        # The launch Control: control.run_log is written through its governed routine.
        control = getattr(ctx, "shared_control", None)
        if control is None:
            from rey_lib.errors.error_utils import ConfigError

            raise ConfigError(
                f"logging.run_store is '{mode}', but this launch has no Control to write "
                "control.run_log through."
            )
        database = DatabaseLogWriter(control)
    return LogWriter(mode, database=database, json=json)


def _resolve_jsonl_path(ctx: Any, operation: str) -> Path:
    """Resolve the JSONL path: ``jsonl_path``, else beside ``log_path``, else under ~/logs."""
    from rey_lib.logs.app_logger import _resolve_log_path

    timestamp = ctx.run_timestamp
    if getattr(ctx, "jsonl_path", None):
        return _resolve_log_path(ctx.jsonl_path, ctx, operation, timestamp)
    if getattr(ctx, "log_path", None):
        return _resolve_log_path(ctx.log_path, ctx, operation, timestamp).with_suffix(".jsonl")
    app_name = getattr(ctx, "app_name", None) or getattr(ctx, "name", None) or "app"
    template = f"~/logs/{app_name}/{app_name}.{{operation}}.{{timestamp}}.jsonl"
    return _resolve_log_path(template, ctx, operation, timestamp)
