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
its baseline mutation inside one routine, and ``clear_classifications`` clears a
whole matched set in one statement. Recreating a lock-shaped API over that would
be modelling the old storage rather than the domain.

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
        classification: Optional[dict[str, Any]] = None,
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
            classification=classification,
        ))

    def update(self, file_manifest_id: int, **fields: Any) -> None:
        """Change a file's current state.

        Only what is named changes; anything absent keeps the value it has. A
        caller that knows the classification does not have to restate the
        checksum to avoid erasing it.
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
    ) -> int:
        """Append one event to a file's history, and return the mutation's id.

        History is append-only. There is no update and no delete here, and the
        routines that could do either are not granted to any application role.

        The mutation points at the batch step that executed it, which the
        routine sets from the step it opens for itself. It is not a parameter:
        the caller's step is the parent above that one, not the step that did
        the work.
        """
        return int(self._control.append_file_mutation(
            file_manifest_id, record_type=record_type, action=action,
            status=status or None, source_record_id=source_record_id,
            run_log_id=run_log_id, path=path or None,
            producer=producer, conversion=conversion, result=result,
            rollback=rollback, deleted_in=deleted_in, deleted_ts=deleted_ts,
        ))

    def clear_classifications(self, file_manifest_ids: list[int]) -> int:
        """Clear the classification on a whole matched set, or none of it.

        Classification is current state, so clearing it returns those files to
        unclassified. No mutation is appended: ``file_mutation`` records what
        happened to the *file*, and the file did not change.

        All-or-nothing across the set, because the routine does it in one
        statement. Returns how many were actually cleared -- comparing that to
        the size of the set says whether any had already been cleared.
        """
        return int(self._control.clear_file_classifications(
            list(file_manifest_ids or [])))

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

    def request_rollback(self, *, file_mutation_id: Optional[int] = None,
                         batch_step_id: Optional[int] = None,
                         batch_id: Optional[int] = None,
                         run_id: Optional[int] = None) -> int:
        """Mark mutations for reversal, and return how many were newly marked.

        Exactly one scope. Rollback is state on the mutation it reverses -- no
        rollback record is written and nothing is deleted -- so this freezes the
        set of rows to reverse and changes no file.

        Asking twice is safe: a mutation already pending keeps the request that
        claimed it, and one already reversed is not reopened.
        """
        return int(self._control.request_file_rollback(
            file_mutation_id=file_mutation_id, batch_step_id=batch_step_id,
            batch_id=batch_id, run_id=run_id,
        ) or 0)

    def pending_rollbacks(self) -> list[dict[str, Any]]:
        """Return the mutations awaiting reversal, newest first.

        These rows are the durable queue. A reversal that fails, or a process
        that dies partway, leaves its row here for the next run to pick up.

        Each carries ``restore_to_path``: where that mutation is reversed to,
        resolved from the file's own history. Current location is whatever the
        newest mutation that has not been rolled back says, so reversing one
        returns the file to the previous surviving mutation's path.
        """
        return self._control.pending_file_rollbacks()

    def complete_rollback(self, file_mutation_id: int) -> None:
        """Record that one mutation's inverse succeeded.

        Called immediately after that mutation's filesystem operation is
        reversed -- one row at a time, because one inverse succeeding proves
        nothing about the others.
        """
        self._control.complete_file_rollback(file_mutation_id)

    def records_for_run(self, run_id: int) -> list[dict[str, Any]]:
        """Return the files one run recorded.

        By the run's durable identity, not the log file it happened to write
        to. A run outlives its log.
        """
        return self._control.files_for_run(run_id)
