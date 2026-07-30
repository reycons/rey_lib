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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "FileManifestError",
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
    if not isinstance(record, dict):
        raise FileManifestError("A manifest record must be a JSON object.")
    if "record_id" in record:
        raise FileManifestError(
            "A manifest record must not supply 'record_id'; the manifest writer "
            "owns durable sequencing."
        )

    manifest_path = resolve_file_manifest_path(ctx)
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileManifestError(
            f"Manifest directory cannot be created for '{manifest_path}': {exc}"
        ) from exc

    with _ManifestLock(manifest_path):
        state = _load_state(manifest_path)
        record_id = int(state[_LAST_RECORD_ID]) + 1
        sequenced = {"record_id": record_id, **record}

        # Imported lazily because the rey_lib.files package eagerly loads
        # file_utils, which imports this logging layer — a module-level import
        # would form a cycle (SGC_Rey_Lib_Primitive_File_IO_Layer).
        from rey_lib.files import primitive_file_io

        try:
            primitive_file_io.append_jsonl(manifest_path, sequenced)
        except (OSError, TypeError, ValueError) as exc:
            raise FileManifestError(
                f"Manifest record could not be appended to '{manifest_path}': {exc}"
            ) from exc

        _commit_state(manifest_path, record_id)

    return record_id


@contextmanager
def file_manifest_write_boundary(ctx: Any) -> Iterator[Path]:
    """Yield the governed manifest under its shared exclusive write lock.

    Domain owners may perform a validated atomic rewrite while this boundary is
    held. On successful exit, shared sequencing state is synchronized to the
    highest retained record ID, preserving intentional ID gaps.
    """
    manifest_path = resolve_file_manifest_path(ctx)
    with _ManifestLock(manifest_path):
        yield manifest_path
        _commit_state(manifest_path, _highest_record_id(manifest_path))


class _ManifestLock:
    """Exclusive advisory lock over one manifest's complete critical section."""

    def __init__(self, manifest_path: Path) -> None:
        """Record the manifest whose companion lock file will be held."""
        self._lock_path = manifest_lock_path(manifest_path)
        self._handle: Any = None

    def __enter__(self) -> "_ManifestLock":
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

    def __exit__(self, *exc_info: Any) -> None:
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
    """Persist the committed record id and the manifest size it corresponds to."""
    from rey_lib.files import primitive_file_io

    state = {
        _LAST_RECORD_ID: int(record_id),
        _MANIFEST_SIZE_BYTES: _manifest_size(manifest_path),
    }
    try:
        primitive_file_io.atomic_write_text(
            manifest_state_path(manifest_path), json.dumps(state)
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
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            highest = 0
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                record_id = record.get("record_id") if isinstance(record, dict) else None
                if (
                    isinstance(record_id, int)
                    and not isinstance(record_id, bool)
                    and record_id > highest
                ):
                    highest = record_id
            return highest
    except (OSError, ValueError) as exc:
        raise FileManifestError(
            f"Manifest record IDs cannot be inspected in '{manifest_path}': {exc}"
        ) from exc


def _as_int(value: Any, default: int) -> int:
    """Coerce a state value to int, falling back to the supplied default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
