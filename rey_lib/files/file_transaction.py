"""
Transactional file-set publication for rey_lib.

Composes the primitive atomic write into an all-or-nothing publication of a
bounded set of files
(SGC_Wolff_Popper_Structural_Onboarding_Runtime_Acceptance_Correction,
REQ-019 through REQ-022). This module owns staging, explicit collision
handling, commit ordering, and rollback. Callers retain ownership of artifact
selection, naming, content, evidence, and publication policy; no application
semantics belong here.

Every member is staged as a temporary file inside its own destination's parent
directory, so each commit is a same-filesystem ``os.replace`` and never a
cross-device copy. Nothing reaches a final destination until every member has
been staged successfully. A failure during commit restores every replaced
destination and removes every newly created one, leaving the destination set
exactly as it was found.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

__all__ = [
    "FileSetCollisionError",
    "FileSetCommitError",
    "FileSetMember",
    "FileSetTransactionError",
    "PublishedFileSet",
    "publish_file_set",
]

# Explicit destination-collision policies. There is no implicit default that
# silently overwrites an existing file.
_COLLISION_POLICIES = ("fail", "replace")


class FileSetTransactionError(Exception):
    """Base error for transactional file-set publication."""


class FileSetCollisionError(FileSetTransactionError):
    """Raised when a destination already exists under the ``fail`` policy."""


class FileSetCommitError(FileSetTransactionError):
    """Raised when a commit failed after the destination set was rolled back."""


@dataclass(frozen=True)
class FileSetMember:
    """
    One member of a publication set: a destination and its exact content.

    Exactly one of ``text`` or ``data`` must be supplied. ``encoding`` applies
    only to ``text``.
    """

    destination: Path
    text: str | None = None
    data: bytes | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True)
class PublishedFileSet:
    """The committed result of one successful publication."""

    committed: tuple[Path, ...]
    created: tuple[Path, ...]
    replaced: tuple[Path, ...]


@dataclass
class _StagedMember:
    """Internal per-member commit state used for rollback."""

    destination: Path
    staged_path: Path
    existed: bool
    backup_path: Path | None = field(default=None)
    committed: bool = field(default=False)


def publish_file_set(
    members: Sequence[FileSetMember],
    *,
    on_collision: str = "fail",
) -> PublishedFileSet:
    """
    Publish a complete set of files atomically at set scope.

    All members are staged before any destination is touched. Destinations are
    then committed in deterministic path order. If any commit fails, every
    replaced destination is restored and every newly created destination is
    removed before the failure is raised.

    Parameters
    ----------
    members : Sequence[FileSetMember]
        The complete publication set. Must be non-empty with unique
        destinations.
    on_collision : str
        ``"fail"`` rejects the publication when any destination already exists.
        ``"replace"`` replaces existing destinations, restoring them on
        rollback. Existing destinations are never silently overwritten.

    Returns
    -------
    PublishedFileSet
        The committed destinations, split into newly created and replaced.

    Raises
    ------
    FileSetCollisionError
        A destination already exists and ``on_collision`` is ``"fail"``.
    FileSetCommitError
        A commit failed; the destination set was restored to its prior state.
    FileSetTransactionError
        The set is invalid, or staging failed before any commit.
    """
    if on_collision not in _COLLISION_POLICIES:
        raise FileSetTransactionError(
            f"Unknown collision policy '{on_collision}'; "
            f"expected one of {', '.join(_COLLISION_POLICIES)}."
        )
    ordered = _validate(members)
    _check_collisions(ordered, on_collision)

    staged = _stage_all(ordered)
    return _commit_all(staged)


def _validate(members: Sequence[FileSetMember]) -> list[tuple[Path, bytes]]:
    """Validate the set and return deterministically ordered destination/payload pairs."""
    if not members:
        raise FileSetTransactionError("A publication set must contain at least one member.")

    resolved: list[tuple[Path, bytes]] = []
    seen: set[Path] = set()
    for index, member in enumerate(members):
        # Exactly one content source keeps the member unambiguous.
        if (member.text is None) == (member.data is None):
            raise FileSetTransactionError(
                f"File-set member {index} must supply exactly one of 'text' or 'data'."
            )
        destination = Path(member.destination).expanduser()
        if not destination.is_absolute():
            raise FileSetTransactionError(
                f"File-set destination must be absolute: '{destination}'."
            )
        if destination in seen:
            raise FileSetTransactionError(
                f"File-set destination is declared more than once: '{destination}'."
            )
        seen.add(destination)
        payload = (
            member.text.encode(member.encoding)
            if member.text is not None
            else member.data
        )
        resolved.append((destination, payload or b""))

    # Deterministic commit order makes a partial commit reproducible in evidence.
    resolved.sort(key=lambda item: str(item[0]))
    return resolved


def _check_collisions(ordered: list[tuple[Path, bytes]], on_collision: str) -> None:
    """Apply the explicit collision policy across the whole set before staging."""
    existing: list[Path] = []
    for destination, _payload in ordered:
        if not destination.exists():
            continue
        # A non-file destination can never be replaced by an atomic rename.
        if not destination.is_file():
            raise FileSetTransactionError(
                f"File-set destination exists and is not a regular file: '{destination}'."
            )
        existing.append(destination)

    if existing and on_collision == "fail":
        listed = ", ".join(f"'{path}'" for path in existing)
        raise FileSetCollisionError(
            f"Publication rejected: {len(existing)} destination(s) already exist: {listed}."
        )


def _stage_all(ordered: list[tuple[Path, bytes]]) -> list[_StagedMember]:
    """Stage every member beside its destination; discard all staging on failure."""
    staged: list[_StagedMember] = []
    try:
        for destination, payload in ordered:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Record prior existence before staging so rollback knows whether the
            # destination must be restored or removed.
            existed = destination.is_file()
            staged.append(
                _StagedMember(
                    destination=destination,
                    staged_path=_stage(destination, payload),
                    existed=existed,
                )
            )
    except OSError as exc:
        _discard_staging(staged)
        raise FileSetTransactionError(
            f"Publication staging failed before any destination was committed: {exc}"
        ) from exc
    return staged


def _commit_all(staged: list[_StagedMember]) -> PublishedFileSet:
    """Commit every staged member, rolling the whole set back on any failure."""
    try:
        for item in staged:
            if item.existed:
                item.backup_path = _backup(item.destination)
            os.replace(item.staged_path, item.destination)
            item.committed = True
    except OSError as exc:
        _rollback(staged)
        raise FileSetCommitError(
            f"Publication commit failed and was rolled back: {exc}"
        ) from exc

    # The set is committed; backups are no longer recoverable state.
    for item in staged:
        if item.backup_path is not None:
            item.backup_path.unlink(missing_ok=True)

    return PublishedFileSet(
        committed=tuple(item.destination for item in staged),
        created=tuple(item.destination for item in staged if not item.existed),
        replaced=tuple(item.destination for item in staged if item.existed),
    )


def _stage(destination: Path, payload: bytes) -> Path:
    """Write ``payload`` to a temporary file beside ``destination`` and return its path."""
    handle_id, staged_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".staged",
    )
    staged_path = Path(staged_name)
    try:
        with os.fdopen(handle_id, "wb") as handle:
            handle.write(payload)
    except BaseException:
        # Never leave a partial staged artifact behind.
        staged_path.unlink(missing_ok=True)
        raise
    if staged_path.stat().st_size != len(payload):
        staged_path.unlink(missing_ok=True)
        raise FileSetTransactionError(
            f"Staged content for '{destination}' is incomplete."
        )
    return staged_path


def _backup(destination: Path) -> Path:
    """Move an existing destination aside so a failed commit can restore it."""
    handle_id, backup_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".backup",
    )
    os.close(handle_id)
    os.replace(destination, backup_name)
    return Path(backup_name)


def _rollback(staged: list[_StagedMember]) -> None:
    """Restore replaced destinations, remove created ones, and discard staging."""
    for item in staged:
        if item.committed:
            if item.backup_path is not None:
                os.replace(item.backup_path, item.destination)
            else:
                item.destination.unlink(missing_ok=True)
            continue
        # Not committed: a backup may still have been taken this iteration.
        if item.backup_path is not None and not item.destination.exists():
            os.replace(item.backup_path, item.destination)
        item.staged_path.unlink(missing_ok=True)


def _discard_staging(staged: list[_StagedMember]) -> None:
    """Remove staged temporary files after a staging failure."""
    for item in staged:
        item.staged_path.unlink(missing_ok=True)
