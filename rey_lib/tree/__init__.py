"""The tree's non-presentation mechanics (backlog 1036, 1054).

Shared by every consumer of the tree -- the Console, which presents it, and an AI
inspection run in ai_app -- so there is one implementation of each:

    TreeProjection   one reader's held records; its lifetime is the consumer's
    TreeTraversal    route a node to its source, read, cap, admit
    persisted        tree.tree_vw, the persisted tree's data object
    sources          the declared non-persisted source names
    inspection       what a model may see of a node, and how far it may expand

Not a tree model: what a node means, what it can do and how it is drawn are not
here.
"""

from __future__ import annotations

from typing import Any

from rey_lib.tree.database import DatabaseDataObject
from rey_lib.tree.folder import FileDataObject, folder_children
from rey_lib.tree.inspection import (
    EXPANSION_LIMIT,
    INSPECTED_FIELDS,
    expand,
    inspected,
    subject,
)
from rey_lib.tree.persisted import (
    TREE_CONNECTION,
    TREE_SCHEMA,
    TREE_VIEW_NAME,
    TreeDataObject,
    TreeDataRow,
    persisted_tree,
)
from rey_lib.tree.projection import TreeProjection
from rey_lib.tree.sources import (
    APPLICATION_SOURCE,
    DATABASE_SOURCE,
    DECLARED_SOURCES,
    FOLDER_SOURCE,
    JSONL_SOURCE,
    TOOL_SOURCE,
)
from rey_lib.tree.traversal import TreeTraversal


def rey_lib_sources() -> dict[str, Any]:
    """The declared tree sources rey_lib implements, by name (backlog 1056, D3).

    The one statement of which sources have crossed into rey_lib: a consumer
    that reads the tree outside the Console builds its traversal from these and
    names no source itself. A declared source not here is one this process
    cannot read, and the traversal refuses it.
    """
    return {FOLDER_SOURCE: FileDataObject(), DATABASE_SOURCE: DatabaseDataObject()}


__all__ = [
    "APPLICATION_SOURCE",
    "DATABASE_SOURCE",
    "DECLARED_SOURCES",
    "DatabaseDataObject",
    "EXPANSION_LIMIT",
    "FOLDER_SOURCE",
    "FileDataObject",
    "INSPECTED_FIELDS",
    "JSONL_SOURCE",
    "TOOL_SOURCE",
    "TREE_CONNECTION",
    "TREE_SCHEMA",
    "TREE_VIEW_NAME",
    "TreeDataObject",
    "TreeDataRow",
    "TreeProjection",
    "TreeTraversal",
    "expand",
    "folder_children",
    "inspected",
    "persisted_tree",
    "rey_lib_sources",
    "subject",
]
