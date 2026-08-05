"""
Installation-scoped governed file manifest.

The file manifest is not an application log. It is a separate append-only JSONL
file recording governed source-file lifecycle facts for one installation
(SGC_Wolff_Popper_Source_File_Inventory_and_File_Manifest). Application
execution records continue to go to the run log; nothing is written here first
and copied there, or the reverse.

Sequencing differs from the run log in one decisive way. A run log has exactly
one writer, so its ``record_id`` can come from unlocked run state. The manifest
is shared by every run in the installation, so assigning a row number requires
real mutual exclusion. The complete critical section — load state, repair it,
assign ``record_id``, append, commit state — is held under an exclusive
``flock`` on a companion lock file:

    file_manifest.jsonl
    file_manifest.jsonl.lock
    file_manifest.jsonl.hstate.json

State carries the committed manifest size in bytes alongside the last record
id. A writer interrupted after the append but before the state commit leaves
the recorded size disagreeing with the file, and the next writer inspects the
manifest and repairs state to its highest retained ID before assigning. Two
concurrent writers can therefore never receive the same ``record_id``.
Governed rewrites may intentionally leave ID gaps; retained IDs are never
renumbered and the next append continues above the highest retained ID.

``flock`` is released by the kernel when a holding process dies, so an
interrupted writer cannot wedge the manifest. This requires a POSIX filesystem.
"""

from __future__ import annotations

import fcntl
import json
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = [
    "FileManifestError",
    "FileManifestSession",
    "file_manifest_session",
    "file_manifest_write_boundary",
    "log_file_manifest_record",
    "manifest_lock_path",
    "manifest_state_path",
    "resolve_file_manifest_path",
]

_LOCK_SUFFIX = ".lock"
_STATE_SUFFIX = ".hstate.json"
_PATH_NAME = "file_manifest"

_LAST_RECORD_ID = "last_record_id"
_MANIFEST_SIZE_BYTES = "manifest_size_bytes"

# The canonical persisted order of manifest root fields, shared by every record
# type. This is the final serialization boundary, so it owns the order; a
# record-type serializer owns which of these fields exist and what they hold,
# never where they sit. Fields a record type does not carry are omitted, and a
# root field that is not listed here is rejected rather than written.
_CANONICAL_ROOT_FIELDS: tuple[str, ...] = (
    "record_id",
    "file_id",
    "recorded_at",
    "record_type",
    "action",
    "status",
    "source_name",
    "evidence",
    "file",
    "lineage",
    "classification",
    "rollback",
    "conversion",
    "result",
    "producer",
)

_logger = logging.getLogger(__name__)


class FileManifestError(Exception):
    """Raised when a governed manifest record cannot be sequenced or appended.

    Deliberately not derived from ``rey_lib.errors.AppError``: the logging layer
    sits below ``rey_lib.errors``, which imports ``get_logger`` from this
    package, so a module-level errors import here would close a cycle. Every
    other rey_lib.logs module reaches ``rey_lib.errors`` through a lazy
    in-function import for the same reason.
    """


def resolve_file_manifest_path(ctx: Any) -> Path:
    """
    Resolve the installation-configured manifest path from the context.

    Parameters
    ----------
    ctx : Any
        Application context carrying the resolved installation ``paths``.

    Returns
    -------
    Path
        The configured ``file_manifest`` path.

    Raises
    ------
    FileManifestError
        If no installation-owned ``file_manifest`` path is configured. There is
        no default and no derived location — a missing path is a configuration
        failure, never something this layer invents.
    """
    resolver = getattr(ctx, "paths", None)
    resolve = getattr(resolver, "resolve", None)
    if not callable(resolve):
        raise FileManifestError(
            "Resolved context carries no path resolver; the installation-owned "
            "'file_manifest' path cannot be resolved."
        )
    # Imported lazily because rey_lib.errors imports get_logger from this
    # package; a module-level import would close a cycle.
    from rey_lib.errors.error_utils import ConfigError

    try:
        return Path(resolve(_PATH_NAME))
    except ConfigError as exc:
        raise FileManifestError(
            f"Installation configuration does not define a '{_PATH_NAME}' path: {exc}"
        ) from exc


def manifest_lock_path(manifest_path: Path | str) -> Path:
    """Return the deterministic companion lock path for a manifest."""
    return Path(str(manifest_path) + _LOCK_SUFFIX)


def manifest_state_path(manifest_path: Path | str) -> Path:
    """Return the deterministic companion sequencing-state path for a manifest."""
    return Path(str(manifest_path) + _STATE_SUFFIX)


def log_file_manifest_record(ctx: Any, record: dict[str, Any]) -> int:
    """
    Append one governed record to the installation file manifest.

    The returned ``record_id`` is also written into the record itself, so a
    manifest line always carries its own durable row number.

    Parameters
    ----------
    ctx : Any
        Application context carrying the resolved installation ``paths``.
    record : dict[str, Any]
        One governed manifest record. It must not already carry ``record_id``;
        sequencing is owned here, never supplied by the caller.

    Returns
    -------
    int
        The committed monotonically assigned ``record_id``. Governed deletion
        may leave gaps, so this identity is not necessarily the physical row.

    Raises
    ------
    FileManifestError
        If the manifest path is unconfigured, the record is malformed, or the
        record could not be sequenced and appended.
    """
    _validate_unsequenced_record(record)

    manifest_path = resolve_file_manifest_path(ctx)
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileManifestError(
            f"Manifest directory cannot be created for '{manifest_path}': {exc}"
        ) from exc

    with _ManifestLock(manifest_path):
        return _append_locked(manifest_path, record)


class FileManifestSession:
    """Public append/read surface held under the shared manifest lock."""

    def __init__(self, manifest_path: Path) -> None:
        self.path = manifest_path

    def read_records(self) -> list[dict[str, Any]]:
        """Strictly read every current manifest record in physical order."""
        if not self.path.exists():
            return []
        from rey_lib.files.jsonl import read_jsonl_file

        return [dict(item.record) for item in read_jsonl_file(self.path)]

    def append(self, record: dict[str, Any]) -> int:
        """Append one record without reacquiring the already-held lock."""
        _validate_unsequenced_record(record)
        return _append_locked(self.path, record)

    def remove_records(self, record_ids: Iterable[int]) -> int:
        """Drop the named records, returning how many were removed.

        This is the governed-rewrite counterpart of :meth:`append`, and exists
        because the lock is not reentrant: ``file_manifest_write_boundary``
        opens its own handle and takes the same exclusive lock, so a domain
        owner holding a session cannot enter the boundary to rewrite. This
        performs the rewrite under the lock the session already holds.

        Removal is atomic and leaves intentional ID gaps: retained records keep
        their IDs, and sequencing state is synchronized to the highest retained
        ID so the next append still lands above everything that came before.
        """
        targets = {_removable_record_id(value) for value in record_ids}
        if not targets:
            return 0
        retained = [
            record
            for record in self.read_records()
            if record.get("record_id") not in targets
        ]
        _rewrite_locked(self.path, retained)
        return len(targets)


@contextmanager
def file_manifest_session(ctx: Any) -> Iterator[FileManifestSession]:
    """Yield a lock-aware manifest session for governed multi-record work."""
    manifest_path = resolve_file_manifest_path(ctx)
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileManifestError(
            f"Manifest directory cannot be created for '{manifest_path}': {exc}"
        ) from exc
    with _ManifestLock(manifest_path):
        yield FileManifestSession(manifest_path)


@contextmanager
def file_manifest_write_boundary(ctx: Any) -> Iterator[Path]:
    """Yield the governed manifest under its shared exclusive write lock.

    Domain owners may perform a validated atomic rewrite while this boundary is
    held. On successful exit, sequencing is resynchronized through the one rule
    every governed rewrite shares.
    """
    manifest_path = resolve_file_manifest_path(ctx)
    with _ManifestLock(manifest_path):
        # Read before the domain owner rewrites: afterwards the removed ids
        # are gone from the manifest and cannot be recovered from state.
        previous = int(_load_state(manifest_path)[_LAST_RECORD_ID])
        yield manifest_path
        _commit_after_rewrite(manifest_path, previous)


def _validate_unsequenced_record(record: Any) -> None:
    """Reject malformed records before entering the append primitive."""
    if not isinstance(record, dict):
        raise FileManifestError("A manifest record must be a JSON object.")
    if "record_id" in record:
        raise FileManifestError(
            "A manifest record must not supply 'record_id'; the manifest writer "
            "owns durable sequencing."
        )


def _canonical_record(record: dict[str, Any], record_id: int) -> dict[str, Any]:
    """Assign ``record_id`` and return the record in canonical root order.

    An unknown root field is rejected rather than written: the manifest is an
    append-only evidence store, so a field nobody can name must not become
    permanent. Nested content is untouched — each section belongs to the
    record-type serializer that built it.
    """
    unknown = sorted(set(record) - set(_CANONICAL_ROOT_FIELDS))
    if unknown:
        raise FileManifestError(
            "Manifest record carries unknown root field(s): " + ", ".join(unknown)
        )
    sequenced = {"record_id": record_id, **record}
    return {
        name: sequenced[name]
        for name in _CANONICAL_ROOT_FIELDS
        if name in sequenced
    }


def _append_locked(manifest_path: Path, record: dict[str, Any]) -> int:
    """Sequence and append one record while the caller holds the manifest lock."""
    state = _load_state(manifest_path)
    record_id = int(state[_LAST_RECORD_ID]) + 1
    sequenced = _canonical_record(record, record_id)

    from rey_lib.files import primitive_file_io

    try:
        primitive_file_io.append_jsonl(manifest_path, sequenced)
    except (OSError, TypeError, ValueError) as exc:
        raise FileManifestError(
            f"Manifest record could not be appended to '{manifest_path}': {exc}"
        ) from exc
    _commit_state(manifest_path, record_id)
    return record_id


def _commit_after_rewrite(
    manifest_path: Path,
    previous_last_record_id: int,
) -> None:
    """Resynchronize sequencing after a governed rewrite.

    Every rewrite path commits through here, so the rule cannot differ between
    them. Retained IDs keep their identity and removed IDs leave gaps, and the
    sequence never moves backwards: removing the highest records, or all of
    them, must not hand their IDs to a later append. The run log stores manifest
    IDs permanently, so a reissued ID would silently retarget evidence that is
    already written and can never be corrected.

    The previous high-water mark must be read before the rewrite. Afterwards it
    is unrecoverable: state repair recounts it from the manifest, which no
    longer holds the removed records.
    """
    _commit_state(
        manifest_path,
        max(_highest_record_id(manifest_path), int(previous_last_record_id)),
    )


def _removable_record_id(value: Any) -> int:
    """Reject anything that is not a durable record ID before a rewrite."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FileManifestError(
            f"Manifest record id to remove must be an integer, got {value!r}."
        ) from exc
    if number < 1:
        raise FileManifestError(
            f"Manifest record id to remove must be positive, got {number}."
        )
    return number


def _rewrite_locked(
    manifest_path: Path,
    retained: list[dict[str, Any]],
) -> None:
    """Replace the manifest with ``retained`` while the caller holds the lock.

    The replacement is staged and moved into place, so a failure mid-write
    leaves the existing manifest untouched rather than truncated.
    """
    from rey_lib.files import primitive_file_io

    previous = int(_load_state(manifest_path)[_LAST_RECORD_ID])
    staged = manifest_path.with_name(f"{manifest_path.name}.rewrite")
    try:
        staged.unlink(missing_ok=True)
        for record in retained:
            primitive_file_io.append_jsonl(staged, record)
        if not retained:
            staged.touch()
        staged.replace(manifest_path)
    except (OSError, TypeError, ValueError) as exc:
        staged.unlink(missing_ok=True)
        raise FileManifestError(
            f"Manifest could not be rewritten at '{manifest_path}': {exc}"
        ) from exc
    _commit_after_rewrite(manifest_path, previous)


class _ManifestLock:
    """Exclusive advisory lock over one manifest's complete critical section."""

    def __init__(self, manifest_path: Path) -> None:
        """Record the manifest whose companion lock file will be held."""
        self._lock_path = manifest_lock_path(manifest_path)
        self._handle: Any = None

    def __enter__(self) -> _ManifestLock:
        """Open the companion lock file and block until the lock is acquired."""
        try:
            # Opened in append mode so acquiring the lock never truncates it and
            # never races another writer's creation of the same file.
            self._handle = self._lock_path.open("a", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise FileManifestError(
                f"Manifest lock could not be acquired at '{self._lock_path}': {exc}"
            ) from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Release the lock and close the companion handle."""
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _load_state(manifest_path: Path) -> dict[str, int]:
    """
    Return sequencing state, repaired against the manifest when it disagrees.

    A recorded size that does not match the manifest means an append or governed
    rewrite occurred after the last state commit, so the manifest's highest
    retained record ID becomes authoritative.
    """
    state = _read_state(manifest_path)
    actual_size = _manifest_size(manifest_path)
    if state[_MANIFEST_SIZE_BYTES] == actual_size:
        return state

    repaired = {
        _LAST_RECORD_ID: _highest_record_id(manifest_path),
        _MANIFEST_SIZE_BYTES: actual_size,
    }
    _logger.warning(
        "file manifest: sequencing state repaired for %s "
        "(recorded size %s, actual size %s, recounted last_record_id %s)",
        manifest_path,
        state[_MANIFEST_SIZE_BYTES],
        actual_size,
        repaired[_LAST_RECORD_ID],
    )
    return repaired


def _read_state(manifest_path: Path) -> dict[str, int]:
    """Read the companion state file, tolerating a missing or malformed file."""
    path = manifest_state_path(manifest_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {_LAST_RECORD_ID: 0, _MANIFEST_SIZE_BYTES: -1}
    if not isinstance(raw, dict):
        return {_LAST_RECORD_ID: 0, _MANIFEST_SIZE_BYTES: -1}
    return {
        _LAST_RECORD_ID: _as_int(raw.get(_LAST_RECORD_ID), 0),
        # -1 can never equal a real size, so an unreadable state always repairs.
        _MANIFEST_SIZE_BYTES: _as_int(raw.get(_MANIFEST_SIZE_BYTES), -1),
    }


def _commit_state(manifest_path: Path, record_id: int) -> None:
    """Persist the committed record id and the manifest size it corresponds to.

    Recovery, not durability. A writer interrupted between appending a record
    and committing this state leaves the two inconsistent, and that is repaired
    by recounting the manifest. The recount fixes inconsistent state; it does
    not make an acknowledged append survive power loss. Neither the append nor
    this state file is flushed to stable storage, so a machine that loses power
    can lose a record the caller was told was written. Choosing synchronization
    boundaries for the append path -- run, batch, checkpoint, or close -- is a
    separate decision and deliberately not made here.
    """
    from rey_lib.files.json import write_json_file

    state = {
        _LAST_RECORD_ID: int(record_id),
        _MANIFEST_SIZE_BYTES: _manifest_size(manifest_path),
    }
    try:
        write_json_file(
            manifest_state_path(manifest_path),
            state,
            mode="compact",
            newline=False,
        )
    except OSError as exc:
        raise FileManifestError(
            f"Manifest sequencing state could not be committed for "
            f"'{manifest_path}': {exc}"
        ) from exc


def _manifest_size(manifest_path: Path) -> int:
    """Return the manifest size in bytes, or 0 when it does not yet exist."""
    try:
        return manifest_path.stat().st_size
    except OSError:
        return 0


def _highest_record_id(manifest_path: Path) -> int:
    """Return the highest valid record ID, preserving intentional gaps."""
    if not manifest_path.exists():
        return 0
    # The governed store is read through the one strict JSONL reader rather
    # than parsed here: a manifest that cannot be read must fail, not be
    # silently recounted from whatever lines happened to parse.
    from rey_lib.files.jsonl import read_jsonl_file

    try:
        highest = 0
        for item in read_jsonl_file(manifest_path):
            record_id = item.record.get("record_id")
            if (
                isinstance(record_id, int)
                and not isinstance(record_id, bool)
                and record_id > highest
            ):
                highest = record_id
        return highest
    except Exception as exc:
        raise FileManifestError(
            f"Manifest record IDs cannot be inspected in '{manifest_path}': {exc}"
        ) from exc


def _as_int(value: Any, default: int) -> int:
    """Coerce a state value to int, falling back to the supplied default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
