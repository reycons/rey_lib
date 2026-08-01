"""
Primitive file I/O for rey_lib.

The lowest-level filesystem write/append primitives, with no Rey application
semantics. This module sits *below* both ``file_utils`` and ``log_utils`` so
either foundational layer can perform durable writes without importing the
other (SGC_Rey_Lib_Primitive_File_IO_Layer):

    primitive_file_io
       ^             ^
    file_utils      log_utils

It knows nothing about run IDs, run timestamps, artifacts, logs, record types,
workflows, pipelines, or apps, and it imports nothing from ``file_utils``,
``log_utils``, workflow, app, or console modules — only the standard library.
Low-level failures are surfaced to callers as standard ``OSError``; this layer
neither logs nor swallows them. Applications must keep using ``file_utils`` or
``log_utils``, not this module.

Write persistence tiers
-----------------------
Three named guarantees. A tier states what it provides and never silently
degrades to a weaker one when the platform cannot deliver it.

``visibility-atomic``
    :func:`atomic_write_text`, :func:`atomic_write_bytes`. Staged write then
    atomic installation, so a reader never observes a partial file. No
    persistence claim: the bytes may still be in page cache when the rename
    happens.

``flushed``
    :func:`flushed_write_bytes`. Adds ``os.fsync`` on the file and on the
    destination directory where supported. Improves persistence. It does *not*
    claim survival of drive-cache loss, because on macOS ``fsync(2)`` does not
    flush the device write cache.

``maximum-durability``
    :func:`durable_write_bytes`. The platform's strongest available flush --
    ``F_FULLFSYNC`` on macOS -- then directory synchronization. Opt-in, and it
    raises rather than falling back when the platform cannot provide the
    documented guarantee.

Choosing a tier is the caller's decision, because only the caller knows whether
its artifact is regenerable or governed evidence. Most artifacts are
regenerable and correctly use the default tier.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

try:  # POSIX only; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "StagedWrite",
    "durable_write_bytes",
    "flushed_write_bytes",
    "stage_write_bytes",
    "write_text",
    "write_bytes",
    "append_text",
    "append_jsonl",
    "render_jsonl_line",
    "write_jsonl_file",
    "atomic_write_text",
    "atomic_write_bytes",
]


def _ensure_parent(path: Path, create_parents: bool) -> None:
    """Create the parent directory for ``path`` when ``create_parents`` is set."""
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)


def write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Write ``text`` to ``path``, replacing any existing file.

    Parameters
    ----------
    path : Path | str
        Destination file path.
    text : str
        Text content to write.
    encoding : str
        Character encoding. Defaults to UTF-8.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    target.write_text(text, encoding=encoding)
    return target


def write_bytes(
    path: Path | str,
    data: bytes,
    *,
    create_parents: bool = True,
) -> Path:
    """Write ``data`` bytes to ``path``, replacing any existing file.

    Parameters
    ----------
    path : Path | str
        Destination file path.
    data : bytes
        Byte content to write.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    target.write_bytes(data)
    return target


def append_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Append ``text`` to ``path`` without truncating existing content.

    Parameters
    ----------
    path : Path | str
        Destination file path.
    text : str
        Text content to append.
    encoding : str
        Character encoding. Defaults to UTF-8.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    with target.open("a", encoding=encoding) as handle:
        handle.write(text)
    return target


def render_jsonl_line(record: Any) -> str:
    """Return one record as its canonical JSONL line, without the newline.

    The single encoding for this format. Every writer uses it so that a record
    holding the same content is the same bytes wherever it was written — an
    append-only store whose rows are re-encoded by a later rewrite is no longer
    byte-comparable evidence.

    ``default=str`` stringifies a non-JSON-native value rather than raising, so
    a caller is never silently unable to record something.
    """
    return json.dumps(record, default=str)


def write_jsonl_file(
    path: Path | str,
    records: Any,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Atomically write every record as one JSONL file, replacing any existing.

    The whole-file counterpart to :func:`append_jsonl`, for an artifact built
    in one pass rather than accumulated. Both render through
    :func:`render_jsonl_line`, so a record holding the same content is the same
    bytes whichever writer produced it.

    The write is atomic: a reader never observes a partially written artifact.
    """
    lines = "".join(render_jsonl_line(record) + "\n" for record in records)
    return atomic_write_text(
        path, lines, encoding=encoding, create_parents=create_parents
    )


def append_jsonl(
    path: Path | str,
    record: Any,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Append one ``record`` as a single JSON line (object-per-line) to ``path``.

    The record is serialised with ``default=str`` so non-JSON-native values are
    stringified rather than raising, and exactly one newline-terminated JSON
    object is written per call.

    Parameters
    ----------
    path : Path | str
        Destination JSONL file path.
    record : Any
        One JSON-serialisable record.
    encoding : str
        Character encoding. Defaults to UTF-8.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    line = render_jsonl_line(record) + "\n"
    # newline="" so the newline written is one byte on every platform. Without
    # it, text mode translates to os.linesep, which would make this record CRLF
    # on Windows in a file whose bytes are hashed. A byte-semantics fix, not a
    # persistence change: this append makes no durability claim, and an
    # acknowledged append is not power-loss durable.
    with target.open("a", encoding=encoding, newline="") as handle:
        handle.write(line)
    return target


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Write ``text`` to ``path`` atomically. Tier: visibility-atomic.

    Readers never observe a partially written file: the content is written to a
    temporary file in the destination directory and atomically moved into place.

    No persistence claim. The bytes may still be in page cache when the rename
    happens, so this survives a crashed process but not a lost machine. Ask for
    :func:`flushed_write_bytes` or :func:`durable_write_bytes` when the artifact
    is governed evidence rather than something regenerable.

    Parameters
    ----------
    path : Path | str
        Destination file path.
    text : str
        Text content to write.
    encoding : str
        Character encoding. Defaults to UTF-8.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    return _atomic_write(target, text.encode(encoding))


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    create_parents: bool = True,
) -> Path:
    """Write ``data`` to ``path`` atomically. Tier: visibility-atomic.

    As :func:`atomic_write_text`, for content the caller has already encoded.
    No persistence claim.

    Parameters
    ----------
    path : Path | str
        Destination file path.
    data : bytes
        Byte content to write.
    create_parents : bool
        Create missing parent directories before writing. Defaults to True.

    Returns
    -------
    Path
        The destination path.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    return _atomic_write(target, data)


class StagedWrite:
    """Content written beside its destination but not yet installed.

    The point of staging is the gap: a caller can read the staged bytes back and
    reject them before anything reaches the destination. Collapsing write and
    install into one call removes that gate, and for governed evidence the gate
    is the correctness guarantee -- it is what catches a re-encoding that
    reparses differently from what was intended.

    Use as a context manager. Leaving the block without calling :meth:`install`
    discards the staged file, including when an exception unwinds through it, so
    a rejected write never leaves a temporary artifact behind.
    """

    def __init__(self, path: Path, destination: Path, tier: str) -> None:
        self.path = path
        self.destination = destination
        self.tier = tier
        self.installed = False

    def __enter__(self) -> "StagedWrite":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.discard()

    def install(self) -> Path:
        """Atomically move the staged file onto its destination.

        Applies the persistence of the tier the write was staged with, so the
        directory is synchronized here rather than at write time -- a rename is
        not durable until its directory is.
        """
        os.replace(self.path, self.destination)
        self.installed = True
        if self.tier in (_TIER_FLUSHED, _TIER_MAXIMUM):
            _sync_directory(self.destination.parent, required=self.tier == _TIER_MAXIMUM)
        return self.destination

    def discard(self) -> None:
        """Remove the staged file if it was never installed."""
        if self.installed:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass


def stage_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    tier: str = "visibility-atomic",
    create_parents: bool = True,
) -> StagedWrite:
    """Write ``data`` beside ``path`` without installing it. Returns the handle.

    The staged file is a sibling of the destination, so installing it is a
    same-filesystem rename and never a cross-device copy.

    ``tier`` selects the persistence applied to the staged content and, on
    install, to the rename. See the module docstring; the tier is named rather
    than inferred because only the caller knows whether the artifact is governed
    evidence.
    """
    target = Path(path)
    _ensure_parent(target, create_parents)
    staged = _write_staged(target, data, _checked_tier(tier))
    return StagedWrite(staged, target, _checked_tier(tier))


def flushed_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    create_parents: bool = True,
) -> Path:
    """Write ``data`` to ``path``. Tier: flushed.

    Flushes the file with ``os.fsync`` and synchronizes the destination
    directory where the platform supports it, then installs atomically.

    This is stronger than visibility-atomic and is still not a durability
    guarantee. On macOS ``fsync(2)`` does not flush the drive write cache, so
    acknowledged data can be lost on power failure. Call
    :func:`durable_write_bytes` when that matters.
    """
    return _tiered_write(path, data, _TIER_FLUSHED, create_parents)


def durable_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    create_parents: bool = True,
) -> Path:
    """Write ``data`` to ``path``. Tier: maximum-durability.

    Uses the strongest flush the platform provides -- ``F_FULLFSYNC`` on macOS,
    ``os.fsync`` elsewhere -- then synchronizes the destination directory, then
    installs atomically.

    Raises ``OSError`` if the platform cannot provide the documented guarantee.
    It never falls back to a weaker tier, because a caller that asked for this
    tier would otherwise be told its data is safe when it is not.
    """
    return _tiered_write(path, data, _TIER_MAXIMUM, create_parents)


def _atomic_write(target: Path, data: bytes) -> Path:
    """Write ``data`` to a temp file in the target directory, then atomically move it."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        # Never leave a partial temp artifact behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


_TIER_VISIBILITY = "visibility-atomic"
_TIER_FLUSHED = "flushed"
_TIER_MAXIMUM = "maximum-durability"
_TIERS = (_TIER_VISIBILITY, _TIER_FLUSHED, _TIER_MAXIMUM)


def _checked_tier(tier: str) -> str:
    """Return ``tier`` if it names a real guarantee, else raise."""
    if tier not in _TIERS:
        raise ValueError(
            f"Unknown write tier {tier!r}; expected one of {', '.join(_TIERS)}."
        )
    return tier


def _tiered_write(
    path: Path | str,
    data: bytes,
    tier: str,
    create_parents: bool,
) -> Path:
    """Stage ``data`` at the requested tier and install it."""
    target = Path(path)
    _ensure_parent(target, create_parents)
    staged = _write_staged(target, data, tier)
    try:
        os.replace(staged, target)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    _sync_directory(target.parent, required=tier == _TIER_MAXIMUM)
    return target


def _write_staged(target: Path, data: bytes, tier: str) -> Path:
    """Write ``data`` to a sibling temp file, flushed to the requested tier."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            if tier in (_TIER_FLUSHED, _TIER_MAXIMUM):
                handle.flush()
                _flush_file(handle.fileno(), tier)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def _flush_file(fileno: int, tier: str) -> None:
    """Flush one open file to the strength the tier promises.

    ``maximum-durability`` needs F_FULLFSYNC on macOS: plain ``fsync`` returns
    once the data reaches the drive, not once the drive has committed it. When
    that call is unavailable the tier cannot keep its promise, so it raises
    rather than quietly delivering the weaker guarantee.
    """
    if tier != _TIER_MAXIMUM:
        os.fsync(fileno)
        return
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None) if fcntl is not None else None
    if full_fsync is not None:
        fcntl.fcntl(fileno, full_fsync)
        return
    if sys.platform == "darwin":
        raise OSError(
            "maximum-durability requires F_FULLFSYNC on macOS and it is "
            "unavailable; refusing to report a guarantee this platform cannot "
            "provide."
        )
    os.fsync(fileno)


def _sync_directory(directory: Path, *, required: bool) -> None:
    """Synchronize ``directory`` so a rename within it is persisted.

    A rename is not durable until its directory is. Some platforms and
    filesystems cannot do this; ``flushed`` tolerates that because it makes no
    power-loss claim, while ``maximum-durability`` raises rather than overstate
    what happened.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        if required:
            raise
        return
    try:
        os.fsync(fd)
    except OSError:
        if required:
            raise
    finally:
        os.close(fd)
