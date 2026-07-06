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
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "write_text",
    "write_bytes",
    "append_text",
    "append_jsonl",
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
    line = json.dumps(record, default=str) + "\n"
    with target.open("a", encoding=encoding) as handle:
        handle.write(line)
    return target


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> Path:
    """Atomically write ``text`` to ``path`` via a temp file and ``os.replace``.

    Readers never observe a partially written file: the content is written to a
    temporary file in the destination directory and atomically moved into place.

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
    """Atomically write ``data`` bytes to ``path`` via a temp file and ``os.replace``.

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
