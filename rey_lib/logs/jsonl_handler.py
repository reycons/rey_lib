"""
The logging handler, and the record-classification vocabulary.

``JsonlHandler`` is plumbing. It receives each stdlib ``LogRecord`` that reaches
the configured pipeline and forwards it: ``LogRecordBuilder`` builds the
structured record once, and ``LogWriter`` writes it to the outputs
``logging.run_store`` selects (db, jsonl or both). The handler builds nothing,
owns no file and chooses no destination. Where a run log is bound, the record
is built with that run's context and the run log is told the row it produced,
so the row can anchor the run's nesting.

It keeps its historical name; it is no longer specific to JSONL.

Public API
----------
JsonlHandler    logging.Handler subclass, added by AppLogger's configuration.
SENSITIVE_FIELD The field a record states its own sensitivity in.
classified / sensitive
                Build a record's ``extra`` fields with that classification.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["JsonlHandler", "SENSITIVE_FIELD", "classified", "sensitive"]

#: What a record says about its own content.
#:
#: **Classification, not suppression.** A record carrying this is written whole
#: and read whole by anyone authorized to read it; the flag exists so an access
#: control can decide who that is. Nothing here removes, trims or rewrites a
#: record because of it -- marked secret, not emptied.
#:
#: Stated by the code that writes the record, because that is the only place
#: that knows. Nothing infers it later from a message.
SENSITIVE_FIELD = "contains_sensitive_data"


def classified(is_sensitive: bool, **fields: Any) -> dict[str, Any]:
    """Fields for one log record, saying whether its content is sensitive.

    One spelling of the name, so two callers cannot disagree about it.

    Args:
        is_sensitive: Whether this record's content is sensitive -- generated
            SQL, database error detail, returned or derived values, or a count
            that discloses something.
        **fields: The record's own structured fields, carried unchanged.

    Returns:
        What to pass as ``extra``.
    """
    return {SENSITIVE_FIELD: bool(is_sensitive), **fields}


def sensitive(**fields: Any) -> dict[str, Any]:
    """Fields for a record whose content is sensitive."""
    return classified(True, **fields)

class JsonlHandler(logging.Handler):
    """Forwards each record to the structured-record builder and the logging writer.

    Args:
        builder: The process's ``LogRecordBuilder``.
        writer: The process's ``LogWriter``.
    """

    def __init__(self, builder: Any, writer: Any) -> None:
        super().__init__()
        self._builder = builder
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        """Build the record once and hand it to the writer.

        A record produced while the writer is persisting is dropped from every
        output, as the writer's guard requires.
        """
        if self._writer.persisting:
            return
        try:
            from rey_lib.logs.record_enrichment import bound_run_log

            run_log = bound_run_log()
            run = run_log.run_fields() if run_log is not None else None
            built, row = self._builder.log_record(record, run=run)
            run_log_id = self._writer.write(built, database_record=row)
            if run_log is not None:
                run_log.recorded(built["nest_level"], run_log_id)
        except Exception:  # noqa: BLE001 — handler must never raise
            self.handleError(record)
