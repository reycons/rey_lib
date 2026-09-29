"""What the inspection builders are given, read from the three canonical objects.

``preview_load`` and ``inspect_load_endpoints`` take their ends as keyword
strings, and stay as they are. This is the one place the selected Source,
Transform and Target become those arguments -- beside the objects, so no entry
point works it out for itself and no browser supplies it.

**A HALF-NAMED LOAD STAYS HALF-NAMED.** Only the selected configurations are
read; nothing is validated. An end nobody has filled in yet becomes an empty
string, which the builders already treat as "not named", and they answer empty
for it -- looking at a load being set up is not an error.

**A NAMED END THAT CANNOT BE READ IS REFUSED**, as the load would refuse it: a
SQL file that is missing or empty, a transform declaration that cannot be read.
Files are read here, in Python -- never by whoever asked.

    Source   file      -> file, file_type
             database  -> statement, source_connection
             sql_file  -> statement (the file's text), source_connection
             manifest  -> nothing yet: the builders take no governed identity
    Target   database  -> destination (the table), connection
             file      -> out_file
    Transform          -> transform: the declaration execution sees, or None
"""

from __future__ import annotations

from typing import Any

from rey_lib.load.source import Source, _held, _statement_in
from rey_lib.load.target import Target
from rey_lib.load.transform import TRANSFORM_KINDS, Transform

__all__ = ["inspection_arguments"]


def inspection_arguments(source: Source, transform: Transform, target: Target) -> dict[str, Any]:
    """The builders' keyword arguments for the three objects' selected configurations.

    Returns:
        ``file``, ``file_type``, ``statement``, ``source_connection``,
        ``destination``, ``connection`` and ``out_file`` as text -- empty where
        not named -- and ``transform``, the executed declaration or None.
        ``preview_load`` takes the source half and the declaration.

    Raises:
        ConfigError: Where a named end cannot be read.
    """
    return {
        **_source_arguments(source),
        **_target_arguments(target),
        "transform": _transform_declaration(transform),
    }


def _text(value: Any) -> str:
    """A configured value as the builders take it: text, empty where none."""
    return str(value) if _held(value) else ""


def _source_arguments(source: Source) -> dict[str, str]:
    """The source half: a file by path, or a statement on a connection."""
    held = source.configuration()
    kind = source.selected_kind()
    arguments = {"file": "", "file_type": "", "statement": "", "source_connection": ""}
    if kind == "file":
        arguments["file"] = _text(held.get("file"))
        arguments["file_type"] = _text(held.get("file-type"))
    elif kind == "database":
        arguments["statement"] = _text(held.get("statement"))
        arguments["source_connection"] = _text(held.get("source-connection"))
    elif kind == "sql_file":
        # READ WHERE NAMED, refused as the load refuses a missing or empty one.
        if _held(held.get("sql-file")):
            arguments["statement"] = _statement_in(held["sql-file"])
        arguments["source_connection"] = _text(held.get("source-connection"))
    # manifest: nothing yet -- the builders take no governed identity.
    return arguments


def _target_arguments(target: Target) -> dict[str, str]:
    """The target half: a table on a connection, or a file by path."""
    held = target.configuration()
    arguments = {"destination": "", "connection": "", "out_file": ""}
    if target.selected_kind() == "file":
        arguments["out_file"] = _text(held.get("out-file"))
    else:
        arguments["destination"] = _text(held.get("table"))
        arguments["connection"] = _text(held.get("connection"))
    return arguments


def _transform_declaration(transform: Transform) -> Any:
    """What execution would see, or None where nothing is declared yet.

    A kind whose declaration is not named yet is half-named, not wrong, and is
    inspected as identity -- the rows as they came. Read from the selected
    kind's own required fields, never through ``validate``.
    """
    kind = next(one for one in TRANSFORM_KINDS if one.id == transform.selected_kind())
    held = transform.configuration()
    if not all(_held(held.get(name)) for name in kind.required):
        return None
    return transform.executed_declaration()
