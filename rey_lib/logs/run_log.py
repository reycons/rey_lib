"""The run log, as one object.

``RunLog`` owns writing a run's durable records: where the log is, which
destinations are selected, and the record sequence that gives each record its
identity, parent and nesting. Those concerns are currently spread across
``record_enrichment``, ``run_store``, ``record_parenting``, ``nest_level`` and
``run_state``, with no single owner — which is the defect this addresses.

Ownership, not performance
--------------------------
The sequence is kept in a JSON state file beside the run log, read and written
per record. That is deliberately left exactly as it is. The file is not merely
persistence: it is how a pipeline step running as a separate process continues
its parent's sequence rather than restarting it, and that continuity is
required.

It is also not synchronisation. Allocation is an unsynchronised
read-modify-write, so concurrent writers claim the same id — measured at 33
rows carrying 10 distinct ids across four threads, and recorded as an xfail in
``test_run_log_writer_concurrency``. Making allocation atomic is a correctness
fix with its own decision to make. Doing it here, under cover of introducing an
object, would bury a behaviour change inside a refactor.

So this object changes no semantics. It moves the same calls, in the same
order, behind one owner.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["RunLog", "run_log_for"]


class RunLog:
    """One run's durable record writer."""

    def __init__(self, ctx: Any) -> None:
        """Bind to the context whose run this log belongs to.

        Nothing is opened here. The log path is resolved on first write, as it
        was before, so a run that writes no records creates no file.
        """
        self._ctx = ctx

    def __repr__(self) -> str:
        path = getattr(self._ctx, "run_log_path", None)
        return f"<RunLog {path or 'unopened'}>"

    @property
    def path(self) -> Optional[str]:
        """The resolved run-log path, or None before the first write."""
        return getattr(self._ctx, "run_log_path", None)

    def destinations(self) -> tuple[bool, bool]:
        """Return ``(writes_jsonl, writes_db)`` for this run."""
        from rey_lib.logs import run_store

        return run_store.writes_jsonl(self._ctx), run_store.writes_db(self._ctx)

    def append(self, record_type: str, *, message: str = "", **fields: Any) -> int | None:
        """Append one typed record to every selected destination.

        The body is the former ``log_run_record``, unchanged: same validation,
        same enrichment, same stamping, same order of writes, same fail-safe.

        Returns
        -------
        int | None
            The committed ``record_id``, or ``None`` when the record could not
            be committed to every selected destination. Never raises: logging
            must not mask application execution.
        """
        from rey_lib.logs import run_store
        from rey_lib.logs.record_enrichment import (
            _enrich_run_record,
            _has_durable_run_path,
            _validate_run_record_fields,
            open_run_log,
        )

        ctx = self._ctx
        if _has_durable_run_path(ctx):
            _validate_run_record_fields(record_type, fields)
        try:
            to_jsonl, to_db = self.destinations()

            # open_run_log is only reached when JSONL is a destination: it fails
            # closed without a durable log path, which is correct for a JSONL
            # run and irrelevant to a database-only one.
            path = open_run_log(ctx) if to_jsonl else None
            record = _enrich_run_record(ctx, record_type, message=message, fields=fields)

            # Logical record identity and parent, from the shared run state
            # (SGC_Rey_Log_Record_Parenting_Phase_2). The sequence belongs to the
            # run rather than to a file: stamped before the write, committed only
            # after a successful one, so a failed write does not skip an id.
            from rey_lib.logs import record_parenting
            from rey_lib.logs.nest_level import get_nest_level

            nest_level = get_nest_level(ctx)
            record_id = record_parenting.stamp_record(ctx, record, nest_level)

            if to_jsonl:
                # Routed through the primitive I/O layer so the run-log writer
                # shares one low-level append with file_utils without either
                # foundational module importing the other
                # (SGC_Rey_Lib_Primitive_File_IO_Layer).
                from rey_lib.files import primitive_file_io

                primitive_file_io.append_jsonl(path, record)

            if to_db:
                run_store.persist_record(ctx, record_type, message, record)

            record_parenting.commit_record(ctx, record_id, nest_level)
            return record_id
        except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
            from rey_lib.logs.logging_setup import get_logger

            get_logger(__name__).warning(
                "run log: could not append %s record: %s", record_type, exc
            )
            return None


def run_log_for(ctx: Any) -> RunLog:
    """Return this run's RunLog, making it once.

    Held on the context for now, which is how ``Control`` is reached too. The
    launch boundary is the better owner and is where both should move; doing
    that here would mean touching every entry point in the same change that
    introduces the object.
    """
    existing = getattr(ctx, "run_log", None)
    if isinstance(existing, RunLog):
        return existing

    run_log = RunLog(ctx)
    try:
        ctx.run_log = run_log
    except (AttributeError, TypeError):
        # A context that cannot hold attributes still gets a RunLog, just not a
        # cached one. Writing a record never mutated the context before this
        # object existed, and some callers pass a bare object; caching is an
        # optimisation, not part of the contract.
        pass
    return run_log
