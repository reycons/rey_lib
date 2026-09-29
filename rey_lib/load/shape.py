"""The load shape: routing derived from where the records come from and where they go.

``load_shape`` is not a third thing a load is told. It is a relation over the
two canonical objects that ARE told -- the Source's selected kind and the
Target's -- and it lives here, beside both, because it belongs to neither.

**ROUTING ONLY, NEVER VALIDITY.** Only the two selected kinds are read. Nothing
here validates, checks that a connection, table or path is complete, or decides
whether the load can run: an incomplete Source or Target may still have a shape,
and saying whether it can execute is each object's own ``validate``.

A pair the loader has no shape for answers None. None is invented.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.load.source import Source
from rey_lib.load.target import Target

__all__ = ["LOAD_SHAPES", "derived_load_shape"]

#: (Source kind, Target kind) -> the loader's load shape for that movement.
LOAD_SHAPES: dict[tuple[str, str], str] = {
    ("file", "database"): "direct",
    ("database", "database"): "query",
    ("sql_file", "database"): "query_file",
    ("manifest", "database"): "manifest",
    ("database", "file"): "query_to_file",
}


def derived_load_shape(source: Source, target: Target) -> Optional[str]:
    """The load shape for the two selected kinds, or None where there is none."""
    return LOAD_SHAPES.get((source.selected_kind(), target.selected_kind()))
