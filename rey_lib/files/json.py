"""Generic JSON mechanics, for a file or a value already in hand.

One set of primitives for parsing, rendering, reading, and writing JSON, so a
document holding the same content is the same bytes wherever it was produced
and the same error wherever it failed.

What this module owns
---------------------
Decoding text, encoding values, reading and atomically writing files, the
rendering modes a caller may ask for, and errors that say which file and which
position failed. Type assertions are available on request — a caller expecting
an object should not have to check for one itself.

What it does not own
--------------------
Schema validation, which record types matter, whether a malformed record is
skipped or fatal, how a subprocess prints its output, and how anything is
displayed. Those are the caller's, and stay there.

Rendering modes
---------------
``compact``   one line, the default for storage and transport
``pretty``    indented for a human reading the file
``canonical`` sorted keys and fixed separators, for hashing and comparison

Non-JSON-native values are stringified rather than raising, so a caller is
never silently unable to record something it holds.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any, Literal

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.primitive_file_io import atomic_write_text

__all__ = [
    "JsonReadError",
    "RenderMode",
    "parse_json_text",
    "read_json_file",
    "render_json",
    "write_json_file",
]

RenderMode = Literal["compact", "pretty", "canonical"]

_RENDER_MODES: dict[str, dict[str, Any]] = {
    "compact": {},
    "pretty": {"indent": 2, "sort_keys": True},
    "canonical": {"sort_keys": True, "separators": (",", ":")},
}


class JsonReadError(AppError):
    """Raised when JSON cannot be read, decoded, or is not the expected type."""


def parse_json_text(
    text: str,
    *,
    source: str = "",
    expect: type | tuple[type, ...] | None = None,
) -> Any:
    """Decode one JSON document from ``text``.

    Parameters
    ----------
    text : str
        The document to decode.
    source : str
        What the text came from — a path, a URL, a command. Used only to make
        the error say where the problem is; it is never opened.
    expect : type | tuple[type, ...] | None
        Assert the decoded value's type, for example ``dict`` when the caller
        requires an object. ``None`` accepts whatever the document holds.

    Returns
    -------
    Any
        The decoded value.

    Raises
    ------
    JsonReadError
        If the text is not valid JSON, carrying the line and column of the
        failure, or if it decodes to a type the caller did not expect.
    """
    try:
        value = _json.loads(text)
    except _json.JSONDecodeError as exc:
        raise JsonReadError(
            f"Invalid JSON{_at(source)} at line {exc.lineno} column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    return _asserted(value, expect, source)


def render_json(value: Any, *, mode: RenderMode = "compact") -> str:
    """Return ``value`` as JSON text in the requested mode.

    ``canonical`` is the mode to use whenever the result will be hashed or
    compared: it fixes key order and separators, so equal content is equal
    bytes. ``pretty`` is for a file a person will read. ``compact`` is the
    default for storage and transport.
    """
    if mode not in _RENDER_MODES:
        raise JsonReadError(
            f"Unknown JSON render mode '{mode}'; expected one of "
            f"{', '.join(sorted(_RENDER_MODES))}."
        )
    return _json.dumps(value, default=str, **_RENDER_MODES[mode])


def read_json_file(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    expect: type | tuple[type, ...] | None = None,
) -> Any:
    """Read and decode one JSON file.

    The path is reported in any decode failure, so a caller never has to
    reconstruct which file was bad. Reading goes through the shared file
    boundary; this is not an authorization boundary, and the caller resolves
    and authorizes the path.
    """
    source_path = Path(path)
    try:
        text = read_text_file(source_path, encoding=encoding)
    except (OSError, UnicodeError) as exc:
        # Bytes that will not decode are a read failure, not malformed JSON.
        # Reporting them as a syntax error would send a caller looking for a
        # bad brace in a file that never became text.
        raise JsonReadError(f"Cannot read '{source_path}': {exc}") from exc
    return parse_json_text(text, source=str(source_path), expect=expect)


def write_json_file(
    path: Path | str,
    value: Any,
    *,
    mode: RenderMode = "pretty",
    encoding: str = "utf-8",
    newline: bool = True,
    create_parents: bool = True,
) -> Path:
    """Atomically write ``value`` to ``path`` as JSON.

    The write is atomic: a reader never observes a partially written document.
    ``pretty`` is the default because a JSON file on disk is usually one a
    person will open; ask for ``canonical`` when the file's bytes are compared
    or hashed.
    """
    text = render_json(value, mode=mode)
    if newline:
        text += "\n"
    return atomic_write_text(
        path, text, encoding=encoding, create_parents=create_parents
    )


def _asserted(
    value: Any,
    expect: type | tuple[type, ...] | None,
    source: str,
) -> Any:
    """Return ``value`` when it satisfies ``expect``, or raise saying what it is."""
    if expect is None:
        return value
    candidates = expect if isinstance(expect, tuple) else (expect,)
    if not candidates or not all(isinstance(item, type) for item in candidates):
        raise JsonReadError(
            f"JSON expect must be a type or tuple of types, not {expect!r}."
        )
    if isinstance(value, expect):
        return value
    names = (
        expect.__name__
        if isinstance(expect, type)
        else " or ".join(item.__name__ for item in expect)
    )
    raise JsonReadError(
        f"JSON{_at(source)} must be {names}, not {type(value).__name__}."
    )


def _at(source: str) -> str:
    """Return a ' in <source>' fragment, or nothing when the caller gave none."""
    return f" in '{source}'" if source else ""
