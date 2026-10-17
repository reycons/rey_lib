"""The declared non-persisted tree sources, by name -- stated once (backlog 1054).

A record names the source of its children in ``tree_object_key``. A record naming
one of these is answered by that source; a record naming nothing, or anything
else, is persisted -- its children are rows of the tree view (``file_manifest``
and ``run_history`` roots are persisted this way).

Names only. Each source's implementation lives with whatever can read it, and a
process that has no implementation for a declared source refuses to expand its
records rather than reading them as persisted.
"""

from __future__ import annotations

__all__ = [
    "APPLICATION_SOURCE",
    "DATABASE_SOURCE",
    "DECLARED_SOURCES",
    "FOLDER_SOURCE",
    "JSONL_SOURCE",
    "TOOL_SOURCE",
]

FOLDER_SOURCE = "folder"
DATABASE_SOURCE = "database"
JSONL_SOURCE = "jsonl"
APPLICATION_SOURCE = "applications"
TOOL_SOURCE = "tools"

#: Every declared non-persisted source.
DECLARED_SOURCES = frozenset({
    FOLDER_SOURCE, DATABASE_SOURCE, JSONL_SOURCE, APPLICATION_SOURCE, TOOL_SOURCE,
})
