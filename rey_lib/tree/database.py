"""A configured connection, as canonical node records.

One feeder among several, structurally identical to the others: it is handed a
record, reads what that record's configuration names, and returns records in the
shape every feeder returns. It holds nothing between calls -- the projection
owns what survives -- and it decides nothing about what a node means.

Where meaning comes from, precisely:

    persisted node   the persisted tree data
    sourced node     the source object being projected

A table and a view are sourced, so what addresses them comes from the database
objects themselves: ``schema_projection`` supplies the connection, schema, name
and statement each is known by. This carries them through untouched. Which
action a relation is assigned is ``tree.node_type_action``'s answer, and no
action is named here.

The levels are the record's own configuration, never a guess:

    {connection}                    catalogs
    {connection, catalog}           schemas
    {connection, catalog, schema}   the collections the schema holds
    {..., collection}               that collection's objects

The same two reads the old tree performed, through the same boundaries:
``connection_snapshot`` once for the connection, and ``load_database_schema``
once per schema.

Moved from the Console with those two readers and ``schema_projection``
(backlog 1057), unchanged in code but for the error vocabulary -- a missing name
or a mis-shaped declaration is a ConfigError, a failed open or read a
DatabaseError -- with the readers' module docstrings folded into theirs.
The readers' broad catches are the database-read boundary, admitted by name.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from rey_lib.db.connection import shared_connection
from rey_lib.db.database_objects import (
    DatabaseFunction,
    DatabaseProcedure,
    DatabaseSchema,
    DatabaseTable,
    DatabaseView,
)
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import AppLogger
from rey_lib.repository_map.indexed_database_object import (
    IndexedCodeReach,
    IndexedDatabaseObject,
    IndexedRelation,
)
from rey_lib.tree.persisted import TREE_CONNECTION, TREE_SCHEMA, TREE_VIEW_NAME, TreeDataObject
from rey_lib.tree.sources import DATABASE_SOURCE

__all__ = [
    "DatabaseDataObject",
    "connection_snapshot",
    "load_database_schema",
    "schema_projection",
]

_logger = AppLogger(__name__)

#: The two dependency readings an object offers, and which way each one asks.
#: Outgoing is what this object needs; incoming is what needs it.
_RELATIONSHIPS = (
    ("depends", "Depends On", False),
    ("used", "Used By", True),
)

#: Where the dependency facts live. The code index is in the database the
#: Console already reads its own projections from, so it is reached the same
#: way -- not through the browsed connection, which may be any database at all
#: and is not where the index was published. What the index knows is
#: IndexedDatabaseObject's (backlog 696, 729); where it lives is this.
_INDEX_CONNECTION = TREE_CONNECTION

#: Where a code symbol's file is rooted, in the token the code tree already
#: uses. The open_file action reads source_root and relative_path off the row.
_SOURCE_ROOT = "{apps}"

#: What each collection reads as: one per canonical object type (backlog 720).
#: The order is the order the old Console listed them; whether a kind appears is
#: whether the schema read holds one.
_COLLECTIONS = (
    ("tables", "Tables", "table"),
    ("views", "Views", "view"),
    ("procedures", "Procedures", "procedure"),
    ("functions", "Functions", "function"),
)


#: The configuration key restricting which collection kinds a level offers.
#:
#: **A RESTRICTION OVER AN EXISTING SET, not a second way to decide what a
#: database has.** The schema read still answers which kinds it holds; this
#: narrows that answer to the ones a root asked for. Naming a kind the schema
#: holds none of admits nothing.
#:
#: Named for the concept rather than for whoever wanted it first. `collection`
#: singular is already taken and means the opposite -- "this node STANDS AT that
#: collection" -- so a plural beside it would be two near-identical keys with
#: unrelated meanings.
#:
#: GENERIC ON PURPOSE. `CollectionDataObject` asks the same question of its own
#: `_COLLECTIONS`, and can adopt this key unchanged when something needs it to.
#: Nothing else implements it today.
ALLOWED_COLLECTIONS = "allowed_collections"


def _allowed_collections(configured: dict[str, Any]) -> set[str] | None:
    """Which collection kinds this configuration admits, or None for all.

    Args:
        configured: The node's carried tree configuration.

    Returns:
        The restriction as a set, or None where none is declared -- which is
        every root that existed before this key did.

    Raises:
        ConfigError: When a named kind is not a collection. Refused rather
            than skipped: a typo that silently removed a level would render a
            tree that looks finished and is missing part of the database.
    """
    declared = configured.get(ALLOWED_COLLECTIONS)
    if declared is None:
        return None
    named = {str(one) for one in declared}
    known = {kind for kind, _label, _icon in _COLLECTIONS}
    unknown = sorted(named - known)
    if unknown:
        raise ConfigError(
            f"{ALLOWED_COLLECTIONS} names {', '.join(unknown)}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not "
            f"{'a collection' if len(unknown) == 1 else 'collections'}. "
            f"The collections are: {', '.join(sorted(known))}."
        )
    return named


#: Whether this root presents objects directly beneath their schema.
#:
#: A PRESENTATION SHAPE, not a second tree. It changes which levels are DRAWN
#: and nothing about what a node is: every node still carries its catalog and
#: its collection, so what is addressed beneath one is unchanged.
OBJECTS_UNDER_SCHEMA = "objects_under_schema"


def _objects_under_schema(configured: dict[str, Any]) -> str | None:
    """The one collection whose objects hang from the schema, or None.

    Answers both questions the two dispatch branches ask -- whether this root
    presents the alternate shape, and which kind it presents -- so neither reads
    the key itself and they cannot come to different conclusions.

    ``allowed_collections`` says WHICH kinds are allowed; this says WHETHER those
    objects appear directly beneath each schema. Neither answers the other's
    question, which is why they are two keys.

    Args:
        configured: The node's carried tree configuration.

    Returns:
        The single admitted collection kind, or None where this root presents
        the ordinary catalog -> schema -> collection -> object shape.

    Raises:
        ConfigError: When the shape is declared without exactly one allowed
            collection. Refused rather than resolved: over several kinds a table
            and a view of the same name would sit side by side with nothing
            telling them apart, and choosing one for the reader would be this
            object deciding what the configuration failed to say.
    """
    if not configured.get(OBJECTS_UNDER_SCHEMA):
        return None
    allowed = _allowed_collections(configured)
    if allowed is None or len(allowed) != 1:
        named = "no collections" if allowed is None else f"{len(allowed)} collections"
        raise ConfigError(
            f"{OBJECTS_UNDER_SCHEMA} presents one collection's objects beneath "
            f"each schema, so it requires exactly one {ALLOWED_COLLECTIONS}; "
            f"this root declares {named}."
        )
    return next(iter(allowed))


class DatabaseDataObject:
    """The children of a record that stands at a connection, or within one."""

    def state_key(self, record: dict[str, Any]) -> str | None:
        """What the source state this record needs is held under.

        A connection's snapshot serves every level beneath it, and a schema's
        objects serve the collections within it. Both are named by what the
        record already carries, so nothing walks up a tree to find them.

        An object and its structure are answered from its schema's read -- the
        same one the object was built from, already held when the object was
        reached by browsing. What it depends on is the index's answer, so a
        dependency level names no state.
        """
        configured = dict(record.get("tree_configuration") or {})
        connection = str(configured.get("connection") or "")
        schema = str(configured.get("schema") or "")
        if configured.get("object") and configured.get("relationship"):
            return None
        if not connection:
            return None
        if not schema:
            return f"database:{connection}"
        catalog = str(configured.get("catalog") or "")
        return f"database:{connection}:{catalog}:{schema}"

    def state(self, ctx: Any, record: dict[str, Any]) -> Any:
        """Read whatever this record's level is answered from.

        The connection's snapshot, or one schema's objects. Read here because
        reading is this object's business; held by the projection because
        surviving between requests is not.
        """
        configured = dict(record.get("tree_configuration") or {})
        connection = str(configured.get("connection") or "")
        schema = str(configured.get("schema") or "")
        if not schema:
            return connection_snapshot(ctx, connection)
        # The schema's objects, and nothing beside them: the kinds are the
        # canonical object types, and which instances exist is the schema
        # read's answer (backlog 720). No provider capability is asked.
        return schema_projection(load_database_schema(
            ctx, connection, str(configured.get("catalog") or ""), schema,
        ))

    def children(
        self,
        ctx: Any,
        record: dict[str, Any],
        state: Any = None,
    ) -> list[dict[str, Any]]:
        """Whatever hangs beneath one record, as canonical records.

        Args:
            ctx: The installation context. Unread here: everything this level
                needs was read into ``state``.
            record: The parent, carrying the configuration that says where it
                stands.
            state: What its level is answered from, held by the projection.

        Returns:
            Records without identity, in the order the source gave them.
        """
        configured = dict(record.get("tree_configuration") or {})
        collection = str(configured.get("collection") or "")
        schema = str(configured.get("schema") or "")
        catalog = str(configured.get("catalog") or "")

        # Object levels first: they are named by keys the levels below do not
        # carry, and testing them last would hand an object to _collections.
        if configured.get("object"):
            structure = str(configured.get("structure") or "")
            if structure:
                return self._structure(configured, state, structure)
            relationship = str(configured.get("relationship") or "")
            if not relationship:
                # What the object is made of, then what it depends on and
                # what uses it -- the latter exactly as before.
                return (
                    self._structure_headings(configured, state)
                    + self._relationships(ctx, configured)
                )
            results = str(configured.get("results") or "")
            if results == "code":
                return self._code_level(ctx, configured)
            if results:
                return self._related(ctx, configured, relationship)
            return self._result_groups(ctx, configured, relationship)
        if collection:
            return self._objects(configured, state, collection)
        # WHICH LEVELS ARE DRAWN, asked once and answered for both branches
        # below. A root presenting the alternate shape skips the two levels
        # that would each hold exactly one thing -- the connection's single
        # catalog, and a heading over the only kind it admits.
        beneath_schema = _objects_under_schema(configured)
        if schema:
            if beneath_schema:
                return self._schema_objects(configured, state, beneath_schema)
            return self._collections(configured, state)
        if catalog:
            return self._schemas(configured, state, catalog)
        if beneath_schema:
            # Every schema the connection reported, which is its one catalog's:
            # the level is removed, not summarized.
            return self._schemas(configured, state)
        return self._catalogs(configured, state)

    # -- one level each ---------------------------------------------------------

    def _catalogs(self, configured: dict[str, Any], state: Any) -> list[dict[str, Any]]:
        """The catalogs the connection reported."""
        return [
            self._branch(
                str(entry.get("name") or ""),
                "database",
                {**configured, "catalog": entry.get("name")},
                _CATALOG_TYPE,
            )
            for entry in (state or {}).get("catalogs") or []
        ]

    def _schemas(
        self,
        configured: dict[str, Any],
        state: Any,
        catalog: str = "",
    ) -> list[dict[str, Any]]:
        """The schemas recorded under one catalog, or under the connection's.

        Known without a read of their own: the connection's answer said which
        schemas each catalog holds, which is why one snapshot serves the whole
        branch.

        **The catalog is taken from the ENTRY, not inherited.** Standing under a
        catalog node it is inherited and the two agree -- the filter below
        matched on it -- so the configuration produced there is unchanged. Where
        the catalog level is not drawn there is no parent to inherit from, and
        the schema must still carry the catalog it belongs to, because what is
        addressed beneath a node is not what is drawn above it.

        Args:
            configured: The node's carried tree configuration.
            state: The connection's snapshot, holding its schemas.
            catalog: Which catalog's schemas, where a catalog node named one.
                Empty means every schema the connection reported -- which is
                that one catalog's, since ``DatabaseSchema.list`` answers
                with the single database a connection is attached to.
        """
        return [
            {
                **self._branch(
                    str(entry.get("name") or ""),
                    "schema",
                    {
                        **configured,
                        "catalog": entry.get("catalog"),
                        "schema": entry.get("name"),
                    },
                    _SCHEMA_TYPE,
                ),
                # What this schema is addressed by, on the record under its own
                # names, so a declared action can read it (backlog 869). The same
                # values the configuration above carries -- nothing is looked up.
                "connection": configured.get("connection"),
                "catalog": entry.get("catalog"),
                "schema": entry.get("name"),
            }
            for entry in (state or {}).get("schemas") or []
            if not catalog or str(entry.get("catalog") or "") == catalog
        ]

    def _schema_objects(
        self,
        configured: dict[str, Any],
        state: Any,
        collection: str,
    ) -> list[dict[str, Any]]:
        """One collection's objects, standing directly beneath their schema.

        The alternate shape's schema level. What it produces is what
        ``_objects`` produces under a collection node -- the same records, built
        the same way, addressing the same objects -- reached without drawing the
        heading.

        A kind the schema read holds none of yields nothing here, exactly as it
        yields no heading in ``_collections``: configuration narrows what the
        schema holds and can never add to it, whichever shape is being drawn.
        """
        return self._objects({**configured, "collection": collection}, state, collection)

    def _collections(self, configured: dict[str, Any], state: Any) -> list[dict[str, Any]]:
        """The collections one schema offers.

        The kinds are the canonical object types -- tables, views, procedures,
        functions -- and a kind appears when the schema read holds one
        (backlog 720). No provider capability is asked: what a provider cannot
        hold, its schema read holds none of.

        ``allowed_collections`` NARROWS that answer where a root declares one.
        It is a restriction and never a grant: a kind the schema holds none of
        cannot be admitted by naming it. Absent -- which every root carries
        today -- the answer is exactly what it has always been.

        Inherited like every other configuration key, so a root states it once
        and each level below carries it forward.

        Raises:
            ConfigError: When the restriction names a kind that is not a
                collection. A mistyped root fails where it is configured rather
                than rendering a tree quietly missing a level.
        """
        allowed = _allowed_collections(configured)
        made: list[dict[str, Any]] = []
        for kind, label, icon in _COLLECTIONS:
            if allowed is not None and kind not in allowed:
                continue
            # The schema's snapshot is what this heading's children are read
            # from, so a kind it holds none of would be a folder that opens on
            # nothing.
            if not (state or {}).get(kind):
                continue
            made.append(self._branch(
                label, icon, {**configured, "collection": kind}, kind,
            ))
        return made

    def _objects(
        self,
        configured: dict[str, Any],
        state: Any,
        collection: str,
    ) -> list[dict[str, Any]]:
        """One collection's objects, as they were constructed.

        A relation arrives already declaring what clicking it means and holding
        the authorization for it. Both are carried exactly as they came: this
        signs nothing and names no action of its own.
        """
        return [
            self._object_record(configured, entry, collection)
            for entry in (state or {}).get(collection) or []
        ]

    def _object_record(
        self,
        configured: dict[str, Any],
        entry: dict[str, Any],
        collection: str,
        suffix: str = "",
    ) -> dict[str, Any]:
        """One database object, however it was arrived at.

        The single construction. A dependency result is the same object as the
        one reached by browsing -- same identity, same payload, same actions,
        same children -- because it is built here from the same entry the
        schema read produced, never assembled from index columns. The index
        says WHICH object; this says what an object is.
        """
        return {
            "row_type": "node",
            "node_type": _MEMBER_TYPE.get(collection, collection),
            "label": self._object_label(entry) + suffix,
            "icon": _COLLECTION_ICON.get(collection, "table"),
            # No longer terminal: an object offers what it depends on and what
            # uses it, both answered from the index.
            "has_children": True,
            "is_enabled": True,
            "refreshable": False,
            "tree_object_key": DATABASE_SOURCE,
            "tree_configuration": self._addressed(configured, entry, collection),
            **self._identity(entry),
        }

    # -- what an object is made of ----------------------------------------------

    def _structured(
        self, configured: dict[str, Any], state: Any,
    ) -> dict[str, Any] | None:
        """The object's own entry in its schema's read, or None where it has none.

        Found by the identity the object carries, signature included, in the
        same read the object itself was built from.
        """
        object_type = str(configured.get("object_type") or "")
        collection = _COLLECTION_OF.get(object_type, object_type)
        return self._entry_for(
            state,
            collection,
            str(configured.get("object") or ""),
            str(configured.get("signature") or ""),
        )

    def _structure_headings(
        self, configured: dict[str, Any], state: Any,
    ) -> list[dict[str, Any]]:
        """The structural collections this object has, each only where non-empty.

        A heading over nothing would claim children the object does not have.
        """
        entry = self._structured(configured, state)
        if entry is None:
            return []
        present = {
            "columns": bool(entry.get("columns")),
            "indexes": bool(entry.get("indexes")),
            "constraints": any(self._constraint_groups(entry).values()),
        }
        return [
            self._branch(label, icon, {**configured, "structure": key}, _STRUCTURE_TYPE)
            for key, label, icon in _STRUCTURES
            if present.get(key)
        ]

    def _structure(
        self, configured: dict[str, Any], state: Any, structure: str,
    ) -> list[dict[str, Any]]:
        """One structural level beneath an object.

        Columns keep their ordinal position, which is the order the table
        declares. Everything else listed by name is listed ignoring case, with
        the raw name settling a tie.
        """
        entry = self._structured(configured, state) or {}
        groups = self._constraint_groups(entry)
        if structure == "columns":
            columns = sorted(
                entry.get("columns") or [], key=lambda one: int(one.get("ordinal") or 0),
            )
            return [
                self._leaf(
                    f"{one.get('name') or ''}  {one.get('type') or ''}".rstrip(),
                    _STRUCTURE_ICON["columns"], "column",
                )
                for one in columns
            ]
        if structure == "indexes":
            return [
                self._leaf(
                    f"{self._named_columns(one)}{' unique' if one.get('unique') else ''}",
                    _STRUCTURE_ICON["indexes"], "index",
                )
                for one in _by_name(entry.get("indexes") or [])
            ]
        if structure == "constraints":
            return [
                self._branch(label, icon, {**configured, "structure": key}, _STRUCTURE_TYPE)
                for key, label, icon in _CONSTRAINT_GROUPS
                if groups.get(key)
            ]
        if structure in groups:
            return [
                self._leaf(
                    self._constraint_label(structure, one),
                    _STRUCTURE_ICON[structure], _CONSTRAINT_TYPE[structure],
                )
                for one in _by_name(groups[structure])
            ]
        return []

    def _constraint_groups(self, entry: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """The object's constraints, by the group each is listed under."""
        key = entry.get("primary_key")
        constraints = list(entry.get("constraints") or [])
        return {
            "primary_key": [key] if key else [],
            "foreign_keys": list(entry.get("foreign_keys") or []),
            "unique": [one for one in constraints if one.get("kind") == "unique"],
            "checks": [one for one in constraints if one.get("kind") == "check"],
        }

    def _constraint_label(self, group: str, one: dict[str, Any]) -> str:
        """What one constraint reads as: its name, and the columns it holds."""
        if group == "checks":
            return str(one.get("name") or "")
        if group == "foreign_keys":
            target = (
                f"{one.get('referenced_schema') or ''}.{one.get('referenced_table') or ''}"
                f"({', '.join(one.get('referenced_columns') or [])})"
            )
            return f"{self._named_columns(one)} → {target}"
        return self._named_columns(one)

    def _named_columns(self, one: dict[str, Any]) -> str:
        """A name and the columns it covers, as `name (a, b)`."""
        return f"{one.get('name') or ''} ({', '.join(one.get('columns') or [])})"

    def _leaf(self, label: str, icon: str, node_type: str) -> dict[str, Any]:
        """One structural entry: shown, and nothing more.

        It opens into nothing, cannot be selected and offers no action -- an
        entry describes part of an object rather than being one.
        """
        return {
            "row_type": "node",
            "node_type": node_type,
            "label": label,
            "icon": icon,
            "has_children": False,
            "is_enabled": True,
            "selectable": False,
            "refreshable": False,
            "tree_object_key": None,
            "tree_configuration": {},
        }

    # -- what an object depends on, and what uses it ----------------------------

    def _relationships(
        self, ctx: Any, configured: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The dependency readings this object actually has.

        A heading appears only where there is something beneath it. An empty
        ``Depends On`` would say this object needs nothing, which is a claim the
        index may simply not be in a position to make.
        """
        if not self._indexed(ctx, configured):
            # Said outright, because an absent heading would read as "this
            # object needs nothing", and the honest answer is that nobody has
            # looked. Not indexed and no dependencies are different answers.
            return [self._status("Not in the published database index")]
        made: list[dict[str, Any]] = []
        for key, label, reverse in _RELATIONSHIPS:
            if not self._related_rows(ctx, configured, reverse):
                continue
            made.append(self._branch(
                label, "link",
                {**configured, "relationship": key},
                _RELATIONSHIP_TYPE,
            ))
        if made:
            return made
        # Indexed, analysed, and genuinely standing alone. Said rather than
        # left blank: an object offers this level, so returning nothing would
        # make it claim children it does not have -- and the reader could not
        # tell that from a failure to answer.
        return [self._status("No dependencies recorded in the index")]

    def _status(self, label: str) -> dict[str, Any]:
        """One node that reports what the index knows, not a thing in it."""
        return {
            "row_type": "node",
            "node_type": _INDEX_STATUS_TYPE,
            "label": label,
            "icon": "info",
            "has_children": False,
            "is_enabled": True,
            "selectable": False,
            "refreshable": False,
            "tree_object_key": None,
            "tree_configuration": {},
        }

    def _indexed(self, ctx: Any, configured: dict[str, Any]) -> bool:
        """Whether the index has ever seen this object.

        The browsed connection may be any database; the index was published
        about a particular one. So an object being absent here is ordinary, and
        it is the difference between "nothing depends on this" and "nobody
        looked".
        """
        handle = shared_connection(ctx, _INDEX_CONNECTION).handle()
        return _indexed_object(configured).is_indexed(handle)

    def _result_groups(
        self, ctx: Any, configured: dict[str, Any], relationship: str,
    ) -> list[dict[str, Any]]:
        """What kinds of thing answer this reading.

        Database objects today. Code belongs here too and is deferred: the
        bridge that resolves a code symbol to a routine is not published yet,
        and a heading with nothing under it would misstate that as "no code".
        """
        made: list[dict[str, Any]] = []
        if self._related_rows(ctx, configured, self._reverse(relationship)):
            made.append(self._branch(
                "Database Objects", "database",
                {**configured, "results": "database"},
                _RESULT_GROUP_TYPE,
            ))
        # Code appears under the INCOMING reading only. Code reaches a database
        # object; a database object does not reach code, so offering it under
        # Depends On would be a heading that can never have anything in it.
        if relationship == "used" and self._bridge_rows(ctx, configured):
            made.append(self._branch(
                "Code", "code",
                {**configured, "results": "code"},
                _RESULT_GROUP_TYPE,
            ))
        return made

    def _related(
        self, ctx: Any, configured: dict[str, Any], relationship: str,
    ) -> list[dict[str, Any]]:
        """The related objects, as the database objects they actually are.

        The index answers WHICH objects, by identity. Each one is then read
        from the database the same way browsing reads it, and built by the same
        constructor -- so it arrives with its real statement and definition,
        and opens in the Workbench exactly as it does when reached through its
        schema.

        Synthesising the record from index columns instead is what made a table
        unopenable: the index holds no definition for a relation, so the row
        carried neither a statement nor a DDL and the open refused it. The
        object model has both; it just had to be asked.
        """
        connection = str(configured.get("connection") or "")
        catalog = str(configured.get("catalog") or "")
        held: dict[str, dict[str, Any]] = {}
        made: list[dict[str, Any]] = []

        for relation in self._related_rows(ctx, configured, self._reverse(relationship)):
            schema_name, name, signature, kind = (
                relation.schema, relation.name, relation.signature, relation.reference_kind,
            )
            object_type = relation.object_type
            collection = _COLLECTION_OF.get(object_type, object_type)
            if schema_name not in held:
                held[schema_name] = schema_projection(load_database_schema(
                    ctx, connection, catalog, schema_name,
                ))
            entry = self._entry_for(held[schema_name], collection, name, signature)
            if entry is None:
                # In the index and not in the database: the publication is
                # older than the schema. Said, because silently dropping it
                # would make the graph look complete when it is stale.
                made.append(self._status(
                    f"{schema_name}.{name} is in the index and not in the database"
                ))
                continue
            made.append(self._object_record(
                {**configured, "schema": schema_name}, entry, collection,
                suffix=f"  ({kind})",
            ))
        return made

    def _entry_for(
        self, objects: Any, collection: str, name: str, signature: str,
    ) -> dict[str, Any] | None:
        """One object out of a schema read, matched on its full identity.

        Signature included, because two overloads share a name and differ only
        here. A match on name alone would hand back whichever came first.
        """
        for entry in (objects or {}).get(collection) or []:
            identity = dict(entry.get("identity") or {})
            if str(identity.get("name") or entry.get("name") or "") != name:
                continue
            if str(identity.get("signature") or "") == signature:
                return entry
        return None

    # -- what code reaches this object, and through which mapping ---------------

    def _code_level(
        self, ctx: Any, configured: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """One of the three levels beneath Code, by what the record names.

        Mappings -> installation -> map -> symbol. The installation and the map
        are levels of their own rather than a flattened label because the
        resolution IS installation-specific: the same binding reaches a
        different routine under a different installation, and burying that in a
        caption would hide the one thing the bridge exists to make visible.
        """
        rows = self._bridge_rows(ctx, configured)
        installation = str(configured.get("bridge_installation") or "")
        map_name = str(configured.get("bridge_map") or "")

        if str(configured.get("bridge") or "") == "mappings" and not installation:
            return [
                self._branch(
                    name, "installation",
                    {**configured, "bridge_installation": name},
                    _RESULT_GROUP_TYPE,
                )
                for name in sorted({reach.installation for reach in rows})
            ]

        if not installation:
            # Only "Mappings" today. Direct Access belongs here too and has no
            # fact source: measured across 243 objects, code never names a
            # database object. The slot is the structure's, not a branch to
            # build before something fills it.
            return [self._branch(
                "Mappings", "link", {**configured, "bridge": "mappings"},
                _RESULT_GROUP_TYPE,
            )] if rows else []

        if not map_name:
            return [
                self._branch(
                    name, "installation",
                    {**configured, "bridge_map": name},
                    _RESULT_GROUP_TYPE,
                )
                for name in sorted({reach.map_name for reach in rows
                                    if reach.installation == installation})
            ]

        return [
            self._code_record(reach)
            for reach in rows
            if reach.installation == installation and reach.map_name == map_name
        ]

    def _code_record(self, reach: IndexedCodeReach) -> dict[str, Any]:
        """One code symbol, as the module record the code tree already uses.

        node_type 'module' because tree.node_type_action already declares
        module -> open_file reading source_root and relative_path. Nothing new
        is registered and no action is named here.

        The LABEL is the symbol; the clickthrough is its file. Several methods
        of one class open the same module, and the label is what tells them
        apart -- 'symbol' carries no action of its own, so collapsing them to
        the file would lose the only thing that distinguishes them.
        """
        repository, relative_path, qualified, binding = (
            reach.repository_key, reach.relative_path, reach.qualified_name,
            reach.binding_name,
        )
        return {
            "row_type": "node",
            "node_type": "module",
            "label": f"{qualified}  ({binding})",
            "icon": "file",
            "has_children": False,
            "is_enabled": True,
            "refreshable": False,
            "tree_object_key": None,
            "tree_configuration": {},
            "source_root": _SOURCE_ROOT,
            # The repository leads, as it does in the code tree's own paths.
            "relative_path": f"{repository}/{relative_path}",
        }

    def _bridge_rows(
        self, ctx: Any, configured: dict[str, Any],
    ) -> tuple[IndexedCodeReach, ...]:
        """Code that reaches this object through a mapping, in one read.

        Read once per level and filtered in memory: the levels beneath Code are
        groupings of the same answer, and asking again for each would be the
        same question three times.
        """
        handle = shared_connection(ctx, _INDEX_CONNECTION).handle()
        return _indexed_object(configured).reached_by_code(handle)

    def _related_rows(
        self, ctx: Any, configured: dict[str, Any], reverse: bool,
    ) -> tuple[IndexedRelation, ...]:
        """One direction of one object's edges, read from the index.

        Read through the connection the Console reads its own projections
        through, never the browsed one: the browsed connection may be any
        database, and the index was published about a particular one.

        An object the index has never seen returns nothing here, and that is
        why no heading is offered for it -- "not indexed" and "no dependencies"
        must not read the same, and an absent heading says the first.
        """
        handle = shared_connection(ctx, _INDEX_CONNECTION).handle()
        return _indexed_object(configured).related(handle, reverse=reverse)

    def _reverse(self, relationship: str) -> bool:
        """Which way a named reading asks."""
        for key, _label, reverse in _RELATIONSHIPS:
            if key == relationship:
                return reverse
        return False

    def _addressed(
        self, configured: dict[str, Any], entry: dict[str, Any], collection: str,
    ) -> dict[str, Any]:
        """How one object is addressed once it owns what is beneath it."""
        identity = dict(entry.get("identity") or {})
        return {
            **{k: v for k, v in configured.items() if k in ("connection", "catalog")},
            "schema": str(configured.get("schema") or ""),
            "object": str(identity.get("name") or entry.get("name") or ""),
            "object_type": _MEMBER_TYPE.get(collection, collection),
            "signature": str(identity.get("signature") or ""),
        }

    # -- shaping ----------------------------------------------------------------

    def _branch(
        self,
        label: str,
        icon: str,
        configuration: dict[str, Any],
        node_type: str,
    ) -> dict[str, Any]:
        """One level that opens into another, carrying its source forward.

        It refuses the selection, as every other heading in the tree does. The
        contract is stated where selection is decided: Applications, Workflows,
        Pipelines, Run History, Tools, the File Manifest and the database
        collections all declare it. Opening one is expanding it, and selecting
        it would say nothing for anything to respond to.
        """
        return {
            "row_type": "node",
            "node_type": node_type,
            "label": label,
            "icon": icon,
            "has_children": True,
            "is_enabled": True,
            "refreshable": False,
            "tree_object_key": DATABASE_SOURCE,
            "tree_configuration": configuration,
        }

    def _object_label(self, entry: dict[str, Any]) -> str:
        """What one object reads as.

        The name and, for a routine, its signature -- so two overloads are told
        apart on screen. The same formatting the old tree used, and a label
        only: what addresses the object is the identity beneath it, which the
        server normalized into separate fields.
        """
        identity = dict(entry.get("identity") or {})
        name = str(identity.get("name") or "")
        signature = str(identity.get("signature") or "")
        return f"{name}({signature})" if signature else name

    def _identity(self, entry: dict[str, Any]) -> dict[str, Any]:
        """What this relation is addressed by.

        The fields the schema read already holds, carried onto the record under
        their own names. Which of them build a reference, and for which action,
        is ``tree.node_type_action``'s answer -- this names neither.

        ``ddl`` is among them because the object already has it: the schema
        reader asks the adapter for it once, when the object is built. Dropping
        it here would mean an action had to go and fetch a value the row was
        holding a moment earlier.
        """
        return {
            field: entry[field]
            for field in ("connection", "schema_name", "name", "signature", "statement", "ddl")
            if field in entry
        }


#: The heading a dependency reading reads as, and the heading its result kinds
#: read as. Both are headings that expand and refuse selection, like every
#: other heading in the tree.
_RELATIONSHIP_TYPE = "reference_group"
_RESULT_GROUP_TYPE = "reference_group"

#: A node that reports the INDEX's coverage rather than a thing in the
#: database. NEW VOCABULARY, and deliberate: without it, an object the index
#: has never seen is indistinguishable from one that genuinely depends on
#: nothing, which is the negative-trust failure this estate keeps paying for.
_INDEX_STATUS_TYPE = "index_status"

#: Which collection an object type belongs to, for the icon. The reverse of
#: _MEMBER_TYPE, which maps the collection to the member.
_COLLECTION_OF = {
    "table": "tables", "view": "views",
    "procedure": "procedures", "function": "functions",
}

#: What each level *is*. The plural names the heading, the singular one of its
#: members -- the convention the collection keys already follow.
_CATALOG_TYPE = "catalog"
_SCHEMA_TYPE = "schema"
_MEMBER_TYPE = {
    "tables": "table",
    "views": "view",
    "procedures": "procedure",
    "functions": "function",
}

#: What each collection's members read as.
_COLLECTION_ICON = {
    "tables": "table",
    "views": "view",
    "procedures": "procedure",
    "functions": "function",
}

#: A heading over part of what an object is made of. It expands and refuses
#: selection, like every other heading in the tree.
_STRUCTURE_TYPE = "structure_group"

#: What an object is made of, in the order its headings are listed: (key,
#: label, icon). Each appears only where the object has some.
_STRUCTURES = (
    ("columns", "Columns", "table-column"),
    ("indexes", "Indexes", "list-details"),
    ("constraints", "Constraints", "validation"),
)

#: The groups beneath Constraints, in their stated order.
_CONSTRAINT_GROUPS = (
    ("primary_key", "Primary Keys", "key"),
    ("foreign_keys", "Foreign Keys", "link-plus"),
    ("unique", "Unique", "fingerprint"),
    ("checks", "Checks", "checkbox"),
)

#: What an entry beneath each heading reads as: its heading's mark.
_STRUCTURE_ICON = {
    **{key: icon for key, _label, icon in _STRUCTURES},
    **{key: icon for key, _label, icon in _CONSTRAINT_GROUPS},
}

#: What one constraint in each group is.
_CONSTRAINT_TYPE = {
    "primary_key": "primary_key",
    "foreign_keys": "foreign_key",
    "unique": "unique_constraint",
    "checks": "check_constraint",
}


def _by_name(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries by name ignoring case, the raw name settling a tie."""
    return sorted(entries, key=lambda one: (
        str(one.get("name") or "").casefold(), str(one.get("name") or ""),
    ))


def _indexed_object(configured: dict[str, Any]) -> IndexedDatabaseObject:
    """The object a record stands at, as the code index knows it (backlog 729).

    Its natural identity, signature always carried: two overloads differ only
    there, and the index answers each separately.
    """
    return IndexedDatabaseObject(
        schema=str(configured.get("schema") or ""),
        name=str(configured.get("object") or ""),
        object_type=str(configured.get("object_type") or ""),
        signature=str(configured.get("signature") or ""),
    )


# -- the two reads, and the projection that crosses (moved, backlog 1057) ----


def connection_snapshot(ctx: Any, connection: str) -> dict[str, Any]:
    """
    What one configured connection contains.

    What one configured connection contains -- its catalogs and the schemas in
    them -- is the canonical schema object's answer: ``DatabaseSchema.list``
    (backlog 720). Nothing here reaches the provider adapter, names a provider,
    or decides what a provider supports.

    Parameters
    ----------
    ctx : Any
        Finalized installation context.
    connection : str
        The configured connection, by name.

    Returns
    -------
    dict[str, Any]
        ``catalogs`` -- each distinct catalog of the listed schemas, in the
        order first listed -- and ``schemas``, each with the catalog it sits
        in, composed from ``DatabaseSchema.list``.

    Raises
    ------
    ConfigError
        When no connection is named.
    DatabaseError
        When the connection is not configured or cannot be opened, or its
        schemas cannot be listed -- including by a provider that cannot list
        them, which is said, never shown as a database with none.
    """
    named = str(connection or "").strip()
    if not named:
        raise ConfigError("A connection's contents are read on a named connection.")
    try:
        handle = shared_connection(ctx, named).handle()
    except Exception as exc:  # noqa: BLE001 -- the database-read boundary (#1057)
        raise DatabaseError(str(exc)) from exc
    try:
        listed = DatabaseSchema.list(handle, named)
    except Exception as exc:  # noqa: BLE001 -- the database-read boundary (#1057)
        raise DatabaseError(str(exc)) from exc
    # The connection is not closed: it is shared and outlives this read.
    catalogs = list(dict.fromkeys(schema.catalog for schema in listed))
    return {
        "catalogs": [{"name": catalog} for catalog in catalogs],
        "schemas": [{"catalog": schema.catalog, "name": schema.schema} for schema in listed],
    }


def load_database_schema(
    ctx: Any,
    connection: str,
    catalog: str,
    schema: str,
) -> DatabaseSchema:
    """Return one schema as the objects it holds.

    Reading a schema into the database objects that describe it is
    :meth:`rey_lib.db.database_objects.DatabaseSchema.read`; this resolves the
    shared connection, turns a failed read into a DatabaseError, and adapts the
    one view the tree is loaded from into ``TreeDataObject``.

    Args:
        ctx: Finalized installation context.
        connection: The configured connection, by name.
        catalog: Which catalog the schema sits in, as the caller knows it.
        schema: The schema to read.

    Raises:
        ConfigError: When no connection is named.
        DatabaseError: When the connection cannot be opened or the schema cannot
            be read. A failed read raises rather than returning a half-built
            schema.
    """
    named = str(connection or "").strip()
    if not named:
        raise ConfigError("A schema is read on a named connection.")

    # Intent before the fallible boundary: which connection and schema, never
    # what comes back. The one place every schema read passes through.
    _logger.debug("Reading schema %s.%s on connection %s", catalog, schema, named)
    try:
        handle = shared_connection(ctx, named).handle()
    except Exception as exc:  # noqa: BLE001 -- the database-read boundary (#1057)
        raise DatabaseError(str(exc)) from exc

    try:
        read = DatabaseSchema.read(handle, named, catalog, schema)
    except Exception as exc:  # noqa: BLE001 -- the database-read boundary (#1057)
        raise DatabaseError(str(exc)) from exc
    # The connection is not closed: it is shared and outlives this read.

    if schema != TREE_SCHEMA:
        return read
    return replace(read, views=tuple(_as_tree_view(view) for view in read.views))


def _as_tree_view(view: DatabaseView) -> DatabaseView:
    """The view the tree is loaded from, as TreeDataObject; any other as it is.

    The tree projection arrives as the object it is rather than being
    recognised later by name. Everything else about it is a view's, because it
    is one: it is built from exactly the fields rey_lib read.
    """
    if view.identity.name != TREE_VIEW_NAME:
        return view
    return TreeDataObject(**{field.name: getattr(view, field.name) for field in fields(view)})


def schema_projection(schema: DatabaseSchema) -> dict[str, Any]:
    """Return one schema's objects as the projection that crosses.

    Args:
        schema: The constructed objects.

    Returns:
        The schema's own serialization, with every object carrying what it is
        addressed by. A table and a view also carry the statement that opens
        them -- the select written when they were read. A routine carries one
        where its provider rendered the call, and carries none where it did
        not: opening a routine is a call, and only the provider knows how one
        is written.
    """
    projection = schema.to_dict()
    projection["tables"] = [
        {**table.to_dict(), **_opens(table)} for table in schema.tables
    ]
    projection["views"] = [
        {**view.to_dict(), **_opens(view)} for view in schema.views
    ]
    projection["procedures"] = [
        {**procedure.to_dict(), **_addresses(procedure)}
        for procedure in schema.procedures
    ]
    projection["functions"] = [
        {**function.to_dict(), **_addresses(function)}
        for function in schema.functions
    ]
    return projection


def _addresses(routine: DatabaseProcedure | DatabaseFunction) -> dict[str, str]:
    """What one routine is addressed by.

    A routine keeps its identity nested, because a signature is part of it and a
    relation has none. The tree reads ordinary fields, so the ordinary ones are
    lifted alongside -- the same three a relation carries, under the same names.

    The signature is lifted too (backlog 1102): it is identity, naming the
    routine among its overloads, so a declaration can address exactly this one.
    """
    identity = routine.identity
    addressed = {
        "connection": identity.connection,
        "schema_name": identity.schema,
        "name": identity.name,
        "signature": identity.signature,
    }
    # The statement that opens a routine is the call its provider rendered, as
    # a relation's is the select written when it was read. Carried, never
    # parsed, and omitted entirely where the provider rendered none -- a
    # routine with no statement is addressed by nothing, exactly as a relation
    # with none is.
    if routine.invocation:
        addressed["statement"] = routine.invocation
    return addressed


def _opens(relation: DatabaseTable | DatabaseView) -> dict[str, str]:
    """What one relation is addressed by.

    The identity and nothing else: which action a relation gets, and whether it
    gets one, is the node type's answer through ``tree.node_type_action``. A
    relation with no statement is addressed by nothing, because there is
    nothing to open.
    """
    if not relation.initial_select:
        return {}
    identity = relation.identity
    return {
        "connection": identity.connection,
        "schema_name": identity.schema,
        "name": identity.name,
        "statement": relation.initial_select,
    }
