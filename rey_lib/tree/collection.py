"""A configured collection, as canonical node records.

One feeder among several, and the same shape as the others: handed a record, it
reads what that record's configuration names, projects what it finds, and
returns records without identity. It holds nothing -- the projection owns what
survives -- and it decides nothing about what a node means.

**It does not know which collection it is.** That is the whole point of it. The
other two feeders are told what to read and this one is told the same way, so
the three are one pattern rather than two plus a special case:

    folder      configuration says which path
    database    configuration says which connection
    collection  configuration says which collection

The collections are exactly the configured boundaries this object owns, and an
unknown name is refused. It is not a switchboard for whatever a row happens to
say: a name nobody implemented is a configuration fault, and answering it with
nothing would hide one.

The levels are the record's own configuration, never a guess:

    {collection: applications}    every configured application
    {collection: tools}           every configured tool

Every member arrives **already addressed**: ``configured_applications`` carries
the id each application is known by, and it is passed through exactly as it
came. Nothing here signs anything or names an action -- which action a kind is
assigned is ``tree.node_type_action``'s answer.

Moved from the Console (backlog 1059, S6) with its two projections, from
rey_console.collection_data_object and rey_console.object_tree. Projection
only: applications are the objects the configuration load built
(``ctx.applications``), and tools are ToolRegistry's interpretation of the
loaded ``ctx.tools`` (backlog 1093). Nothing here reads or interprets
configuration.
"""

from __future__ import annotations

from typing import Any, Callable

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tools import ToolRegistry
from rey_lib.tree.sources import APPLICATION_SOURCE, TOOL_SOURCE

__all__ = [
    "CollectionDataObject",
    "configured_applications",
    "configured_tools",
]


def configured_applications(ctx: Any) -> list[dict[str, Any]]:
    """
    The applications this installation has configured.

    A projection of ``ctx.applications`` -- the objects the config load built,
    which own the enabled filtering and the shape the declaration had to be in.
    Nothing is normalized, enriched or filtered here.
    """
    return [
        {
            "id": row.name,
            "label": row.label,
            # Published by the application itself, beside its CLI. Carried,
            # never chosen here: which mark is an application's own is a fact
            # about the application, and this reader decides nothing.
            "icon": row.icon,
            # What an application is addressed by. Which action that identity
            # is for is tree.node_type_action's answer, not this reader's.
            "application_id": row.name,
            # Published by the application, as its icon is (backlog 1098):
            # which node type its node is. Carried, never chosen here.
            "node_type": row.node_type,
        }
        for row in _ctx_applications(ctx)
    ]


def _ctx_applications(ctx: Any) -> tuple[Any, ...]:
    """The applications the config load built.

    A seam, not a normalizer: these are already objects, validated when they
    were constructed, so re-wrapping them would be a second answer to a settled
    question.

    Its absence is a fault, not an empty installation: applications are built by
    the config load, so a context without them was not loaded by it.
    """
    applications = getattr(ctx, "applications", None)
    if applications is None:
        raise ConfigError(
            "ctx.applications is missing: applications are built by the "
            "configuration load, so a context without them was not built by it."
        )
    return tuple(applications)


def configured_tools(ctx: Any) -> list[dict[str, Any]]:
    """
    The tools this installation has configured.

    A projection of ToolRegistry's enabled tools, as it interprets the loaded
    ``ctx.tools``. What a tool is, which sections hold tools and which are
    enabled are the registry's answers; this only shapes what it returns.
    """
    return [
        {"id": tool.name, "label": tool.label}
        for tool in ToolRegistry.from_configuration(ctx.tools).all_tools()
    ]

#: Which boundary answers for each collection. The whole of what this object
#: supports: a name absent here is refused, not improvised.
_COLLECTIONS: dict[str, Callable[[Any], list[dict[str, Any]]]] = {
    APPLICATION_SOURCE: configured_applications,
    TOOL_SOURCE: configured_tools,
}

#: What each level reads as. Named, never drawn.
_MEMBER_ICON = {
    APPLICATION_SOURCE: "application",
    TOOL_SOURCE: "tool",
}

#: What one member of each collection *is*. The word the behaviour registry is
#: keyed by.
_MEMBER_TYPE = {
    APPLICATION_SOURCE: "application",
    TOOL_SOURCE: "tool",
}
#: What each collection's members are addressed by. The field the configured
#: reader already fills, named here only so it is carried onto the record.
_IDENTITY_FIELD = {
    APPLICATION_SOURCE: "application_id",
}


class CollectionDataObject:
    """The children of a record that stands at a configured collection."""

    def state_key(self, record: dict[str, Any]) -> str | None:
        """What the collection this record needs is held under."""
        return f"collection:{self._collection(record)}"

    def state(self, ctx: Any, record: dict[str, Any]) -> Any:
        """Read the collection this record's level is answered from.

        Read here because reading is this object's business; held by the
        projection because surviving between requests is not.
        """
        return _COLLECTIONS[self._collection(record)](ctx)

    def children(
        self,
        ctx: Any,
        record: dict[str, Any],
        state: Any = None,
    ) -> list[dict[str, Any]]:
        """Whatever hangs beneath one record, as canonical records.

        Args:
            ctx: The installation context. Unread here: the collection this
                level needs was read into ``state``.
            record: The parent, carrying the configuration that says where it
                stands.
            state: The collection it is answered from, held by the projection.

        Returns:
            Records without identity, by label ignoring case. A member opens
            into nothing.
        """
        collection = self._collection(record)
        return _alphabetical([
            self._member(collection, entry) for entry in (state or [])
        ])

    # -- which collection -------------------------------------------------------

    def _collection(self, record: dict[str, Any]) -> str:
        """Which collection a record's configuration names.

        Refused rather than guessed. A row naming a collection nobody
        implemented is a fault worth seeing at the moment it is opened, and a
        root that quietly answers with nothing reads as a tree that is empty.
        """
        configured = dict(record.get("tree_configuration") or {})
        named = str(configured.get("collection") or "")
        if named in _COLLECTIONS:
            return named
        raise ConfigError(f"No collection is configured as '{named}'.")

    # -- shaping ----------------------------------------------------------------

    def _member(self, collection: str, entry: dict[str, Any]) -> dict[str, Any]:
        """One member of a collection, as the record beneath its node.

        Args:
            collection: Which collection it came from, which is what it reads
                as.
            entry: The member, as the configured boundary normalized it.
        """
        return {
            "row_type": "node",
            # What the member published, or what its collection's members are:
            # a published node type is the member's own (backlog 1098).
            "node_type": str(entry.get("node_type") or "") or _MEMBER_TYPE[collection],
            "label": str(entry.get("label") or entry.get("id") or ""),
            # What the member published, or the mark its collection is known
            # by. The fallback is the SEMANTIC icon rather than nothing: an
            # empty name resolves to no icon at all, and an app.* name nothing
            # defines resolves to a briefcase.
            "icon": str(entry.get("icon") or "") or _MEMBER_ICON[collection],
            "has_children": False,
            "is_enabled": True,
            "refreshable": False,
            "tree_object_key": None,
            "tree_configuration": {},
            **self._identity(collection, entry),
        }

    def _identity(self, collection: str, entry: dict[str, Any]) -> dict[str, Any]:
        """What this subject is addressed by.

        The identity the configured collection already holds, carried onto the
        record under its own name. Which action it is for is
        ``tree.node_type_action``'s answer; this names none.
        """
        field = _IDENTITY_FIELD.get(collection)
        return {field: entry[field]} if field and field in entry else {}


def _alphabetical(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A collection's members by label, ignoring case.

    Configuration order is the order a file happened to list them in, not an
    order anyone declared.
    """
    return sorted(records, key=lambda record: (
        str(record.get("label") or "").casefold(),
        str(record.get("label") or ""),
    ))
