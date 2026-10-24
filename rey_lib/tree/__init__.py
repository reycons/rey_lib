"""The canonical Tree objects and the library traversal they use (backlog 1036, 1054, 1218).

Defined here and hosted live by tree_app (#1218); one implementation of each:

    persisted        tree.tree_vw, the governed Tree state (groups, roots, nodes)
    composition      the groups a placement surfaces, the roots a group exposes, a node's children
    declarations     what a node type declares, the declaration an interaction
                     names, and the unsigned identity it binds a record to
    TreeProjection   one reader's live Tree: admitted records and source state;
                     public_node states a node outside the Tree (PUBLIC_NODE_FIELDS)
    inspection       what may be disclosed of a node, and how far it may expand
    TreeTraversal    library traversal: route a node to its source, read, cap, admit
    sources          the declared non-persisted source names, and their adapters

Not here: the web representation of the Tree (rey_bff), signing an opening for a
browser (rey_bff), and drawing the Tree (rey_console).
"""

from __future__ import annotations

from typing import Any

from rey_lib.tree.collection import CollectionDataObject
from rey_lib.tree.composition import RUNTIME_BINDINGS, tree_children, tree_groups, tree_roots
from rey_lib.tree.database import DatabaseDataObject
from rey_lib.tree.declarations import (
    NODE_TYPE_ACTION_VIEW_NAME,
    NodeTypeActionDataObject,
    declared_fact,
    interacted_declaration,
    offered,
    offered_actions,
)
from rey_lib.tree.folder import FileDataObject, folder_children
from rey_lib.tree.jsonl import JsonlDataObject
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
from rey_lib.tree.projection import PUBLIC_NODE_FIELDS, TreeProjection, public_node
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
    collections = CollectionDataObject()
    return {
        FOLDER_SOURCE: FileDataObject(),
        DATABASE_SOURCE: DatabaseDataObject(),
        JSONL_SOURCE: JsonlDataObject(),
        # One object under two names: which collection to read is the
        # configuration's (backlog 1059).
        APPLICATION_SOURCE: collections,
        TOOL_SOURCE: collections,
    }


__all__ = [
    "APPLICATION_SOURCE",
    "CollectionDataObject",
    "DATABASE_SOURCE",
    "DECLARED_SOURCES",
    "DatabaseDataObject",
    "EXPANSION_LIMIT",
    "FOLDER_SOURCE",
    "FileDataObject",
    "INSPECTED_FIELDS",
    "JSONL_SOURCE",
    "JsonlDataObject",
    "NODE_TYPE_ACTION_VIEW_NAME",
    "NodeTypeActionDataObject",
    "PUBLIC_NODE_FIELDS",
    "RUNTIME_BINDINGS",
    "TOOL_SOURCE",
    "TREE_CONNECTION",
    "TREE_SCHEMA",
    "TREE_VIEW_NAME",
    "TreeDataObject",
    "TreeDataRow",
    "TreeProjection",
    "TreeTraversal",
    "declared_fact",
    "expand",
    "folder_children",
    "inspected",
    "interacted_declaration",
    "offered",
    "offered_actions",
    "persisted_tree",
    "public_node",
    "rey_lib_sources",
    "subject",
    "tree_children",
    "tree_groups",
    "tree_roots",
]
