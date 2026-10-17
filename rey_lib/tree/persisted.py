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

`tree.tree_vw` is the only tree datasource this reads. The tables beneath it --
``tree_group``, ``tree_root``, ``tree_node`` -- and the producer views that feed
it are never queried from here. The database enforces it too: the ``tree`` connection is granted
SELECT on the view alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.errors.error_utils import ConfigError

from rey_lib.db.connection import shared_connection
from rey_lib.db.database_objects import DatabaseView
from rey_lib.db.database_query import DatabaseQuery

__all__ = [
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


def _projections(ctx: Any) -> frozenset[str]:
    """Every view of the tree schema, as the catalog has them.

    Read once per process: the set changes when a projection is created, which
    is a deployment, not a request.
    """
    global _KNOWN
    if _KNOWN is None:
        handle = shared_connection(ctx, TREE_CONNECTION).handle()
        rows = DatabaseQuery(_RELATIONS).rows(handle)
        _KNOWN = frozenset(str(row.get("relation") or "") for row in rows)
    return _KNOWN


#: The views this schema has, once resolved. Not a cache of anything a request
#: changes: a projection is created by a migration.
_KNOWN: frozenset[str] | None = None

_RELATIONS = """
SELECT n.nspname || '.' || c.relname AS relation
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'tree' AND c.relkind = 'v'
"""


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
        """Read rows from the view, in the order the view's own columns define.

        Values arrive as the database emitted them. Nothing is reinterpreted,
        normalized, deduplicated or reordered afterwards: a node that carries
        two actions is two rows, because that is what the view says, and hiding
        it here would make Python disagree with its own datasource.

        Args:
            ctx: Finalized installation context, which resolves the connection.
            row_type: Restrict to one kind of row, or every kind when omitted.
            tree_group_id: Restrict to one group, or every group when omitted.
            parent_node_key: Restrict to the children of one node, or every row
                when omitted. The key is bound, never interpreted: this does not
                know what one is made of and never takes one apart.
            limit: At most this many rows, or every row when omitted.
            relation: Which projection to read, or the union when omitted. A
                caller that knows which one owns what it is asking for says so,
                and is answered without the others being computed.
            tree_root_id: Restrict to one root, or every root when omitted.

        Returns:
            The rows, in view order.
        """
        statement, params = self._select(
            row_type=row_type,
            tree_group_id=tree_group_id,
            parent_node_key=parent_node_key,
            limit=limit,
            relation=relation,
            tree_root_id=tree_root_id,
        )
        handle = shared_connection(ctx, TREE_CONNECTION).handle()
        read = DatabaseQuery(statement, params).rows(handle)
        return tuple(TreeDataRow(values=MappingProxyType(row)) for row in read)

    def children(self, ctx: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
        """The children of one record, from the projection that owns them.

        The edge the view declares -- a row's ``parent_node_key`` is some other
        row's ``node_key`` -- and nothing more. The key is passed back exactly as
        it was received: nothing splits one, counts its separators, or recovers
        an id from it.

        **Which projection is asked is the record's own answer.** It carries the
        ``expansion_tvw`` its root declared, so a folder read never evaluates a
        projection it has nothing to do with. Reading the union instead would
        make every branch pay for every other: a recursive projection cannot
        have a parent predicate pushed into it, so it is computed whole before
        the filter applies, and one slow branch is then every branch's cost.

        These arrive with their identity already composed, so whoever holds them
        does not give them one.

        **The read carries the parent's own tree scope.** The record holds its
        ``tree_group_id`` and ``tree_root_id``, and a child is always in its
        parent's group and root, so both are bound beside the key. Without
        them the union evaluates every tree to answer one node. A record that
        holds neither is still read by its key alone -- a compatibility
        fallback, not the normal path.
        """
        return [
            row.to_dict()
            for row in self.rows(
                ctx,
                row_type="node",
                tree_group_id=record.get("tree_group_id"),
                tree_root_id=record.get("tree_root_id"),
                parent_node_key=str(record.get("node_key") or ""),
                relation=self._owning(ctx, record),
            )
        ]

    def _owning(self, ctx: Any, record: dict[str, Any]) -> str:
        """Which projection owns this record's descendants.

        The root said so and the row carries it. A name that is not a view of
        this schema is refused rather than run: the value is the database's own,
        and checking it against the catalog is what keeps it that way however it
        got there.
        """
        declared = str(record.get("expansion_tvw") or "").strip()
        if not declared:
            return self._qualified()
        if declared not in _projections(ctx):
            raise ConfigError(
                f"'{declared}' is named as an expansion view and is not one.",
            )
        return declared

    def _qualified(self) -> str:
        """This view, as it is addressed."""
        return f"{self.identity.schema}.{self.identity.name}"

    def _select(
        self,
        *,
        row_type: str | None,
        tree_group_id: Any = None,
        parent_node_key: str | None = None,
        limit: int | None,
        relation: str | None = None,
        tree_root_id: Any = None,
    ) -> tuple[str, dict[str, Any]]:
        """The statement that reads this view, and the values bound into it.

        Ordering is the view's own presentation ordering, so the same read
        answers the same way twice. A root read is ordered by the root's own
        ``sort_order`` first. ``node_key`` breaks the final tie because it
        is unique and is the one column guaranteed to be present on every row.

        Parameters are bound, never interpolated: a value concatenated into SQL
        is an injection and a quoting bug waiting for its first apostrophe.
        """
        qualified = relation or self._qualified()
        params: dict[str, Any] = {}
        # Every predicate is optional and every one is bound. They compose:
        # the tree is read one scope at a time -- a group's roots, or one
        # node's children -- and never filtered after the fact.
        clauses: list[str] = []
        if row_type is not None:
            clauses.append("row_type = :row_type")
            params["row_type"] = str(row_type)
        if tree_group_id is not None:
            clauses.append("tree_group_id = :tree_group_id")
            params["tree_group_id"] = int(tree_group_id)
        if tree_root_id is not None:
            clauses.append("tree_root_id = :tree_root_id")
            params["tree_root_id"] = int(tree_root_id)
        if parent_node_key is not None:
            clauses.append("parent_node_key = :parent_node_key")
            params["parent_node_key"] = str(parent_node_key)
        where = ""
        if clauses:
            where = "WHERE  " + "\n   AND ".join(clauses) + "\n"
        bound = ""
        if limit is not None:
            bound = "\nLIMIT :row_limit"
            params["row_limit"] = int(limit)
        # A root's position is its row's declared sort_order. Only a root read
        # asks for it: every other level keeps the order its projection gives.
        declared = "         sort_order   NULLS LAST,\n" if row_type == "root" else ""
        return (
            "SELECT *\n"
            f"FROM   {qualified}\n"
            f"{where}"
            "ORDER BY tree_group_id,\n"
            f"{declared}"
            "         tree_root_id NULLS FIRST,\n"
            "         sort_rank    NULLS LAST,\n"
            "         sort_ts      DESC NULLS LAST,\n"
            "         sort_number  NULLS LAST,\n"
            "         sort_text    NULLS LAST,\n"
            f"         node_key     NULLS FIRST{bound}"
        ), params

