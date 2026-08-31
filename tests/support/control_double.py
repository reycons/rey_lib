"""A Control-shaped double: the transport, and nothing above it.

The governed file manifest lives in the control database. These tests are about
what the code above that boundary does -- how a hierarchy is assembled, how a
selection matches, how a run resolves its files, what a rollback does on disk --
so the database is replaced and everything above it stays real.

What this is not
----------------
It is not a mock of the behaviour under test. Production code does the writing,
the projection and the reading; this only holds the rows those routines would
have stored, with the same semantics the routines have:

- the database mints identity, so ``file_manifest_id`` is generated here and is
  the governed file's ``file_id``
- recording a file writes its baseline mutation in the same operation
- an update changes only the fields it names
- classification is state on the file, never a record beside it
- a rollback request marks only untouched rows and never reopens a completed
  one; a completion transitions exactly one pending row

The live contract is covered separately, against the real routines, in
``console_next/tests/test_file_mutation_rollback.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

__all__ = ["ControlDouble", "control_backed_ctx"]


class ControlDouble:
    """In-memory stand-in for ``Control``'s file-manifest surface."""

    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self._next_file = 1
        self._next_mutation = 1
        # The batch state a real Control carries. Nothing here opens steps, but
        # a caller may read these the way it reads them in production.
        self.batch_id: Optional[int] = None
        self.batch_root_step_id: Optional[int] = None
        self.batch_step_id: Optional[int] = None

    # -- writing -------------------------------------------------------------

    def inventory_file(self, path: str, file_name: str, base_name: str,
                       file_extension: str, checksum_sha256: str,
                       size_bytes: int, source_name: Optional[str] = None,
                       recorded_at: Optional[str] = None,
                       evidence: Optional[dict[str, Any]] = None,
                       producer: Optional[dict[str, Any]] = None,
                       classification: Optional[dict[str, Any]] = None,
                       required: bool = True) -> int:
        """Record a governed file and its baseline mutation, as one operation."""
        file_manifest_id = self._next_file
        self._next_file += 1
        self.files.append({
            "file_manifest_id": file_manifest_id,
            "path": path,
            "file_name": file_name,
            "base_name": base_name,
            "file_extension": file_extension,
            "checksum_sha256": checksum_sha256,
            "size_bytes": size_bytes,
            "source_name": source_name,
            "recorded_at": recorded_at,
            "evidence": evidence,
            "producer": producer,
            "classification": classification,
        })
        # A file never exists without the record of where it was discovered.
        self._append(
            file_manifest_id, record_type="source_file_inventory",
            action="record_only", path=path,
            run_log_file=(evidence or {}).get("run_log_file"),
            run_log_id=(evidence or {}).get("run_log_id"),
            created_ts=recorded_at,
        )
        return file_manifest_id

    def update_file_manifest(self, file_manifest_id: int,
                             required: bool = True, **fields: Any) -> None:
        """Change only what is named; anything absent keeps its value."""
        row = self._file(file_manifest_id)
        if row is None:
            return
        for name, value in fields.items():
            if value is not None:
                row[name] = value

    def append_file_mutation(self, file_manifest_id: int, record_type: str,
                             action: str, required: bool = True,
                             **fields: Any) -> int:
        return self._append(file_manifest_id, record_type=record_type,
                            action=action, **fields)

    def clear_file_classifications(self, file_manifest_ids: list[int],
                                   required: bool = True) -> int:
        """Clear the whole matched set, returning how many actually changed."""
        wanted = set(file_manifest_ids or [])
        cleared = 0
        for row in self.files:
            if row["file_manifest_id"] in wanted and row.get("classification"):
                row["classification"] = None
                cleared += 1
        return cleared

    # -- reading -------------------------------------------------------------

    def get_file_manifest(self, file_manifest_id: int,
                          required: bool = True) -> Optional[dict[str, Any]]:
        row = self._file(file_manifest_id)
        return dict(row) if row else None

    def find_file_manifest(self, path: Optional[str] = None,
                           checksum_sha256: Optional[str] = None,
                           source_name: Optional[str] = None,
                           file_name: Optional[str] = None,
                           limit: int = 100,
                           required: bool = True) -> list[dict[str, Any]]:
        """Every filter given must hold; one not supplied is not a filter."""
        wanted = {"path": path, "checksum_sha256": checksum_sha256,
                  "source_name": source_name, "file_name": file_name}
        matched = [
            dict(row) for row in self.files
            if all(row.get(field) == value
                   for field, value in wanted.items() if value is not None)
        ]
        return matched[:limit]

    def list_file_manifest(self, required: bool = True) -> list[dict[str, Any]]:
        return [dict(row) for row in self.files]

    def file_history(self, file_manifest_id: int,
                     required: bool = True) -> list[dict[str, Any]]:
        """One file's mutations, oldest first -- baseline first."""
        return [dict(row) for row in self.mutations
                if row["file_manifest_id"] == file_manifest_id]

    def list_file_mutations(self, file_manifest_id: Optional[int] = None,
                            required: bool = True) -> list[dict[str, Any]]:
        return [dict(row) for row in self.mutations
                if file_manifest_id is None
                or row["file_manifest_id"] == file_manifest_id]

    def files_for_run(self, run_id: int,
                      required: bool = True) -> list[dict[str, Any]]:
        return [dict(row) for row in self.files
                if (row.get("evidence") or {}).get("run_id") == run_id]

    # -- rollback ------------------------------------------------------------

    def request_file_rollback(self, *, file_mutation_id: Optional[int] = None,
                              batch_step_id: Optional[int] = None,
                              batch_id: Optional[int] = None,
                              run_id: Optional[int] = None,
                              required: bool = True) -> int:
        """Mark untouched rows only, and report just what this call added."""
        scopes = [file_mutation_id, batch_step_id, batch_id, run_id]
        if sum(scope is not None for scope in scopes) != 1:
            raise ValueError("exactly one rollback scope is required")
        marked = 0
        for row in self.mutations:
            if file_mutation_id is not None and row["file_mutation_id"] != file_mutation_id:
                continue
            if row["rollback_request_in"] or row["rollback_complete_in"]:
                continue
            row["rollback_request_in"] = 1
            row["rollback_request_batch_step_id"] = self.batch_step_id
            marked += 1
        return marked

    def pending_file_rollbacks(self,
                               required: bool = True) -> list[dict[str, Any]]:
        """Pending rows newest first, each with where it reverses to."""
        pending = [row for row in self.mutations
                   if row["rollback_request_in"] == 1
                   and row["rollback_complete_in"] == 0]
        rows = []
        for row in sorted(pending, key=lambda r: r["file_mutation_id"],
                          reverse=True):
            out = dict(row)
            out["restore_to_path"] = self._restore_target(row)
            rows.append(out)
        return rows

    def complete_file_rollback(self, file_mutation_id: int,
                               required: bool = True) -> None:
        """Transition exactly one pending row; refuse anything else."""
        for row in self.mutations:
            if (row["file_mutation_id"] == file_mutation_id
                    and row["rollback_request_in"] == 1
                    and row["rollback_complete_in"] == 0):
                row["rollback_request_in"] = 0
                row["rollback_complete_in"] = 1
                row["rollback_batch_step_id"] = self.batch_step_id
                return
        raise ValueError(
            f"file_mutation {file_mutation_id} is not pending rollback")

    # -- internals -----------------------------------------------------------

    def _file(self, file_manifest_id: int) -> Optional[dict[str, Any]]:
        return next((row for row in self.files
                     if row["file_manifest_id"] == file_manifest_id), None)

    def _append(self, file_manifest_id: int, **fields: Any) -> int:
        file_mutation_id = self._next_mutation
        self._next_mutation += 1
        row = {
            "file_mutation_id": file_mutation_id,
            "file_manifest_id": file_manifest_id,
            "source_record_id": None, "record_type": None, "action": None,
            "run_log_file": None, "run_log_id": None, "path": None,
            "status": None, "deleted_in": None, "deleted_ts": None,
            "created_ts": None, "producer": None, "conversion": None,
            "result": None, "rollback": None,
            "clear_profile": None, "redacted_profile": None,
            "batch_step_id": self.batch_step_id,
            "rollback_request_in": 0, "rollback_complete_in": 0,
            "rollback_request_batch_step_id": None,
            "rollback_batch_step_id": None,
        }
        row.update({k: v for k, v in fields.items() if k in row})
        self.mutations.append(row)
        return file_mutation_id

    def _restore_target(self, row: dict[str, Any]) -> Optional[str]:
        """The path of the previous mutation that has not been rolled back.

        Current location is whatever the newest surviving mutation says, so
        reversing one returns the file to its predecessor's path.
        """
        earlier = [
            other for other in self.mutations
            if other["file_manifest_id"] == row["file_manifest_id"]
            and other["file_mutation_id"] < row["file_mutation_id"]
            and other["rollback_complete_in"] == 0
        ]
        return earlier[-1]["path"] if earlier else None


def control_backed_ctx(**attributes: Any) -> SimpleNamespace:
    """A context whose governed manifest is a ControlDouble.

    Seed it by calling production code -- ``log_file_manifest_record`` or
    ``FileManifest`` -- so the rows under test are the rows the real writers
    would have produced.
    """
    ctx = SimpleNamespace(**attributes)
    ctx.shared_control = ControlDouble()
    return ctx
