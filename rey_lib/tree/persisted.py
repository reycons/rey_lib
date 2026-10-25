"""The tree projection, as the database object it is.

``tree.tree_vw`` is an ordinary database view. It is read by the canonical
schema loader like any other, and the loader hands back this class instead of a
plain :class:`DatabaseView` -- the concrete class is what says this particular
view carries tree behaviour, so nothing downstream needs a discriminator or a
second discovery path.

It is the **one** backend specialization of ``DatabaseView`` for
``schema='tree', name='tree_vw'``. There is no second class for that view.

It is a data object, not a display surface. It knows its connection, its view
identity, its columns and how to read rows; it knows nothing of panels,
selection, expansion, actions or what any row means. The display surface is
``TreeObject``, which is not this and is not here.

It is one feeder among several. ``NodeDataObject`` asks it for the children of a
record whose children are persisted, and asks a different object for a record
sourced from somewhere else; every feeder answers in the same canonical record
shape, and none of them knows the others exist.

It reads through ``tree.f_tree_rows`` (backlog 1206 / 1207), the database
implementation of this object's retrieval seam: one packaged, parameterized
statement (``sql/tree_rows.sql``), every value bound. Which provider answers is
the database's dispatch of the record's own expansion -- NULL is the persisted
provider, an unknown expansion is refused, and there is no fallback to the union.
Python never names a relation. The tables beneath -- ``tree_group``,
``tree_root``, ``tree_node`` -- are never queried from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.db.connection import shared_connection
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseView
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.files import read_text_file

__all__ = [
    "persisted_tree",
    "TreeDataObject",
    "TreeDataRow",
    "TREE_CONNECTION",
    "TREE_SCHEMA",
    "TREE_VIEW_NAME",
]

#: The logical connection the tree is read through. Its own identity, not
#: control's: the two happen to reach one physical database today, and that is a
#: deployment fact rather than a reason to share a name.
TREE_CONNECTION = "tree"

#: Where the projection lives. The loader compares against these to decide
#: which view specializes; nothing else parses a name to find the tree.
TREE_SCHEMA = "tree"
TREE_VIEW_NAME = "tree_vw"


#: The packaged statement every Tree read runs (backlog 1207).
_ROWS_SQL = Path(__file__).parent / "sql" / "tree_rows.sql"


@dataclass(frozen=True)
class TreeDataRow:
    """One row of ``tree.tree_vw``, exactly as the view emitted it.

    The view owns the row contract, so the columns are whatever it returned --
    they are not restated here as fields. Restating them would be a second
    contract to keep in step, and it would drift the first time the view gains
    a column.

    ``row_type`` says whether this is a group, a root or a node. ``node_key``
    and ``parent_node_key`` are opaque: composed by the producing SQL, carried
    here unread. Nothing splits one, rebuilds one, or derives parentage from
    one.
    """

    values: Mapping[str, Any]

    def __getitem__(self, column: str) -> Any:
        return self.values[column]

    def get(self, column: str, default: Any = None) -> Any:
        return self.values.get(column, default)

    @property
    def row_type(self) -> Any:
        """Group, root or node, as emitted."""
        return self.values.get("row_type")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class TreeDataObject(DatabaseView):
    """``tree.tree_vw``: the database view the tree is loaded from.

    Everything a ``DatabaseView`` owns is retained -- identity, columns, DDL and
    ``initial_select`` -- because it *is* one. What it adds is the read.
    """

    def rows(
        self,
        ctx: Any,
        *,
        row_type: str | None = None,
        tree_group_id: Any = None,
        parent_node_key: str | None = None,
        limit: int | None = None,
        relation: str | None = None,
        tree_root_id: Any = None,
    ) -> tuple[TreeDataRow, ...]:
        """Read the Tree rows one expansion owns, in the Tree's presentation order.

        One packaged statement (``sql/tree_rows.sql``) over ``tree.f_tree_rows``,
        every value bound: an omitted filter is bound as NULL and applies no
        predicate. Values arrive as the database emitted them. Nothing is
        reinterpreted, normalized, deduplicated or reordered afterwards: a node
        that carries two actions is two rows, because that is what the
        projection says.

        Args:
            ctx: Finalized installation context, which resolves the connection.
            row_type: Restrict to one kind of row, or every kind when omitted.
            tree_group_id: Restrict to one group, or every group when omitted.
            parent_node_key: Restrict to the children of one node, or every row
                when omitted. The key is bound, never interpreted: this does not
                know what one is made of and never takes one apart.
            limit: At most this many rows, or every row when omitted.
            relation: The expansion that owns what is asked for -- a record's
                own ``expansion_tvw`` -- or the persisted provider when omitted.
                The database dispatches it and refuses one it does not declare.
            tree_root_id: Restrict to one root, or every root when omitted.

        Returns:
            The rows, in the Tree's presentation order.
        """
        params = {
            "expansion_tvw": relation,
            "row_type": None if row_type is None else str(row_type),
            "tree_group_id": None if tree_group_id is None else int(tree_group_id),
            "tree_root_id": None if tree_root_id is None else int(tree_root_id),
            "parent_node_key": None if parent_node_key is None else str(parent_node_key),
            "row_limit": None if limit is None else int(limit),
        }
        handle = shared_connection(ctx, TREE_CONNECTION).handle()
        read = DatabaseQuery(read_text_file(_ROWS_SQL), params).rows(handle)
        return tuple(TreeDataRow(values=MappingProxyType(row)) for row in read)

    def children(self, ctx: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
        """The children of one record, from the projection that owns them.

        The edge the projection declares -- a row's ``parent_node_key`` is some
        other row's ``node_key`` -- and nothing more. The key is passed back
        exactly as it was received: nothing splits one, counts its separators,
        or recovers an id from it.

        **Which projection is asked is the record's own answer.** It carries the
        ``expansion_tvw`` its provider declared, and the database dispatches it
        to that one provider -- a NULL expansion to the persisted one -- so a
        read never evaluates a projection it has nothing to do with.

        These arrive with their identity already composed, so whoever holds them
        does not give them one.

        **The read carries the parent's own tree scope.** The record holds its
        ``tree_group_id`` and ``tree_root_id``, and a child is always in its
        parent's group and root, so both are bound beside the key. A record that
        holds neither is still read by its key alone.
        """
        return [
            row.to_dict()
            for row in self.rows(
                ctx,
                row_type="node",
                tree_group_id=record.get("tree_group_id"),
                tree_root_id=record.get("tree_root_id"),
                parent_node_key=str(record.get("node_key") or ""),
                relation=_owning(record),
            )
        ]


def _owning(record: Mapping[str, Any]) -> str | None:
    """The expansion a record declares for its descendants, or None for the persisted one.

    Carried as the record states it; whether it is a declared expansion is the
    database's answer (``tree.f_tree_rows`` refuses one it does not declare).
    """
    declared = str(record.get("expansion_tvw") or "").strip()
    return declared or None


def persisted_tree() -> TreeDataObject:
    """The persisted tree's data object, stated once (backlog 1055, D3).

    The canonical statement of which view the persisted tree is and where it is
    reached: every consumer that reads persisted children asks for this rather
    than composing the identity itself.
    """
    return TreeDataObject(
        identity=DatabaseObjectIdentity(
            connection=TREE_CONNECTION,
            catalog="rey_apps",
            schema=TREE_SCHEMA,
            name=TREE_VIEW_NAME,
        ),
    )

