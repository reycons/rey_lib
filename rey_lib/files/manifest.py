"""
The governed file manifest, as one object.

A file's current authoritative state, and the history of what has happened to
it. Two records, one boundary:

    file_manifest   what the file is now -- where it sits, what it hashes to,
                    what it was classified as
    file_mutation   append-only history beneath that row, oldest first

Consumers ask this object for a domain operation. They do not name routines,
open transactions, or coordinate persistence, and there is no session to hold.

Why there is no session
-----------------------
The JSONL manifest was one shared file, so every writer in the installation
took an exclusive ``flock`` around the whole critical section -- load state,
assign a record id, append, commit state. That lock was global because the file
was global.

A database does not need one. Every operation here is a single mapped call, and
each of those is atomic on its own: ``inventory`` writes the manifest row and
its baseline mutation inside one routine, and ``append_mutation`` writes one
event. Recreating a lock-shaped API over that would be modelling the old storage
rather than the domain.

    Every domain operation maps to one atomic database call.

If an operation ever genuinely spans several, it becomes one routine -- as
inventory already is -- rather than a transaction a caller has to hold open.

Layering
--------
Reached through ``Control``, which is how everything reaches the control
database. This object knows no routine names and no table names; the procedure
map binds a logical operation to a routine, and that binding is the only place
either is written down.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["FileManifest"]


class FileManifest:
    """One installation's governed file manifest."""

    def __init__(self, control: Any) -> None:
        """Bind to the Control this installation reaches its manifest through.

        Parameters
        ----------
        control : Any
            The ``Control`` for this installation's control database.
        """
        self._control = control

    def __repr__(self) -> str:
        return "<FileManifest>"

    # -- recording -----------------------------------------------------------

    def inventory(
        self,
        *,
        path: str,
        file_name: str,
        base_name: str,
        file_extension: str,
        checksum_sha256: str,
        size_bytes: int,
        source_name: str = "",
        evidence: Optional[dict[str, Any]] = None,
        producer: Optional[dict[str, Any]] = None,
    ) -> int:
        """Record a governed file for the first time, and return its id.

        The manifest row and the first mutation are written together: a file
        never exists without the record of where it was discovered, so a later
        move is always understood as a change from somewhere.

        The id is the database's. Nothing mints one here, the same way nothing
        mints a run id -- recording the file is what gives it identity.

        Returns
        -------
        int
            ``file_manifest_id``.
        """
        return int(self._control.inventory_file(
            path=path, file_name=file_name, base_name=base_name,
            file_extension=file_extension, checksum_sha256=checksum_sha256,
            size_bytes=size_bytes, source_name=source_name or None,
            evidence=evidence, producer=producer,
        ))

    def update(self, file_manifest_id: int, **fields: Any) -> None:
        """Change a file's current state.

        Only what is named changes; anything absent keeps the value it has, so
        a caller that knows one field does not restate the others to avoid
        erasing them. Classification is not among the fields: it is an event on
        the file's history, not current state to be overwritten.
        """
        self._control.update_file_manifest(file_manifest_id, **fields)

    def append_mutation(
        self,
        file_manifest_id: int,
        *,
        record_type: str,
        action: str,
        status: str = "",
        source_record_id: Optional[int] = None,
        run_log_id: Optional[int] = None,
        path: str = "",
        producer: Optional[dict[str, Any]] = None,
        conversion: Optional[dict[str, Any]] = None,
        result: Optional[dict[str, Any]] = None,
        rollback: Optional[dict[str, Any]] = None,
        deleted_in: Optional[int] = None,
        deleted_ts: Optional[str] = None,
        classification: Optional[dict[str, Any]] = None,
        base_path: str = "",
    ) -> int:
        """Append one event to a file's history, and return the mutation's id.

        History is append-only. There is no update and no delete here, and the
        routines that could do either are not granted to any application role.

        The mutation points at the batch step that executed it, which the
        routine sets from the step it opens for itself. It is not a parameter:
        the caller's step is the parent above that one, not the step that did
        the work.

        ``classification`` and ``base_path`` belong to a classification event.
        Classifying is something that happens to a file, so it is recorded here
        like every other thing that happens to one -- and recording it twice
        leaves both, which is what makes reclassification non-destructive.
        """
        return int(self._control.append_file_mutation(
            file_manifest_id, record_type=record_type, action=action,
            status=status or None, source_record_id=source_record_id,
            run_log_id=run_log_id, path=path or None,
            producer=producer, conversion=conversion, result=result,
            rollback=rollback, deleted_in=deleted_in, deleted_ts=deleted_ts,
            classification=classification, base_path=base_path or None,
        ))

    # -- reading -------------------------------------------------------------

    def get(self, file_manifest_id: int) -> Optional[dict[str, Any]]:
        """Return one file's current state, or None if it was never recorded."""
        return self._control.get_file_manifest(file_manifest_id)

    def find(
        self,
        *,
        path: str = "",
        checksum_sha256: str = "",
        source_name: str = "",
        file_name: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return files matching every filter given.

        A filter that is not supplied is not a filter. ``path`` with
        ``checksum_sha256`` is how a producer asks whether it has already
        inventoried what it is looking at.
        """
        return self._control.find_file_manifest(
            path=path or None, checksum_sha256=checksum_sha256 or None,
            source_name=source_name or None, file_name=file_name or None,
            limit=limit,
        )

    def list_files(self) -> list[dict[str, Any]]:
        """Return every current file, in order.

        No filter and no cap: the consumers of this build a whole picture --
        the file hierarchy, a configured selection -- and a limit would
        silently truncate it.
        """
        return self._control.list_file_manifest()

    def history(self, file_manifest_id: int) -> list[dict[str, Any]]:
        """Return one file's mutations, oldest first.

        Ordered by the mutation's own id, which is monotonic. The first is
        always the baseline written when the file was inventoried.
        """
        return self._control.file_history(file_manifest_id)

    def all_mutations(self) -> list[dict[str, Any]]:
        """Return every mutation, in order, for building a whole hierarchy."""
        return self._control.list_file_mutations(None)

    # -- rollback ------------------------------------------------------------

    def request_rollback(self, *, dry_run: bool = True,
                         file_mutation_id: Optional[int] = None,
                         file_manifest_id: Optional[int] = None,
                         batch_step_id: Optional[int] = None,
                         batch_id: Optional[int] = None,
                         run_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Return the rollback set for one scope, marking it unless previewing.

        Exactly one scope. Under ``dry_run`` nothing is written, so this is the
        preview as well as the request -- one predicate, one shape, no way for
        the two to describe different reversals. A row that can be reversed
        carries the command that reverses it.
        """
        return self._control.request_file_rollback(
            dry_run=dry_run, file_mutation_id=file_mutation_id,
            file_manifest_id=file_manifest_id, batch_step_id=batch_step_id,
            batch_id=batch_id, run_id=run_id,
        )

    def complete_rollback(self, file_mutation_ids: list[int]) -> None:
        """Close the rollbacks whose reversals ran, named one by one.

        The row is the unit, not the request. What stays requested is what is
        still owed, and it keeps its mutation so the next rollback can pick it
        up -- which is how the service finishes work an earlier run could not.
        """
        self._control.complete_file_rollback(list(file_mutation_ids))

    def current_classification(
        self, file_manifest_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """What a file is classified as now, or every classified file.

        The one way to ask. Classification is a lifecycle event, so "current"
        is a question about history, and the database answers it -- nothing
        here selects classification events and works out which one won.
        """
        return self._control.get_current_classification(file_manifest_id)

    def records_for_run(self, run_id: int) -> list[dict[str, Any]]:
        """Return the files one run recorded.

        By the run's durable identity, not the log file it happened to write
        to. A run outlives its log.
        """
        return self._control.files_for_run(run_id)
