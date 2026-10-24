"""The canonical Tree's group, root and child composition (#1218).

Which governed Tree groups take part in a Tree, which roots that Tree exposes,
and what hangs beneath a node:

    tree_groups   installation + placement -> the governed groups surfaced there
    tree_roots    a group's governed roots -> the roots the Tree exposes
    tree_children a node's record -> the enabled children the traversal yields

All are Tree-domain behaviour: they turn governed Tree configuration into the
groups, roots and children of the Tree, and they belong with the canonical Tree objects
wherever those are hosted. Nothing here builds a web representation, signs an
opening or knows which surface shows the result.

**A root's configuration is resolved here.** The database stores the token
configuration wrote (``{configs}``); the source that stands at a root is handed a
place. Path tokens resolve through the installation's own PathResolver; the closed
set of runtime bindings (``connection``) is supplied by whoever composes the Tree
and is refused when absent rather than left blank.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Mapping

from rey_lib.db.connection import shared_connection
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import read_text_file
import rey_lib.tree as _tree
from rey_lib.tree.persisted import TREE_CONNECTION, persisted_tree
from rey_lib.tree.traversal import Assemble, TreeTraversal

__all__ = ["RUNTIME_BINDINGS", "tree_children", "tree_groups", "tree_roots"]

#: The packaged statements this module reads (package data, like inferred_architecture/sql).
_SQL_DIR: Final = Path(__file__).parent / "sql"

#: The configuration values a caller may supply when a Tree is composed.
#:
#: CLOSED, and that is the whole of its safety. A root is governed configuration:
#: if any key could be supplied per request, a caller could rewrite a path, the
#: collections a root is restricted to, or point one installation's root at
#: another's database. Only these names may be bound; everything else is ignored.
RUNTIME_BINDINGS: Final[frozenset[str]] = frozenset({"connection"})


def tree_groups(ctx: Any, placement: str) -> list[dict[str, Any]]:
    """The governed Tree groups the context's installation surfaces in one placement.

    Which groups, whether each is enabled, and their display order are the
    routine's answer (``tree.f_tree_tabs``); nothing here decides them again. Zero
    groups is an ordinary answer -- an installation may surface none there.

    Args:
        ctx: The installation context the Tree runs under; its installation
            (``ctx.installation.name``) is whose groups these are, and it
            resolves the Tree connection.
        placement: Where the Tree is asked for (``explorer``, ``loader_source``,
            ...). The vocabulary is the database's.

    Returns:
        One entry per group, in display order: ``tree_group_id``,
        ``tree_group_key``, ``label``, ``icon``, ``panel_key`` and
        ``panel_configuration`` (an empty mapping when none is declared).

    Raises:
        ConfigError: When no placement is named, or the context names no
            installation.
    """
    if not placement:
        raise ConfigError("A Tree's groups are asked for in a named placement.")
    installation = str(getattr(getattr(ctx, "installation", None), "name", "") or "")
    if not installation:
        raise ConfigError("A Tree's groups belong to an installation, and this context names none.")
    handle = shared_connection(ctx, TREE_CONNECTION).handle()
    rows = DatabaseQuery(read_text_file(_SQL_DIR / "tree_groups.sql"), {
        "installation": installation,
        "placement": placement,
    }).rows(handle)
    return [
        {**dict(row), "panel_configuration": dict(row.get("panel_configuration") or {})}
        for row in rows
    ]


def tree_roots(
    ctx: Any,
    tree_group_id: Any,
    bindings: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The roots one governed Tree group exposes, with their configuration resolved.

    Roots are persisted rows of the Tree (``tree.tree_vw``, ``row_type='root'``)
    in their declared order. A disabled root is not exposed. Each exposed root's
    ``tree_configuration`` is resolved (:func:`_resolved_configuration`), and a
    configuration that declares ``refresh`` states whether the root may be read
    again.

    Args:
        ctx: The resolved installation context.
        tree_group_id: The group whose roots are asked for.
        bindings: Runtime binding values for this composition; only the names in
            :data:`RUNTIME_BINDINGS` are read.

    Returns:
        The exposed root records, in declared order.

    Raises:
        ConfigError: When a root needs a runtime binding that was not supplied.
    """
    exposed: list[dict[str, Any]] = []
    for row in persisted_tree().rows(ctx, row_type="root", tree_group_id=tree_group_id):
        record = row.to_dict()
        if not _true(record.get("is_enabled")):
            continue
        record["tree_configuration"] = _resolved_configuration(
            ctx, record.get("tree_configuration"), bindings,
        )
        # Whether a place changes under a reader is a fact its configuration declares.
        if "refresh" in record["tree_configuration"]:
            record["refreshable"] = bool(record["tree_configuration"]["refresh"])
        exposed.append(record)
    return exposed


def tree_children(
    ctx: Any,
    record: dict[str, Any],
    projection: Any = None,
    fresh: bool = False,
    assemble: Assemble | None = None,
) -> list[dict[str, Any]]:
    """The enabled children of one node, whatever its source.

    The canonical traversal over the persisted Tree and the sources rey_lib
    implements (``TreeTraversal(persisted_tree(), rey_lib_sources())``): the
    record decides where its children come from, and the projection, when
    given, admits them and holds them for the next expansion. A disabled child
    is not exposed.

    Args:
        ctx: The installation context the source is read in.
        record: The parent, as the projection holds it.
        projection: The reader's projection of this Tree, if there is one.
        fresh: Read the source again rather than answer from held state.
        assemble: What the host adds to each admitted child before the
            projection holds it (``TreeTraversal``'s step), if anything.

    Returns:
        The enabled children, in the order their source gave them.
    """
    traversal = TreeTraversal(persisted_tree(), _tree.rey_lib_sources())
    return [
        child for child in traversal.children(ctx, record, projection, fresh, assemble)
        if _true(child.get("is_enabled"))
    ]


def _resolved_configuration(
    ctx: Any,
    declared: Any,
    bindings: Mapping[str, str] | None,
) -> dict[str, Any]:
    """A root's declared configuration, with every whole-token value turned into a value.

    Only a string that is exactly one ``{token}`` is resolved; mappings, lists and
    partial strings are carried as declared. A runtime binding is consulted first,
    so an authorized binding name is never handed to the path resolver; any other
    token resolves through the installation's PathResolver.

    Raises:
        ConfigError: When a runtime binding the configuration names was not supplied.
    """
    configuration = dict(declared or {})
    supplied = dict(bindings or {})
    paths = getattr(ctx, "paths", None)
    for key, value in configuration.items():
        if not (isinstance(value, str) and value.startswith("{") and value.endswith("}")):
            continue
        token = value[1:-1]
        if token in RUNTIME_BINDINGS:
            if not supplied.get(token):
                raise ConfigError(
                    f"Tree configuration '{key}' is supplied when the Tree is composed, "
                    f"and no '{token}' was given."
                )
            configuration[key] = supplied[token]
            continue
        if paths is not None:
            configuration[key] = str(paths.resolve(token))
    return configuration


def _true(value: Any) -> bool:
    """Whether a projected flag says yes, however the driver spelled it."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"t", "true", "1", "yes"}
