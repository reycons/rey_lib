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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.encryption import sha256_file
from rey_lib.errors.error_utils import ConfigError, DatabaseError

__all__ = ["FileManifest", "InventoryOutcome"]


@dataclass(frozen=True)
class InventoryOutcome:
    """What inventorying one candidate did.

    ``status`` is ``inventoried``, ``inventory_recorded``,
    ``already_inventoried`` or ``failed``. A failure carries its ``reason``
    and what KIND of failure it was (``failure``), and nothing else; the
    created flags are the routine's answer, read off the row it returned.

    The kind is what a failure means, so a caller handles each by that meaning
    rather than generically (backlog 624):

        missing     the file is no longer there -- nothing to govern
        unstable    its size moved while it was hashed -- still being written
        unreadable  it is there and could not be read
        database    the manifest could not record it -- a system failure
    """

    status: str
    reason: Optional[str] = None
    file_manifest_id: Optional[int] = None
    manifest_created: bool = False
    inventory_created: bool = False
    failure: Optional[str] = None

    @classmethod
    def failed(cls, reason: str, failure: str) -> "InventoryOutcome":
        """A candidate that could not be inventoried, why, and what kind of failure."""
        return cls(status="failed", reason=reason, failure=failure)


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
        path: Path | str,
        *,
        source_name: str = "",
        evidence: Optional[dict[str, Any]] = None,
        producer: Optional[dict[str, Any]] = None,
    ) -> InventoryOutcome:
        """Inventory one candidate file, and say what happened.

        Reads the candidate's facts, then records it: the manifest row and the
        baseline mutation are written together by one routine call, so a file
        never exists without the record of where it was discovered.

        Nothing is looked up first. The routine decides: a file whose (path,
        checksum) is already governed is written nowhere and comes back as the
        id it already had. The id is the database's; nothing mints one here.

        A manifest record must describe ONE STABLE FILE STATE. The size is read
        either side of the checksum, and a file whose size moved was being
        written while it was read -- its checksum and size would describe
        different states -- so it is failed rather than recorded.

        Two facts come back, because they are two different kinds of thing: the
        manifest row is the file's identity, minted once; the baseline is a
        mutation saying it was observed. A file can already exist and still need
        a new observation -- rollback deletes mutations and leaves the manifest
        standing.

        Args:
            path: The candidate file.
            source_name: The configured inventory source it was found under.
            evidence: Evidence handed to the routine, where a caller has some.
            producer: Who is recording it.

        Returns:
            ``inventoried`` (a new governed file), ``inventory_recorded`` (a new
            observation of a known one), ``already_inventoried``, or ``failed``
            with its reason.
        """
        source_file = Path(path)
        try:
            size_before = source_file.stat().st_size
            checksum = sha256_file(source_file)
            size_after = source_file.stat().st_size
        except FileNotFoundError as exc:
            return InventoryOutcome.failed(str(exc), "missing")
        except OSError as exc:
            return InventoryOutcome.failed(str(exc), "unreadable")

        if size_before != size_after:
            return InventoryOutcome.failed(
                f"the file changed during inventory (size {size_before} -> {size_after})",
                "unstable",
            )

        try:
            row = self._control.inventory_file_result(
                path=str(source_file),
                file_name=source_file.name,
                base_name=source_file.stem,
                file_extension=source_file.suffix.removeprefix(".").lower(),
                checksum_sha256=checksum,
                size_bytes=size_after,
                source_name=source_name or None,
                evidence=evidence,
                producer=producer,
            )
        except (ConfigError, DatabaseError) as exc:
            return InventoryOutcome.failed(str(exc), "database")

        if not row:
            return InventoryOutcome.failed(
                "the file manifest returned no result for the inventoried file",
                "database",
            )

        manifest_created = bool(row.get("o_manifest_created"))
        inventory_created = bool(row.get("o_inventory_created"))
        if manifest_created:
            status = "inventoried"
        elif inventory_created:
            status = "inventory_recorded"
        else:
            status = "already_inventoried"
        return InventoryOutcome(
            status=status,
            file_manifest_id=row.get("o_file_manifest_id"),
            manifest_created=manifest_created,
            inventory_created=inventory_created,
        )

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
        # The reason this mutation records -- text, as the column holds it.
        result: Optional[str] = None,
        rollback: Optional[dict[str, Any]] = None,
        deleted_in: Optional[int] = None,
        deleted_ts: Optional[str] = None,
        classification: Optional[dict[str, Any]] = None,
        clear_profile: Optional[dict[str, Any]] = None,
        redacted_profile: Optional[dict[str, Any]] = None,
        base_path: str = "",
    ) -> int:
        """Append one event to a file's history, and return the mutation's id.

        History is append-only. There is no update and no delete here, and the
        routines that could do either are not granted to any application role.

        The mutation points at the batch step that executed it, which the
        routine sets from the step it opens for itself. It is not a parameter:
        the caller's step is the parent above that one, not the step that did
        the work.

        ``clear_profile`` and ``redacted_profile`` belong to a profiling event.
        They are two representations of one profiling of one file, so they are
        written together on a single mutation or not at all.

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
            classification=classification,
            clear_profile=clear_profile, redacted_profile=redacted_profile,
            base_path=base_path or None,
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

    def rollback(
        self,
        *,
        scope: str = "file",
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Take governed files back to a point in their history (backlog 612).

            selected mutation + scope -> the DB returns the scope's records
            -> per manifest, cut at the selected mutation's record type
            -> newest first, reverse the boundary and everything after it:
               undo its change, delete its record
            -> a file with no mutation left: delete its manifest

        The selected mutation is the anchor and its record type the lifecycle
        boundary. Its own manifest is cut at the selected mutation exactly;
        every other manifest at its latest mutation of that record type. The
        boundary is reversed with what follows it. A manifest with no mutation
        of that type has nothing reversed.
        A selected file, with no mutation, is reversed whole.

        Args:
            scope: file, run, file_type, batch_step, batch or installation.
            file_manifest_id: The selected file, when no mutation was selected.
            file_mutation_id: The selected mutation.
            dry_run: Return the mutations that would be reversed, changing nothing.

        Returns:
            ``mutations`` (what is, or would be, reversed, newest first),
            ``reversed`` (what was), ``failed`` (the first reversal that could
            not be done, which stops the rollback and keeps its record), and
            ``manifests_deleted``.

        Raises:
            ValueError: Unless exactly one of the file or the mutation is given,
                or when the selected mutation is not among the scope's records.
        """
        # The reverse behaviour per action already exists; it is used, not copied.
        from rey_lib.files.log_run_rollback import (
            _reversal_candidate,
            _resolved_compensation,
        )

        if (file_manifest_id is None) == (file_mutation_id is None):
            raise ValueError(
                "FileManifest.rollback needs the selected file or the selected "
                "mutation, exactly one."
            )
        records = sorted(
            self._control.request_file_rollback(
                dry_run=True,
                scope=scope,
                anchor_file_manifest_id=(
                    int(file_manifest_id) if file_mutation_id is None else None),
                rollback_to_mutation_id=(
                    int(file_mutation_id) if file_mutation_id is not None else None),
            ),
            key=lambda row: int(row["file_mutation_id"]),
            reverse=True,
        )
        boundary = None
        mutations = records
        if file_mutation_id is not None:
            anchor = next((row for row in records
                           if int(row["file_mutation_id"]) == int(file_mutation_id)), None)
            if anchor is None:
                raise ValueError(
                    f"mutation {file_mutation_id} is not among the scope's records; "
                    "only a successful mutation can be a rollback boundary."
                )
            boundary = {"file_mutation_id": int(file_mutation_id),
                        "record_type": anchor.get("record_type")}
            mutations = self._after_boundary(records, anchor)
        result: dict[str, Any] = {
            "scope": scope,
            "file_manifest_id": file_manifest_id,
            "file_mutation_id": file_mutation_id,
            "boundary": boundary,
            "dry_run": bool(dry_run),
            "mutations": mutations,
            "reversed": [],
            "failed": None,
            "manifests_deleted": [],
        }
        if dry_run:
            return result

        touched: list[int] = []
        for row in mutations:
            failure = self._reverse(row, _reversal_candidate, _resolved_compensation)
            if failure is not None:
                result["failed"] = {"file_mutation_id": row["file_mutation_id"],
                                    "reason": failure}
                break
            # Then the persistence: the record of a change that no longer exists.
            self._control.delete_file_mutation(int(row["file_mutation_id"]))
            result["reversed"].append(int(row["file_mutation_id"]))
            manifest = int(row["file_manifest_id"])
            if manifest not in touched:
                touched.append(manifest)

        # A file with no mutation left is no longer governed.
        for manifest in touched:
            if not self._control.list_file_mutations(manifest):
                self._control.delete_file_manifest(manifest)
                result["manifests_deleted"].append(manifest)
        return result

    @staticmethod
    def _after_boundary(
        records: list[Mapping[str, Any]], anchor: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Each manifest's boundary and the records after it, newest first.

        The anchor's manifest is cut at the anchor; every other manifest at its
        latest record of the anchor's record type. A manifest without one is
        not cut, so nothing of it is returned.
        """
        record_type = anchor.get("record_type")
        cut = {int(anchor["file_manifest_id"]): int(anchor["file_mutation_id"])}
        for row in records:
            manifest = int(row["file_manifest_id"])
            if manifest not in cut and row.get("record_type") == record_type:
                cut[manifest] = int(row["file_mutation_id"])
        return [row for row in records
                if int(row["file_manifest_id"]) in cut
                and int(row["file_mutation_id"]) >= cut[int(row["file_manifest_id"])]]

    @staticmethod
    def _reverse(row: Mapping[str, Any], candidate_of: Any, reverse_of: Any) -> Optional[str]:
        """Undo one mutation's change; the reason it could not be, or None.

        A mutation with nothing physical to undo carries no command; its reverse
        is deleting its record, which the caller does.
        """
        if not str(row.get("command") or "").strip():
            return None
        candidate = candidate_of(row)
        try:
            reverse = reverse_of(candidate)
        except KeyError:
            return f"no reverse behaviour for action {row.get('action')!r}"
        problem = reverse.validate(candidate)
        if problem is not None:
            return problem
        try:
            reverse.execute(candidate)
        except OSError as exc:
            return str(exc)
        return None

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
