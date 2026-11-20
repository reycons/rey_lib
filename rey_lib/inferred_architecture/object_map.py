"""The map of the code estate: one live object, three projections (#1119, #1120, #1304).

``ObjectMap`` is the first-class owner of a map's state: what it read, what is
in focus, how a press changes that focus, and the projection of the focused
screen. It is opened on one governed map subject and the subject decides which
readings it holds:

    inferred_architecture {installation}            -> the object and flow readings
    database_schema {connection, catalog, schema}   -> the schema projection

The object projection reads the estate's canonical-object claims, their
relationships, and where the latest inferred-architecture generation places
them. The flow projection (#1304) reads registered application entry points
and their indexed static control flow: statements, decisions, loop and exit
transitions, with coverage evidence. Calls drill into their indexed callee;
unresolved destinations remain explicit. Both are readings of the ONE
live object: ``focus(reading=...)`` switches which is active; the projection
says which it is and which the object holds. The schema projection draws one
database schema from the canonical database read
(``rey_lib.db.database_objects.DatabaseSchema``): the tables and columns stay
the database domain's objects; the map owns only how they are drawn, related
and focused.

A projection is semantic -- nodes, edges, the path back, the focused object's
details, and which node is selected with the connections it highlights. How it
is laid out and styled is the viewer's.

The map owns what an interaction means (#1099): a ``click`` selects a node; a
``double_click`` drills; a ``context_click`` (a right-click) on a claim opens
it. Only the last ever opens, and it answers the canonical identity of the File
the claim shows (#1218) and stops there: which application hosts that File is
the hosting application's own declaration, and how it is presented its
subject's; neither is the map's.
"""

from __future__ import annotations

import json

from typing import Any, Callable, Final

from rey_lib.db.database_objects import (
    DatabaseChildIdentity, DatabaseConstraint, DatabaseObjectListing, DatabaseSchema,
    DatabaseTable, data_type_category, parameter_role,
)
from rey_lib.db.postgres_references import (
    VALUE, carries_forward, derivation_place, expression_head,
)
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.repository_map.indexed_database_object import IndexedDatabaseObject

__all__ = ["CLAIM_NODE_TYPE", "FLOW_PROJECTION", "OBJECT_PROJECTION", "SCHEMA_PROJECTION",
           "ObjectMap"]

#: The node type a claim is governed as -- the record ``record`` answers.
CLAIM_NODE_TYPE: Final = "canonical_object"

#: The three projections: the two an architecture subject holds as its readings,
#: and the one a schema subject opens.
OBJECT_PROJECTION: Final = "object"
SCHEMA_PROJECTION: Final = "schema"
FLOW_PROJECTION: Final = "flow"

#: The five reads the object projection is built from, in the order they are held.
_OBJECT_READS: Final = ("claims", "relationships", "placements", "areas", "area_links")
#: Governed application/command/operation inventory; CFG evidence is read on navigation.
_FLOW_READS: Final = ("flows", "flow_commands", "flow_operations", "flow_requests")

#: Where each reading opens.
_OPENING_FOCUS: Final = {
    OBJECT_PROJECTION: {"level": "estate"},
    FLOW_PROJECTION: {"flow": None},
    SCHEMA_PROJECTION: {"opened": []},
}

#: One element or relation of the map's semantic scope, beside the source row it
#: maps (None where it maps none): the row is what a screen may draw from; the
#: element alone is the map's own identity.
_Mapped = tuple[dict[str, Any], "dict[str, Any] | None"]

_AREA: Final = "area:"
_CLAIM: Final = "claim:"
_TABLE: Final = "table:"
_EXTERNAL: Final = "external:"
_FLOW: Final = "flow:"
_APPLICATION: Final = "application:"
_SYMBOL: Final = "symbol:"
_BOUNDARY: Final = "boundary:"
#: A level-1 column flow (#1426): input column -> output column, its transforms hidden.
_COLUMN_FLOW_EDGE: Final = "column_flow:"
#: A table's or view's header row (#1426): it names the object; a click selects it.
_HEADER: Final = "header:"
#: The +/- on a header row (#1426): a click shows or hides the object's columns.
_TOGGLE: Final = "toggle:"
#: The connections control on a header row (#1426): a click hides or restores
#: every line into or out of the object, nothing else.
_LINKS: Final = "links:"
#: The relationships control on a header row (#1426): a click hides or restores
#: the object's header-to-header lines only; its column lines stay.
_RELATIONS: Final = "relations:"
#: Each header control that hides a kind of line (#1426), and the focus key that
#: holds the objects whose lines of that kind are hidden: the connections control
#: the column-to-column lines, the relationships control the header-to-header ones.
_HIDDEN_LINES: Final = ((_LINKS, "hidden_connections"), (_RELATIONS, "hidden_relationships"))
#: The generations an object-focused map can hide (#1426 Map Options): the
#: objects one hop from the focused object, and those two hops away.
_GENERATIONS: Final = frozenset({1, 2})
#: The object types any database map can hide (#1426 Map Options).
_OBJECT_TYPES: Final = frozenset({"table", "view", "function", "procedure"})
#: The map's visibility toggles (#1426 Map Options), in the pill's order: what a
#: toggle is ``on``, the focus key holding what is hidden, and the value it hides.
_GENERATION_TOGGLES: Final = (("first_order", "hidden_generations", 1),
                              ("second_order", "hidden_generations", 2))
_TYPE_TOGGLES: Final = (("tables", "hidden_object_types", "table"),
                        ("views", "hidden_object_types", "view"),
                        ("functions", "hidden_object_types", "function"),
                        ("procedures", "hidden_object_types", "procedure"))
#: The acts of the whole view's declared actions (#1426, #1151): on its opened
#: panels, and on its hidden connections.
_EXPAND: Final = "expand"
_COLLAPSE: Final = "collapse"
_HIDE: Final = "hide"
_SHOW: Final = "show"
#: The pill a header's controls sit in (#1426): drawn only, never pressed.
_CONTROLS: Final = "controls:"
#: An ER box's own parts, never marked for a selection: its header and its controls.
_BOX_PARTS: Final = frozenset({"header", "toggle", "links", "relations", "controls", "group"})
#: One column pair of a foreign key drawn between rows or boxes (#1426).
_FK_COLUMN_EDGE: Final = "fk_column:"
#: The column keys of an object the map holds no catalog read of (#1441): none known --
#: the same three DatabaseTable.column_keys answers, each empty.
_NO_COLUMN_KEYS: Final = {"primary_key": frozenset(), "foreign_key": frozenset(),
                          "unique": frozenset()}

#: Column lineage levels on an opened table or view (#1426). Both draw the same
#: #1425 relationships: column flow hides the transforms between two columns,
#: derivation detail shows them with their expressions.
_COLUMN_FLOW: Final = "columns"
_DERIVATION_DETAIL: Final = "transforms"
_LINEAGE_LEVELS: Final = frozenset({_COLUMN_FLOW, _DERIVATION_DETAIL})
#: The toolbar toggle that shows derivation detail (#1426, the user's ruling).
_DERIVATION_DETAIL_LABEL: Final = "Transforms"
#: How far a double-clicked column's lineage reaches (#1426): all of it -- no
#: depth (the user's ruling, 2026-11-18, replacing 3 graph edges).
_COLUMN_FOCUS_DEPTH: Final[int | None] = None
#: The members whose full lineage the AI may read from the map (#1429): what
#: #1425's derivation graph holds -- a column, a transform, a routine's parameter.
_LINEAGE_MEMBERS: Final = ("column", "transform", "parameter")
#: The focused lineage reads left to right, as the approved visual (#1426) shows.
_COLUMN_FOCUS_DIRECTION: Final = "RIGHT"
#: The focused lineage's key, in the theme's colours as the schema style draws them.
_COLUMN_FOCUS_LEGEND: Final = (
    {"label": "Table / View", "shape": "round-rectangle", "color": "var(--border)"},
    {"label": "Column", "shape": "round-rectangle", "color": "var(--ink)"},
    {"label": "Transform", "shape": "diamond", "color": "var(--progress-fill)"},
    {"label": "Column derivation (flow)", "shape": "line", "color": "var(--progress-fill)"},
)


def _transform_label(role: str, expression: str | None, key: str) -> str:
    """A transform's short label (#1426): where it sits, what it produces and from what.

    A value transform reads ``definition.ai ← jsonb_build_object``: the CTE or
    subquery it is in (none for the statement's own query), the name it
    produces, and what its expression applies -- or its role, where nothing
    short names that. A join, filter or group is its role. Both readings are
    #1425's analyser's own (``derivation_place``, ``expression_head``).
    """
    if role != VALUE:
        return role
    place, name = derivation_place(key, role)
    applies = expression_head(expression or "") or role
    if not name:
        return f"{role} → {applies}"
    return f"{place + '.' if place else ''}{name} ← {applies}"


def _transforms_anything(role: str, expression: str | None) -> bool:
    """Whether a transform is drawn on the map (#1426): every one but a value that
    only carries a column forward -- its line runs column to column instead."""
    return not (role == VALUE and carries_forward(expression or ""))


def _mark_holding_objects(nodes: list[dict[str, Any]], holding: set[str],
                          selected: str | None) -> None:
    """A column selection read at object level (#1426 design): the object holding
    the selected column is ``focus``, every other object holding a marked column
    ``first`` -- the map's own marks, which the panels show as their fill."""
    owner = next((node.get("parent") for node in nodes if node["id"] == selected), None)
    for node in nodes:
        if node["id"] in holding and not node.get("highlight"):
            node["highlight"] = ["focus"] if node["id"] == owner else ["first"]


def _line_columns(nodes: list[dict[str, Any]], edge: dict[str, Any] | None,
                  display: dict[str, str]) -> set[str]:
    """The drawn column rows a selected line joins (#1426): shown as selected columns.

    A foreign key's columns and a derivation's two columns, where each is drawn as a
    row; an end in a collapsed object, or a line that names no column, gives none.
    """
    if edge is None:
        return set()
    ends = {display.get(one, one) for one in (edge.get("from", edge["source"]),
                                              edge.get("to", edge["target"]))}
    return {node["id"] for node in nodes if node["id"] in ends and node.get("kind") == "column"}


def _object_type_of(node: dict[str, Any]) -> str | None:
    """A drawn object's database type (#1426 Map Options): its identity's, or a
    schema overview's table (an external table too) or view; None for code."""
    stated = (node.get("identity") or {}).get("object_type")
    if stated:
        return str(stated)
    return {"table": "table", "external": "table", "view": "view"}.get(str(node.get("kind") or ""))


def _evidence_rows(edge: dict[str, Any]) -> list[list[Any]]:
    """A derivation's evidence, one details row each."""
    return [["Evidence", f"{evidence['fact_source']} · {evidence['resolution']}"
                         f" · {evidence['evidence']}"]
            for evidence in edge.get("evidence") or []]
#: What may be opened to show its columns (decision (d): tables and views).
_OPENABLE: Final = frozenset({"table", "view"})
#: The database routines that open like a view (#1426): a function or procedure
#: shows its parameters, its inputs and its outputs, as a view shows its columns.
_OPENABLE_ROUTINES: Final = frozenset({"function", "procedure"})
#: A function's parameter groups (#1426), in panel order: each group's heading
#: and the parameter roles it holds (``parameter_role`` of the member's mode).
_PARAMETER_GROUPS: Final = (("inputs:", "Inputs", frozenset({"input"})),
                            ("outputs:", "Outputs", frozenset({"output"})))

#: A claim's owner file is a configured file beneath the apps root: its
#: ``owner_subject_id`` is the path relative to it (#1218).
_CLAIM_FILE_KIND: Final = "configured_file"
_CLAIM_FILE_ROOT: Final = "{apps}"


class ObjectMap:
    """One map subject, read, held live, focused and projected.

    It states no presentation (#1215): its governed subject's presentation is
    resolved by Projection.for_subject.
    """

    def __init__(self, subject: dict[str, Any], projection_kind: str,
                 read: dict[str, Any] | DatabaseSchema) -> None:
        """Hold what was read. Use ``for_architecture`` or ``for_schema``.

        A schema map's read is the ``DatabaseSchema`` itself (#1441): its tables,
        views and routines as the database domain's own objects, never their
        serialization. Every other reading's read is its governed reads' rows.
        """
        self.subject = dict(subject)
        self.projection_kind = projection_kind
        self._schema: DatabaseSchema | None = (
            read if projection_kind == SCHEMA_PROJECTION and isinstance(read, DatabaseSchema) else None)
        if projection_kind == SCHEMA_PROJECTION and self._schema is None:
            raise ConfigError("A schema map reads its schema as a DatabaseSchema.")
        self._read: dict[str, Any] = read if isinstance(read, dict) else {}
        #: The readings this one object holds (#1304): the active one is
        #: ``projection_kind``; ``focus(reading=...)`` moves between them. An
        #: architecture map read with its flows holds the flow reading too.
        self._readings: tuple[str, ...] = (projection_kind,) + (
            (FLOW_PROJECTION,) if projection_kind == OBJECT_PROJECTION and "flows" in read
            else ())
        #: Each inactive reading's focus and path, kept so a tab comes back as left.
        self._kept: dict[str, dict[str, Any]] = {}
        # The object projection opens on the estate; the flow projection on its
        # flows; the schema projection on every table closed to its name.
        self._focus: dict[str, Any] = dict(_OPENING_FOCUS[projection_kind])
        self._project: dict[str, Callable[[], dict[str, Any]]] = {
            OBJECT_PROJECTION: self._object_projection,
            SCHEMA_PROJECTION: self._schema_projection,
            FLOW_PROJECTION: self._flow_projection,
        }
        self._drill: dict[str, Callable[[str], None]] = {
            OBJECT_PROJECTION: self._object_drill,
            SCHEMA_PROJECTION: self._schema_drill,
            FLOW_PROJECTION: self._flow_drill,
        }
        self._holds: dict[str, Callable[[str], bool]] = {
            OBJECT_PROJECTION: self._holds_object_node,
            SCHEMA_PROJECTION: self._holds_schema_node,
            FLOW_PROJECTION: self._holds_flow_node,
        }
        #: The selected node (#1099), which a click sets and the projection
        #: highlights. None when nothing is selected.
        self._selected: str | None = None
        self._index_connection: Any = None
        #: The runtime's shared Control: every index read is its governed read (#1360).
        self._control: Any = None
        self._index_installation = ""
        self._database_provider = ""
        self._table_context: dict[str, Any] | None = None
        #: A focused column's lineage (#1426), as its read answered; the screen it draws.
        self._column_focus: dict[str, Any] | None = None
        #: The column rows the schema projection last drew: a double-click focuses one.
        self._column_rows: set[str] = set()
        #: Each drawn column row's identity -- schema, object, object_type, column --
        #: what an explain gesture names it by (#1399, #1426).
        self._member_identities: dict[str, dict[str, str]] = {}
        self._context_path: list[dict[str, Any]] = []
        self._edge_inspection: dict[str, Any] | None = None
        self._layout_control: Any = None
        self._layouts: dict[str, dict[str, Any]] = {}
        #: The column, transform and derivation-edge ids the schema projection
        #: last drew for its opened tables and views (#1426). Selectable; they
        #: never open or drill.
        self._lineage_ids: set[str] = set()

    @property
    def _database(self) -> DatabaseSchema:
        """The schema this map reads (#1441); a map of no schema has none to give.

        Raises:
            ConfigError: When this map reads no database schema.
        """
        if self._schema is None:
            raise ConfigError("This map reads no database schema.")
        return self._schema

    def bind_layout(self, control: Any) -> None:
        """Use the existing Control for this map's geometry, without replacing the map."""
        self._layout_control = control

    def _layout_scope(self) -> dict[str, Any]:
        return {"reading": self.projection_kind, "focus": dict(self._focus)}

    def _layout(self, scope: dict[str, Any]) -> dict[str, Any]:
        if self._layout_control is None:
            raise ConfigError("This map has no layout persistence connection.")
        key = json.dumps(scope, sort_keys=True)
        if key not in self._layouts:
            if scope != self._layout_scope():
                raise ConfigError("This map has not opened the requested layout scope.")
            row = self._layout_control.map_layout(scope["reading"], self.subject, scope["focus"])
            geometry = row["geometry"] if row else None
            self._layouts[key] = {"geometry": geometry, "saved": geometry,
                                  "revision": int(row["revision"]) if row else 0}
        return self._layouts[key]

    def edit_layout(self, geometry: dict[str, Any], scope: dict[str, Any]) -> None:
        """Hold a reported geometry edit; only Save writes it."""
        if self._layout_control is None:
            raise ConfigError("This map has no layout persistence connection.")
        if not isinstance(geometry, dict) or not {"elements", "positions", "pan", "zoom"} <= geometry.keys():
            raise ConfigError("A map layout edit requires its graph geometry.")
        # ``routes``: the reader's routing of the lines they moved (#1426), by line id.
        if geometry.keys() - {"elements", "positions", "pan", "zoom", "viewport", "routes"}:
            raise ConfigError("A map layout contains geometry only.")
        try:
            copied = json.loads(json.dumps(geometry, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ConfigError("A map layout must contain finite JSON geometry.") from exc
        self._layout(scope)["geometry"] = copied

    def save(self, scope: dict[str, Any]) -> None:
        """Save the held geometry and re-read its authoritative revision."""
        held = self._layout(scope)
        if held["geometry"] is None:
            raise ConfigError("This map has no geometry to save.")
        self._layout_control.save_map_layout(scope["reading"], self.subject, scope["focus"],
                                             held["geometry"], held["revision"])
        row = self._layout_control.map_layout(scope["reading"], self.subject, scope["focus"])
        if row is None:
            raise ConfigError("The saved map layout could not be recovered.")
        held.update(geometry=row["geometry"], saved=row["geometry"], revision=int(row["revision"]))

    def load(self, scope: dict[str, Any]) -> None:
        """Hold the saved arrangement again for this scope (#1426); nothing is written."""
        held = self._layout(scope)
        if held["saved"] is None:
            raise ConfigError("This map has no saved layout to open.")
        held["geometry"] = json.loads(json.dumps(held["saved"]))

    def layout_projection(self) -> dict[str, Any]:
        """Geometry and the exact commands this live map permits."""
        scope = self._layout_scope()
        held = self._layout(scope)
        actions = [{"role": {"act": "set", "on": "map_layout"},
                    "command": {"command": "map", "action": "edit_layout", "scope": scope},
                    "inputs": {"geometry": ["geometry"]}}]
        if held["geometry"] is not None and held["geometry"] != held["saved"]:
            actions.append({"role": {"act": "save", "on": "map_layout"},
                            "command": {"command": "map", "action": "save", "scope": scope}})
        # Open from save (#1426): only where this scope has a saved arrangement.
        if held["saved"] is not None:
            actions.append({"role": {"act": "load", "on": "map_layout"},
                            "command": {"command": "map", "action": "load", "scope": scope}})
        return {"key": json.dumps(scope, sort_keys=True), "revision": held["revision"],
                "geometry": json.loads(json.dumps(held["geometry"])), "actions": actions}

    # -- opening ---------------------------------------------------------------

    @classmethod
    def for_architecture(cls, connection: Any, installation: str, control: Any) -> "ObjectMap":
        """The estate's object map: its five reads through the shared Control (#1360).

        Args:
            connection: The shared Connection the host resolved, held for the
                map's later index reads; never opened or closed here.
            installation: The installation the map subject names.
            control: The runtime's shared Control, whose governed reads
                (``code.f_object_map_*``) answer the five object-map reads.
        """
        readers = {
            "claims": control.object_map_claims,
            "relationships": control.object_map_relationships,
            "placements": control.object_map_placements,
            "areas": control.object_map_areas,
            "area_links": control.object_map_area_links,
        }
        read = {name: [dict(row) for row in readers[name]()] for name in _OBJECT_READS}
        held = cls({"kind": "inferred_architecture", "installation": installation},
                   OBJECT_PROJECTION, read)
        held._index_connection = connection
        held._control = control
        return held

    @classmethod
    def for_application(cls, connection: Any, application: str, control: Any) -> "ObjectMap":
        """The selected application's input flows, with no estate-level navigation.

        The four flow reads are the shared Control's governed reads (#1360).
        """
        readers = {
            "flows": control.object_map_flows,
            "flow_commands": control.object_map_flow_commands,
            "flow_operations": control.object_map_flow_operations,
            "flow_requests": control.object_map_flow_requests,
        }
        read = {name: [dict(row) for row in readers[name]()] for name in _FLOW_READS}
        read["flows"] = [row for row in read["flows"] if row["entry_repository_key"] == application]
        for name in ("flow_commands", "flow_operations", "flow_requests"):
            read[name] = [row for row in read[name] if row["application_name"] == application]
        held = cls({"kind": "application", "application": application}, FLOW_PROJECTION, read)
        held._index_connection = connection
        held._control = control
        held.focus(application=application)
        return held

    @classmethod
    def for_schema(cls, connection: Any, catalog: str, schema: str, *,
                   index_connection: Any = None, installation: str = "",
                   control: Any = None) -> "ObjectMap":
        """One database schema's map, from the canonical database read.

        The schema and its tables are read as the database domain's own objects
        (``DatabaseSchema.read``); the map keeps their serialization to draw.

        Args:
            connection: The shared Connection the subject names, resolved by the
                host; never opened or closed here.
            catalog: The schema's catalog, as the subject names it.
            schema: The schema.
        """
        read = DatabaseSchema.read(connection.handle(), connection.name, catalog, schema)
        held = cls({"kind": "database_schema", "connection": connection.name,
                    "catalog": catalog, "schema": schema}, SCHEMA_PROJECTION, read)
        held._index_connection = index_connection
        held._index_installation = installation
        held._database_provider = str(getattr(connection, "provider", "") or "")
        held._control = control
        return held

    # -- behaviour ---------------------------------------------------------------

    def focus(self, **focus: Any) -> None:
        """Move the focus to the screen named, as the path names it.

        ``reading`` names which of this object's readings the screen is of
        (#1304): given alone it switches the active reading, which comes back
        as it was left; given with a screen it switches and then focuses.

        Raises:
            ConfigError: When the screen names nothing this map holds.
        """
        if "reading" in focus:
            reading = str(focus.pop("reading") or "")
            if reading not in self._readings:
                raise ConfigError(f"This map holds no reading {reading!r}.")
            if reading != self.projection_kind:
                self._switch_reading(reading)
            if not focus:
                return
        if self.projection_kind == FLOW_PROJECTION:
            self._focus_flow(focus)
            return
        if self.projection_kind == SCHEMA_PROJECTION:
            # Opened: a table by name, or a table or view on the focused screen
            # by its node (#1426). Checked once the screen is known, below.
            opened = [str(name) for name in focus.get("opened") or []]
            lineage = str(focus.get("lineage") or self._focus.get("lineage") or _COLUMN_FLOW)
            if lineage not in _LINEAGE_LEVELS:
                raise ConfigError(f"The map has no lineage level {lineage!r}.")
            table = str(focus.get("table") or "")
            node = str(focus.get("node") or "")
            column = str(focus.get("column") or "")
            object_focus = focus.get("object")
            # The display options (#1426 Map Options): which generations and object
            # types are hidden. They filter what this map holds; they never read it.
            hidden_generations = sorted({int(one) for one in focus.get("hidden_generations") or []})
            if set(hidden_generations) - _GENERATIONS:
                raise ConfigError(f"The map has no generation {hidden_generations}.")
            hidden_types = sorted({str(one) for one in focus.get("hidden_object_types") or []})
            if set(hidden_types) - _OBJECT_TYPES:
                raise ConfigError(f"The map has no object type {hidden_types}.")
            if isinstance(object_focus, dict):
                object_focus = {"name": str(object_focus.get("name") or ""),
                                "object_type": str(object_focus.get("object_type") or ""),
                                "signature": str(object_focus.get("signature") or "")}
            # The same object focused again -- a display option changed -- keeps the
            # context this map already read for it.
            same_object = (not column and self._table_context is not None
                           and table == str(self._focus.get("table") or "")
                           and node == str(self._focus.get("node") or "")
                           and object_focus == self._focus.get("object"))
            step = None
            if column:
                # A column's own lineage (#1426), to the map's depth.
                context = self._read_column_focus(column)
                step = {"value": column, "label": context["label"], "focus": {"column": column}}
            elif object_focus is not None:
                if not isinstance(object_focus, dict):
                    raise ConfigError("A database-object focus must be an identity.")
                kind = object_focus["object_type"]
                held = self._database
                entries = {"view": held.views, "function": held.functions,
                           "procedure": held.procedures}.get(kind, ())
                matches = [entry for entry in entries
                           if entry.identity.name == object_focus["name"]
                           and str(getattr(entry.identity, "signature", "") or "") == object_focus["signature"]]
                if len(matches) != 1:
                    raise AppError("This schema does not hold exactly one matching view or routine.")
                context = self._table_context if same_object else self._read_table_context(
                    object_focus["name"], object_type=kind, signature=object_focus["signature"])
                # Its displayed name is its identity's name -- the name it matched by.
                step = {"value": context["root_id"], "label": matches[0].identity.name,
                        "focus": {"object": object_focus}}
            elif table:
                if table not in self._table_names():
                    raise AppError(f"This schema holds no table {table!r}.")
                context = self._table_context if same_object else self._read_table_context(table)
                step = {"value": f"table:{table}", "label": table, "focus": {"table": table}}
            elif node:
                known = next((n for n, _ in self._schema_scope()[0] if n["id"] == node), None)
                previous = next((s for s in self._context_path if s["value"] == node), None)
                if known is None and previous is None:
                    raise AppError(f"This map holds no indexed node {node!r}.")
                label = (known or previous or {}).get("label", node)
                if same_object:
                    context = self._table_context
                elif node.startswith(_EXTERNAL):
                    target = next((key for t in self._database.tables for key in t.foreign_keys
                                   if f"{_EXTERNAL}{key.referenced_schema}.{key.referenced_table}" == node),
                                  None)
                    if target is None:
                        raise AppError(f"This map holds no external table {node!r}.")
                    context = self._read_table_context(target.referenced_table,
                                                       schema=target.referenced_schema, alias=node)
                else:
                    context = self._read_table_context(node=node)
                step = {"value": node, "label": label, "focus": {"node": node}}
            names = self._table_names()
            openable = set(names) | {f"{_TABLE}{name}" for name in names}
            if step is not None and not column:
                openable |= {n["id"] for n in context["nodes"] if self._opens(n)}
            unknown = sorted(set(opened) - openable)
            if unknown:
                raise ConfigError(f"This schema holds no table or view {unknown}.")
            # Objects whose column or header lines are hidden (#1426): objects on
            # this screen.
            drawn = {f"{_TABLE}{name}" for name in names} | (
                {n["id"] for n in context["nodes"]} if step is not None and not column else set())
            hidden_lines: dict[str, list[str]] = {}
            for _, key in _HIDDEN_LINES:
                hidden = [str(one) for one in focus.get(key) or []]
                if set(hidden) - drawn:
                    raise ConfigError(
                        f"This screen draws no object {sorted(set(hidden) - drawn)}.")
                if hidden:
                    hidden_lines[key] = hidden
            if step is not None:
                # Keep only the visited path, not another object or transport registry.
                at = next((i for i, s in enumerate(self._context_path)
                           if s["value"] == step["value"]), len(self._context_path))
                self._context_path = self._context_path[:at] + [step]
                if column:
                    self._column_focus = context
                else:
                    self._table_context = context
            else:
                self._context_path = []
            self._focus = {"opened": opened, **({"table": table} if table else {}),
                           **({"node": node} if node else {}),
                           **({"column": column} if column else {}),
                           **({"object": object_focus} if object_focus is not None else {}),
                           **({"lineage": lineage} if lineage != _COLUMN_FLOW else {}),
                           **({"hidden_generations": hidden_generations} if hidden_generations else {}),
                           **({"hidden_object_types": hidden_types} if hidden_types else {}),
                           **hidden_lines}
            self._edge_inspection = None
            return
        level = str(focus.get("level") or "")
        if level == "estate":
            self._focus = {"level": "estate"}
        elif level == "area" and self._area(focus.get("area_id")) is not None:
            self._focus = {"level": "area", "area_id": int(focus["area_id"])}
        elif level == "object" and self._claim(focus.get("claim_id")) is not None:
            area_id = focus.get("area_id")
            self._focus = {"level": "object", "claim_id": int(focus["claim_id"]),
                           "area_id": int(area_id) if area_id is not None else None}
        else:
            raise ConfigError(f"The map has no screen {focus!r}.")
        if level == "estate":
            self._context_path = []
        elif level == "area":
            area = self._area(self._focus["area_id"]) or {}
            self._context_path = [{"value": f"area:{self._focus['area_id']}",
                                   "label": area.get("area_label", ""), "focus": dict(self._focus)}]
        else:
            area_id = self._focus.get("area_id")
            area_value = f"area:{area_id}"
            if area_id is not None and not any(s["value"] == area_value for s in self._context_path):
                area = self._area(area_id) or {}
                self._context_path = [{"value": area_value, "label": area.get("area_label", ""),
                                       "focus": {"level": "area", "area_id": area_id}}]
            elif area_id is None and any(s["focus"].get("level") == "area" for s in self._context_path):
                self._context_path = []
            claim = self._claim(self._focus["claim_id"]) or {}
            value = f"object:{self._focus['claim_id']}"
            at = next((i for i, s in enumerate(self._context_path) if s["value"] == value),
                      len(self._context_path))
            self._context_path = self._context_path[:at] + [{
                "value": value, "label": claim.get("owner_qualified_name") or claim.get("object_key", ""),
                "focus": dict(self._focus)}]
        self._edge_inspection = None

    def _switch_reading(self, reading: str) -> None:
        """Make ``reading`` the active one: the current reading's focus and path are
        kept, the named reading's are restored or opened; the selection is the
        screen's and does not travel."""
        self._kept[self.projection_kind] = {"focus": dict(self._focus),
                                            "path": [dict(s) for s in self._context_path]}
        kept = self._kept.get(reading)
        self._focus = dict(kept["focus"]) if kept else dict(_OPENING_FOCUS[reading])
        self._context_path = [dict(s) for s in kept["path"]] if kept else []
        self._selected = None
        self._edge_inspection = None
        self.projection_kind = reading

    def _focus_flow(self, focus: dict[str, Any]) -> None:
        """Application declarations, proved operation branches, or exact function flow."""
        key = focus.get("flow")
        application = focus.get("application")
        owned_application = self.subject.get("application") if self.subject.get("kind") == "application" else None
        if owned_application:
            if application and application != owned_application:
                raise AppError("This map belongs to another application.")
            application = owned_application
        if not key:
            if application and not owned_application and not any(f["entry_repository_key"] == application
                                       for f in self._read.get("flows", [])):
                raise AppError(f"This map holds no application {application!r}.")
            self._focus = {"flow": None, **({"application": application} if application else {})}
            self._context_path = ([{"value": f"{_APPLICATION}{application}", "label": application,
                                    "focus": dict(self._focus)}] if application else [])
        else:
            flow = self._flow(str(key))
            if flow is None:
                raise AppError(f"This map holds no flow {key!r}.")
            application = flow["entry_repository_key"]
            command_id = focus.get("command_id")
            operation_id = focus.get("operation_id")
            request_id = focus.get("request_id")
            if sum(v is not None for v in (command_id, operation_id, request_id)) > 1:
                raise AppError("Choose one CLI command, request, or operation.")
            bindings = [r for r in self._read.get("flow_commands", [])
                        if r["application_name"] == application
                        and r["application_command_id"] == command_id] if command_id is not None else []
            operations = [r for r in self._read.get("flow_operations", [])
                          if r["application_name"] == application
                          and r["application_operation_id"] == operation_id] if operation_id is not None else []
            requests = [r for r in self._read.get("flow_requests", [])
                        if r["application_name"] == application
                        and r["application_request_id"] == request_id] if request_id is not None else []
            if request_id is not None and not requests:
                raise AppError("This application holds no such request.")
            if command_id is not None and not bindings:
                raise AppError("This application declares no such command.")
            if operation_id is not None and not operations:
                raise AppError("This application declares no such operation.")
            symbol_id = focus.get("symbol")
            if symbol_id is not None:
                symbol_id = int(symbol_id)
                visited = any(step["focus"].get("symbol") == symbol_id
                              and step["focus"].get("flow") == key
                              for step in self._context_path)
                available = any(n.get("target_symbol_id") == symbol_id
                                or n.get("expand_symbol_id") == symbol_id
                                for n, _ in self._flow_scope()[0])
                if not visited and not available:
                    raise AppError(f"This flow holds no symbol {symbol_id!r}.")
            source_id = symbol_id if symbol_id is not None else flow.get("entry_code_symbol_id")
            source_ids = (sorted({r["code_symbol_id"] for r in bindings + operations + requests if r.get("code_symbol_id")})
                          if (command_id is not None or operation_id is not None or request_id is not None) and symbol_id is None else
                          [source_id] if source_id is not None else [])
            call_site = focus.get("call_site")
            if call_site is not None:
                visited = any(step["focus"].get("call_site") == call_site
                              and step["focus"].get("symbol") == symbol_id
                              for step in self._context_path)
                available_site = any(row.get("target_code_symbol_id") == symbol_id
                                     and self._flow_call_site(node["id"]) == call_site
                                     for node, row in self._flow_scope()[0] if row)
                if symbol_id is None or not (visited or available_site):
                    raise AppError("This flow does not hold that call-site context.")
            # Read each proved implementation through the shared Control (#1360).
            # A leaf row still carries its indexed source metadata.
            if self._control is not None and source_ids:
                steps = []
                for current_id in source_ids:
                    rows = [dict(row) for row in self._control.object_map_flow_steps(current_id)]
                    if not rows:
                        raise AppError(f"The index no longer holds symbol {current_id!r}.")
                    steps.extend(rows)
                self._read["flow_steps"] = steps
            self._focus = {"flow": flow["flow_key"],
                           **({"command_id": command_id} if command_id is not None else {}),
                           **({"operation_id": operation_id} if operation_id is not None else {}),
                           **({"request_id": request_id} if request_id is not None else {}),
                           **({"symbol": symbol_id} if symbol_id is not None else {}),
                           **({"detail": True} if focus.get("detail") else {}),
                           **({"call_site": call_site} if call_site is not None else {})}
            base = [{"value": f"{_APPLICATION}{application}", "label": application,
                     "focus": {"application": application, "flow": None}},
                    {"value": f"{_FLOW}{key}", "label": flow.get("label") or key,
                     "focus": {"flow": key}}]
            if command_id is not None or operation_id is not None or request_id is not None:
                base = base[:1]  # A declared operation does not require visiting main first.
                declaration = (bindings or operations or requests)[0]
                declaration_focus = {k: v for k, v in self._focus.items()
                                     if k not in ("symbol", "call_site", "detail")}
                base.append({"value": f"declaration:{command_id}:{operation_id}:{request_id}",
                             "label": declaration.get("command_name") or declaration.get("operation_name") or declaration["request_name"],
                             "focus": declaration_focus})
            if symbol_id is None:
                self._context_path = base
                if self._focus.get("detail"):
                    self._context_path.append({"value": f"detail:{key}:{command_id}:{operation_id}:{request_id}",
                        "label": "Function detail", "focus": dict(self._focus)})
            else:
                source = next((r for r in self._read.get("flow_steps", [])
                               if r.get("code_symbol_id") == symbol_id), {})
                value = (f"{_SYMBOL}{symbol_id}:{call_site['caller_symbol_id']}:{call_site['node_key']}"
                         if call_site else f"{_SYMBOL}{symbol_id}")
                if self._focus.get("detail"):
                    value += ":detail"
                path = self._context_path if any(
                    step["focus"].get("flow") == key
                    and step["focus"].get("command_id") == command_id
                    and step["focus"].get("operation_id") == operation_id
                    and step["focus"].get("request_id") == request_id
                    for step in self._context_path) else base
                at = next((i for i, step in enumerate(path) if step["value"] == value), len(path))
                self._context_path = path[:at] + [{"value": value,
                    "label": source.get("dotted_identity") or value, "focus": dict(self._focus)}]
        self._edge_inspection = None

    def click(self, node_id: str | None) -> None:
        """Select (#1099): the node becomes the selection; the projection
        highlights its connections. A click on no node clears it. Never opens.

        Whether a node is one this map holds is its own inventory's answer,
        checked before anything changes.

        Raises:
            ConfigError: When the node is none this map holds.
        """
        if not node_id:
            self._selected = None
            self._edge_inspection = None
            return
        node_id = str(node_id)
        if node_id.startswith(_TOGGLE) and node_id in self._lineage_ids:
            # The header's +/- (#1426) shows or hides its object's columns; the
            # selection stays as it was.
            self._toggle_open(node_id[len(_TOGGLE):])
            return
        for prefix, key in _HIDDEN_LINES:
            if node_id.startswith(prefix) and node_id in self._lineage_ids:
                # The header's connections control hides or restores the object's
                # column lines; its relationships control its header lines.
                self._toggle_connections(node_id[len(prefix):], key)
                return
        # A header names its object: a click on it selects that object.
        node_id = self._header_object(node_id)
        if node_id not in self._lineage_ids and not self._holds[self.projection_kind](node_id) \
                and not any(e["id"] == node_id for e, _ in self._scope()[1]):
            raise ConfigError(f"The map has no node {node_id!r}.")
        self._selected = node_id
        self._edge_inspection = None

    def context_click(self, node_id: str) -> dict[str, Any] | None:
        """Open (#1099, #1218): a right-click on a claim opens the subject it represents.

        Validity is this map's own inventory's answer, never the screen on show,
        so the same claim opens from any focus. A node this map holds that is no
        claim has no opening behavior; an area leaves the focus unchanged.
        Database-map nodes return their existing database or source-file opening.
        A claim shows its owner's File, and the map says only which File that is:
        its canonical identity, a configured file beneath the apps root.

        Returns:
            The selected subject's standard identity envelope, naming no
            application or panel. None for an architecture area.

        Raises:
            ConfigError: When the node is none this map holds, or the claim names
                no owner file.
        """
        node_id = self._header_object(str(node_id or ""))
        if self.projection_kind in (SCHEMA_PROJECTION, FLOW_PROJECTION) and (
                node_id in self._lineage_ids
                or any(edge["id"] == node_id for edge, _ in self._scope()[1])):
            # A column, transform or derivation (#1426) opens nothing: it is selected.
            self.click(node_id)
            return
        if self.projection_kind == FLOW_PROJECTION:
            return self._flow_opening(node_id)
        edge = self._mapped_edge(node_id)
        if edge is not None:
            rows = (self._edge_inspection["evidence"] if self._edge_inspection is not None
                    and self._edge_inspection["id"] == node_id else self._read_edge_evidence(edge[1]))
            files = {(r["repository_key"], r["relative_path"]) for r in rows
                     if r.get("repository_key") and r.get("relative_path")}
            if not files:
                raise AppError("This relationship has no indexed source file to open.")
            if len(files) != 1:
                raise AppError(f"This relationship has {len(files)} source files; inspect its indexed evidence.")
            repository, path = next(iter(files))
            return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                                 "relative_path": f"{repository}/{path}"}}
        if not self._holds[self.projection_kind](node_id):
            raise ConfigError(f"The map has no node {node_id!r}.")
        if self.projection_kind == SCHEMA_PROJECTION:
            element, entry = next((n, row) for n, row in self._schema_scope()[0] if n["id"] == node_id)
            identity = dict(element.get("identity") or (entry.identity.to_dict() if entry else {}))
            if element["kind"] == "external":
                key = next(k for t in self._database.tables for k in t.foreign_keys
                           if f"{_EXTERNAL}{k.referenced_schema}.{k.referenced_table}" == node_id)
                identity = {"schema": key.referenced_schema, "name": key.referenced_table,
                            "object_type": "table"}
            if not identity:
                raise AppError("This map node carries no declared opening identity.")
            if identity.get("repository_key") and identity.get("relative_path"):
                return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                                     "relative_path": f"{identity['repository_key']}/{identity['relative_path']}"}}
            kind = str(identity.get("object_type") or element["kind"])
            owner = DatabaseObjectListing.object_type(f"{kind}s")
            parent: dict[str, Any] = {}
            if owner.IDENTITY is DatabaseChildIdentity:
                if identity.get("table"):
                    parent = {"schema": identity["schema"], "table": identity["table"]}
                elif kind == "trigger":
                    # The index already declares the trigger's owning table by
                    # its 'trigger' relation. Use that fact, never the focus or
                    # a name parsed out of a node key/definition.
                    nodes, edges = self._schema_scope()
                    targets = {edge["target"] for edge, _ in edges
                               if edge["source"] == node_id and edge["kind"] == "trigger"}
                    tables = [dict(node.get("identity") or (row or {}).get("identity") or {})
                              for node, row in nodes if node["id"] in targets]
                    if len(tables) == 1 and tables[0].get("object_type") == "table":
                        parent = {"schema": tables[0]["schema"], "table": tables[0]["name"]}
                if not parent:
                    raise AppError("This map object has no unambiguous declared parent table.")
            return {"identity": {"kind": "database_object", "subject_kind": owner.KIND,
                                 "connection": self.subject["connection"], "catalog": self.subject["catalog"],
                                 "schema": identity["schema"], "name": identity["name"],
                                 **parent,
                                 **({"signature": identity.get("signature", "")}
                                    if kind in ("function", "procedure") else {})}}
        claim = self._claim_keyed(node_id)
        if claim is None:
            return None
        relative_path = str(claim.get("owner_subject_id") or "")
        if not relative_path:
            raise ConfigError("This claim names no owner file to open.")
        return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                             "relative_path": relative_path}}

    def double_click(self, node_id: str) -> None:
        """Drill into the selected object's map context, for either projection."""
        node_id = self._header_object(str(node_id or ""))
        if self.projection_kind == SCHEMA_PROJECTION and node_id in self._column_rows:
            # A column row (#1426): the map focuses on that column's lineage.
            names = set(self._table_names())
            self.focus(column=node_id,
                       opened=[one for one in self._focus.get("opened", []) if one in names])
            self._selected = node_id
            self._edge_inspection = None
            return
        if self.projection_kind in (SCHEMA_PROJECTION, FLOW_PROJECTION) and (
                node_id in self._lineage_ids
                or any(edge["id"] == node_id for edge, _ in self._scope()[1])):
            self.click(node_id)
            return
        edge = self._mapped_edge(node_id)
        if edge is not None:
            evidence = self._read_edge_evidence(edge[1])
            self._edge_inspection = {"id": node_id, "evidence": evidence}
            self._selected = node_id
            return
        if not self._holds[self.projection_kind](node_id):
            raise ConfigError(f"The map has no node {node_id!r}.")
        self._drill[self.projection_kind](node_id)
        self._selected = None
        self._edge_inspection = None

    def _mapped_edge(self, edge_id: str) -> _Mapped | None:
        """A visible code-map relationship and its existing index row."""
        if self.projection_kind != OBJECT_PROJECTION:
            return None
        return next((pair for pair in self._scope()[1] if pair[0]["id"] == edge_id), None)

    def _read_edge_evidence(self, row: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Read supporting call sites through the existing canonical-region view."""
        if row is None or self._control is None:
            raise AppError("This map has no configured index connection for relationship evidence.")
        if "canonical_object_id" in row:
            sources = [row["canonical_object_id"]]
            targets = [row["linked_canonical_object_id"]]
        else:
            sources = sorted({p["canonical_object_id"] for p in self._read.get("placements", [])
                              if p["inferred_architecture_area_id"] == row["from_area_id"]})
            targets = sorted({p["canonical_object_id"] for p in self._read.get("placements", [])
                              if p["inferred_architecture_area_id"] == row["to_area_id"]})
        if not sources or not targets:
            return []
        return [dict(r) for r in self._control.object_map_edge_evidence(sources, targets)]

    def projection(self) -> dict[str, Any]:
        """The focused screen: nodes, edges, the path back, the details, and the
        selected node with the connections it highlights -- beside its subject
        identity (``subject``) and which screen it is (``projection``)."""
        screen = self._project[self.projection_kind]()
        # A column-lineage selection (#1426) is marked by the schema projection
        # itself: upstream, downstream, and everything else subdued.
        selected = (screen.pop("selected") if screen.pop("marked", False)
                    else self._marked(screen["nodes"], screen["edges"]))
        return {
            "subject": dict(self.subject),
            "projection": self.projection_kind,
            **({"layout": self.layout_projection()} if self._layout_control is not None else {}),
            # Every reading this one object holds (#1304); the active one is ``projection``.
            "readings": list(self._readings),
            "focus": dict(self._focus),
            "selected": selected,
            **screen,
        }

    def inspect(self) -> dict[str, Any]:
        """What this map is, where it is focused, and the map structure there (#1199 S4).

        The map's own semantic state, independent of how it is drawn: its subject,
        which map it is, its focus and the path to it, its selection (under the
        map's own rule: a selection belongs to its screen), and the elements and
        relations of the FOCUSED scope as map identities -- id, kind, label, the
        identity they map and their role relative to the focus. It identifies and
        relates the mapped things; it does not redefine, evaluate, measure or
        describe them (no claim details or outcome, counts, weights, direct flags,
        columns), and it carries no presentation (panel type, legend, highlight).
        Read only: nothing moves.
        """
        elements, relations = self._scope()
        held = [dict(element) for element, _ in elements]
        return {
            "subject": dict(self.subject),
            "map": self.projection_kind,
            "focus": dict(self._focus),
            "path": [{"value": step["value"], "label": step["label"]} for step in self._path()],
            "selected": self._selection_in({element["id"] for element in held}
                                           | {edge["id"] for edge, _ in relations}),
            "elements": held,
            "relations": [dict(relation) for relation, _ in relations],
        }

    def explain_member(self, node_id: str) -> dict[str, Any] | None:
        """The member an explain gesture names (#1399, #1426); None when the node is none.

        A drawn column row, transform or line is a member of this running map
        (#1429): it becomes the map's selection -- the state its lineage read
        answers -- and its identity is what the BFF explains from this map
        (``member`` beside what the map holds of it). An object -- a panel or its
        header -- is not a member: it goes on to be opened by its owner, as before.
        """
        node_id = str(node_id or "")
        if self.projection_kind != SCHEMA_PROJECTION or not node_id:
            return None
        if node_id in self._column_rows:
            self.click(node_id)
            return {"member": node_id, **self._member_identities[node_id]}
        screen = self.projection()
        transform = next((node for node in screen["nodes"]
                          if node["id"] == node_id and node.get("kind") == "transform"), None)
        line = next((edge for edge in screen["edges"] if edge["id"] == node_id), None)
        if transform is None and line is None:
            return None
        self.click(node_id)
        if transform is not None:
            return {"member": node_id, "member_kind": "transform",
                    "label": transform.get("label"), "expression": transform.get("expression")}
        names = {node["id"]: node.get("label") or node["id"] for node in screen["nodes"]}
        return {"member": node_id, "member_kind": "relationship", "relationship": line["kind"],
                "source": names.get(line["source"], line["source"]),
                "target": names.get(line["target"], line["target"])}

    def column_lineage(self, limit: int | None = None) -> dict[str, Any]:
        """The selected member's full lineage (#1426, #1429), for the AI: the map's
        governed read.

        ``object_map.column_lineage``: the focused-lineage read a double-click draws
        (code.f_object_map_column_focus), with NO depth -- the full reach
        code.f_db_reachable answers; the map's three steps are how it is drawn, not
        how far impact goes. For a selected column or transform: every column it
        derives from or feeds, every transform between with where it sits, its role
        and full expression, and each derivation step with its evidence. For a
        selected line: its relationship, ends, key columns and evidence as the map
        holds them, and the lineage of each end that is a column. ``limit`` (the
        calling tool's governed limit) bounds the items answered, nearest first;
        the totals, the deepest step and whether it was truncated are always said.
        Read only.

        Raises:
            AppError: When no column, transform or line of this map is selected, or
                the read refuses.
        """
        selected = str(self._selected or "")
        if selected.startswith("db:") and (selected in self._member_identities
                                           or selected in self._lineage_ids):
            return self._member_lineage(selected, limit)
        screen = self.projection() if selected else {"nodes": [], "edges": []}
        line = next((edge for edge in screen["edges"] if edge["id"] == selected), None)
        if line is None:
            raise AppError("The map has no selected column, transform or line to read the "
                           "lineage of.")
        names = {node["id"]: node.get("label") or node["id"] for node in screen["nodes"]}
        ends = [one for one in (line.get("from", line["source"]), line.get("to", line["target"]))
                if one in self._member_identities]
        return {"line": {
                    "id": line["id"], "relationship": line["kind"],
                    "reference_kind": line.get("reference_kind"),
                    "source": names.get(line["source"], line["source"]),
                    "target": names.get(line["target"], line["target"]),
                    "constraint": line.get("constraint"),
                    "columns": line.get("columns"),
                    "referenced_columns": line.get("referenced_columns"),
                    "through": line.get("via"),
                    "evidence": line.get("evidence") or []},
                "ends": [self._member_lineage(one, limit) for one in ends]}

    def _member_lineage(self, member: str, limit: int | None) -> dict[str, Any]:
        """One column's or transform's full lineage, bounded by ``limit`` (#1429)."""
        read = self._read_column_focus(member, None, _LINEAGE_MEMBERS)
        reach = ("depth", "upstream", "downstream")
        nodes = sorted(read["nodes"], key=lambda one: (one["depth"], one["schema"],
                                                        one["object"], one["ordinal"] or 0))
        columns = [{"id": one["id"], "schema": one["schema"], "object": one["object"],
                    "object_type": one["object_type"], "column": one["name"],
                    "data_type": one["data_type"], **{key: one[key] for key in reach}}
                   for one in nodes if one["member_kind"] == "column"]
        transforms = [{"id": one["id"],
                       "label": _transform_label(one["role"], one["expression"], one["name"]),
                       "role": one["role"], "expression": one["expression"],
                       "schema": one["schema"], "object": one["object"],
                       "object_type": one["object_type"], **{key: one[key] for key in reach}}
                      for one in nodes if one["member_kind"] != "column"]
        steps = [{"source": edge["source"], "target": edge["target"],
                  "kind": edge["kind"], "evidence": edge.get("evidence") or []}
                 for edge in read.get("edges") or []]
        start = next(one for one in read["nodes"] if one["id"] == member)

        def bounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return items if limit is None else items[:limit]

        # A column keeps the identity it is answered by (#1426); any other member
        # says what it is.
        named = ({"column": {"id": member, **self._member_identities[member]}}
                 if member in self._member_identities else
                 {"member": {"id": member, "member_kind": start["member_kind"],
                             "schema": start["schema"], "object": start["object"],
                             "object_type": start["object_type"], "name": start["name"]}})
        return {**named, "depth": _COLUMN_FOCUS_DEPTH,
                "max_depth": max((one["depth"] for one in nodes), default=0),
                "totals": {"columns": len(columns), "transforms": len(transforms),
                           "steps": len(steps)},
                "limit": limit,
                "truncated": limit is not None and any(
                    len(items) > limit for items in (columns, transforms, steps)),
                "columns": bounded(columns), "transforms": bounded(transforms),
                "steps": bounded(steps)}

    def _selection_in(self, ids: set[str]) -> str | None:
        """The selection, where it is one of ``ids`` -- the scope it belongs to -- else None."""
        return self._selected if self._selected in ids else None

    def _marked(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str | None:
        """Mark the selected node and its connections on this screen; the selection shown.

        ``highlight`` on every node and edge: ``focus`` the selected node,
        ``first`` it and what it links to directly (and the links among those),
        ``second`` what those link to next -- empty when nothing on this screen
        is selected.
        """
        selected = self._selection_in({node["id"] for node in nodes} | {edge["id"] for edge in edges})
        chosen_edge = next((edge for edge in edges if edge["id"] == selected), None)
        if chosen_edge is not None:
            endpoints = {chosen_edge["source"], chosen_edge["target"]}
            for node in nodes:
                node["highlight"] = ["first"] if node["id"] in endpoints else []
            for edge in edges:
                edge["highlight"] = ["focus", "first"] if edge["id"] == selected else []
            return selected
        # An object panel's genealogy is between objects (#1426): a line ending at
        # one of a panel's rows ends at that panel, however far it is expanded.
        panels = {node["id"] for node in nodes if node.get("expandable")}
        owner = {node["id"]: node["parent"] for node in nodes
                 if node.get("parent") in panels}

        def ends_of(edge: dict[str, Any]) -> set[str]:
            return {owner.get(edge["source"], edge["source"]),
                    owner.get(edge["target"], edge["target"])}

        near: dict[str, set[str]] = {}
        for edge in edges:
            source, target = (owner.get(edge["source"], edge["source"]),
                              owner.get(edge["target"], edge["target"]))
            near.setdefault(source, set()).add(target)
            near.setdefault(target, set()).add(source)
        # A transform is between the objects it joins, never a generation of its
        # own: the objects beyond it are as near as it is, and it is marked with them.
        between = {node["id"] for node in nodes if node.get("kind") == "transform"}

        def reached(start: str) -> tuple[set[str], set[str]]:
            found: set[str] = set()
            passed: set[str] = set()
            pending = list(near.get(start, set()))
            while pending:
                one = pending.pop()
                if one == start or one in found or one in passed:
                    continue
                if one in between:
                    passed.add(one)
                    pending.extend(near.get(one, set()))
                else:
                    found.add(one)
            return found, passed

        first: set[str] = set()
        second: set[str] = set()
        if selected:
            objects, passed = reached(selected)
            first = objects | passed
            for one in objects:
                further, through = reached(one)
                second |= further | through
            second -= first | {selected}
        for node in nodes:
            node["highlight"] = (["focus", "first"] if node["id"] == selected else
                                 ["first"] if node["id"] in first else
                                 ["second"] if node["id"] in second else [])
        for edge in edges:
            ends = ends_of(edge)
            edge["highlight"] = (["first"] if selected in ends or ends <= first else
                                 ["second"] if ends & first and ends & second else [])
        return selected

    # -- the object projection -------------------------------------------------

    def _claim(self, claim_id: Any) -> dict[str, Any] | None:
        if claim_id is None:
            return None
        return next((c for c in self._read.get("claims", [])
                     if c.get("canonical_object_id") == int(claim_id)), None)

    def _claim_keyed(self, key: str) -> dict[str, Any] | None:
        """The claim of this map's inventory a ``claim:<id>`` key names, or None."""
        ident = key[len(_CLAIM):] if key.startswith(_CLAIM) else ""
        return self._claim(ident) if ident.isdigit() else None

    def _holds_object_node(self, node_id: str) -> bool:
        """Whether this map's inventory holds the area or claim a node names."""
        if node_id.startswith(_AREA):
            ident = node_id[len(_AREA):]
            return ident.isdigit() and self._area(ident) is not None
        return self._claim_keyed(node_id) is not None

    def _area(self, area_id: Any) -> dict[str, Any] | None:
        if area_id is None:
            return None
        return next((a for a in self._read.get("areas", [])
                     if a.get("inferred_architecture_area_id") == int(area_id)), None)

    def _object_drill(self, node_id: str) -> None:
        if node_id.startswith(_AREA):
            self.focus(level="area", area_id=int(node_id[len(_AREA):]))
            return
        # Into the claim; the object screen remembers the area it was reached
        # from. Drilling into the focused claim keeps it in focus.
        self.focus(level="object", claim_id=int(node_id[len(_CLAIM):]),
                   area_id=self._focus.get("area_id"))

    # -- the semantic scope: map identities, shared by inspect and the screens ---

    def _scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """The focused scope's elements and relations, each with the source row it maps.

        The map structure alone -- id, kind, label, the identity it maps, its role
        relative to the focus. The row is kept beside it, never inside it, so a
        screen can add what it draws from the row and ``inspect`` can leave it out.
        """
        if self.projection_kind == SCHEMA_PROJECTION:
            return self._schema_scope()
        if self.projection_kind == FLOW_PROJECTION:
            return self._flow_scope()
        level = self._focus.get("level")
        if level == "area":
            return self._area_scope(self._focus["area_id"])
        if level == "object":
            return self._object_scope(self._focus["claim_id"])
        return self._estate_scope()

    def _object_projection(self) -> dict[str, Any]:
        """The focused screen: the scope, with what each element and relation draws."""
        level = self._focus.get("level")
        elements, relations = self._scope()
        if level in ("area", "object"):
            nodes = [element if row is None or element["kind"] != "claim" else
                     {**element, "outcome": row.get("evaluation_outcome") or "unevaluated"}
                     for element, row in elements]
            edges = [{**relation, "direct": bool(row.get("is_direct"))}
                     for relation, row in relations]
        else:
            nodes = [{**element, "claim_count": row["claim_count"],
                      "internal_relationship_count": row["internal_relationship_count"],
                      "crossing_relationship_count": row["crossing_relationship_count"]}
                     for element, row in elements]
            edges = [{**relation, "weight": row["relationship_count"],
                      "direct": bool(row["direct_relationship_count"])}
                     for relation, row in relations]
        return {"nodes": nodes, "edges": edges, "path": self._path(),
                "details": self._details(), "legend": self._legend()}

    def _legend(self) -> list[dict[str, str]]:
        """The shape/color key carried with this map's projection, per reading."""
        if self.projection_kind == FLOW_PROJECTION:
            if self._focus.get("flow") and not self._focus.get("detail"):
                return [
                    {"label": "Start / return", "shape": "ellipse", "color": "var(--good)"},
                    {"label": "Activity / subprocess", "shape": "round-rectangle", "color": "#7a8794"},
                    {"label": "Decision / loop", "shape": "diamond", "color": "#c79a2b"},
                    {"label": "Static primary route", "shape": "line", "color": "var(--accent)"},
                    {"label": "Alternative route", "shape": "line", "color": "var(--muted)"},
                    {"label": "Failure / refusal", "shape": "line", "color": "var(--bad)"},
                ]
            return [
                {"label": "Application / entry point", "shape": "ellipse", "color": "#4a6fa5"},
                {"label": "Entry symbol", "shape": "round-rectangle", "color": "#3f9b5f"},
                {"label": "Statement / call" if self._focus.get("detail") else "Operation / object interaction",
                 "shape": "round-rectangle", "color": "#7a8794"},
                {"label": "Indexing boundary", "shape": "round-rectangle", "color": "#c79a2b"},
                {"label": "Control transition" if self._focus.get("detail") else "Indexed normal path",
                 "shape": "line", "color": "var(--muted)"},
                {"label": "Partial indexed path", "shape": "line", "color": "#c79a2b"},
                {"label": "Selected / adjacent steps", "shape": "line",
                 "color": "var(--selected-edge, var(--accent))"},
            ]
        return [
            {"label": "Architecture area", "shape": "ellipse", "color": "#4a6fa5"},
            {"label": "Object — unevaluated", "shape": "round-rectangle", "color": "#7a8794"},
            {"label": "Object — supported", "shape": "round-rectangle", "color": "#3f9b5f"},
            {"label": "Object — unprovable", "shape": "round-rectangle", "color": "#c79a2b"},
            {"label": "Object — contradicted", "shape": "round-rectangle", "color": "#c0453b"},
            {"label": "Selected / first-order links", "shape": "line", "color": "var(--selected-edge, var(--accent))"},
            {"label": "Second-order links", "shape": "line", "color": "var(--progress-fill, var(--accent))"},
        ]

    def _estate_scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """Every area, and every area-to-area link -- each beside the row it maps."""
        elements: list[_Mapped] = [({
            "id": f"{_AREA}{area['inferred_architecture_area_id']}", "kind": "area",
            "area_id": area["inferred_architecture_area_id"], "label": area["area_label"],
            "role": "" if area["claim_count"] else "empty",
        }, area) for area in self._read.get("areas", [])]
        relations: list[_Mapped] = [({
            "id": f"link:{link['from_area_id']}:{link['to_area_id']}", "kind": "link",
            "source": f"{_AREA}{link['from_area_id']}", "target": f"{_AREA}{link['to_area_id']}",
        }, link) for link in self._read.get("area_links", [])]
        return elements, relations

    @staticmethod
    def _claim_element(claim: dict[str, Any], role: str = "") -> _Mapped:
        return ({
            "id": f"{_CLAIM}{claim['canonical_object_id']}", "kind": "claim",
            "claim_id": claim["canonical_object_id"],
            "label": claim.get("owner_qualified_name") or claim.get("object_key"),
            "role": role,
        }, claim)

    @staticmethod
    def _relationship(row: dict[str, Any], relation_id: str, source: str,
                      target: str) -> _Mapped:
        return ({"id": relation_id, "kind": "relationship", "source": source,
                 "target": target}, row)

    def _area_scope(self, area_id: int) -> tuple[list[_Mapped], list[_Mapped]]:
        """One area's claims and their links.

        A link with both claims in the area joins them. A link leaving the area
        is drawn to each area the other claim is placed in, as that area's node;
        a claim placed nowhere is drawn as itself.
        """
        claims = {c["canonical_object_id"]: c for c in self._read.get("claims", [])}
        areas_of: dict[int, set[int]] = {}
        for placement in self._read.get("placements", []):
            areas_of.setdefault(placement["canonical_object_id"], set()).add(
                placement["inferred_architecture_area_id"])
        labels = {a["inferred_architecture_area_id"]: a["area_label"]
                  for a in self._read.get("areas", [])}
        inside = {claim_id for claim_id, areas in areas_of.items() if area_id in areas}

        elements: dict[str, _Mapped] = {}
        for claim_id in sorted(inside):
            if claim_id in claims:
                elements[f"{_CLAIM}{claim_id}"] = self._claim_element(claims[claim_id])

        def outside(claim_id: int) -> list[str]:
            elsewhere = sorted(areas_of.get(claim_id, set()))
            if not elsewhere:
                if claim_id in claims:
                    elements[f"{_CLAIM}{claim_id}"] = self._claim_element(
                        claims[claim_id], "outside")
                return [f"{_CLAIM}{claim_id}"]
            for other in elsewhere:
                elements[f"{_AREA}{other}"] = ({"id": f"{_AREA}{other}", "kind": "area",
                                                "area_id": other, "label": labels.get(other, ""),
                                                "role": "elsewhere"}, None)
            return [f"{_AREA}{other}" for other in elsewhere]

        relations: list[_Mapped] = []
        for row in self._read.get("relationships", []):
            source_id, target_id = row["canonical_object_id"], row["linked_canonical_object_id"]
            relation_id = f"relationship:{source_id}:{target_id}"
            if source_id in inside and target_id in inside:
                relations.append(self._relationship(
                    row, relation_id, f"{_CLAIM}{source_id}", f"{_CLAIM}{target_id}"))
            elif source_id in inside:
                for target in outside(target_id):
                    relations.append(self._relationship(
                        row, f"{relation_id}:{target}", f"{_CLAIM}{source_id}", target))
            elif target_id in inside:
                for source in outside(source_id):
                    relations.append(self._relationship(
                        row, f"{relation_id}:{source}", source, f"{_CLAIM}{target_id}"))
        return list(elements.values()), relations

    def _object_scope(self, claim_id: int) -> tuple[list[_Mapped], list[_Mapped]]:
        """One claim and the claims it is directly linked with, in both directions."""
        claims = {c["canonical_object_id"]: c for c in self._read.get("claims", [])}
        elements: dict[str, _Mapped] = {}
        if claim_id in claims:
            elements[f"{_CLAIM}{claim_id}"] = self._claim_element(claims[claim_id], "focus")
        relations: list[_Mapped] = []
        for row in self._read.get("relationships", []):
            ends = (row["canonical_object_id"], row["linked_canonical_object_id"])
            if claim_id not in ends:
                continue
            for end in ends:
                if end != claim_id and end in claims:
                    elements[f"{_CLAIM}{end}"] = self._claim_element(claims[end])
            relations.append(self._relationship(
                row, f"relationship:{ends[0]}:{ends[1]}", f"{_CLAIM}{ends[0]}",
                f"{_CLAIM}{ends[1]}"))
        return list(elements.values()), relations

    def _path(self) -> list[dict[str, Any]]:
        """The path to the focused screen, Estate first; each step names its focus.

        Each map retains the traversed objects after its root.
        """
        if self.projection_kind == FLOW_PROJECTION:
            if self.subject.get("kind") == "application":
                return list(self._context_path)
            return [{"value": "flows", "label": "Applications", "focus": {"flow": None}},
                    *self._context_path]
        if self.projection_kind == SCHEMA_PROJECTION:
            steps = [{"value": "schema", "label": self.subject["schema"],
                      "focus": {"opened": list(self._focus.get("opened") or [])}}]
            steps.extend({**step, "focus": {**step["focus"],
                          "opened": list(self._focus.get("opened") or [])}}
                         for step in self._context_path)
            return steps
        return [{"value": "estate", "label": "Estate", "focus": {"level": "estate"}},
                *self._context_path]

    def _details(self) -> dict[str, Any] | None:
        """The focused claim's details, as its own row states them."""
        if self.projection_kind == FLOW_PROJECTION:
            return self._flow_details()
        edge = self._mapped_edge(self._selected or "")
        if edge is not None:
            element, row = edge
            names = {n["id"]: n.get("label", n["id"]) for n, _ in self._scope()[0]}
            details = [["Relationship", element["kind"]],
                       ["Source", (row or {}).get("object_key") or names.get(element["source"])],
                       ["Target", (row or {}).get("linked_object_key") or names.get(element["target"])]]
            inspection = self._edge_inspection
            if inspection is None or inspection["id"] != element["id"]:
                return {"rows": details, "hint": "Double-click for indexed call/reference evidence."}
            evidence = inspection["evidence"]
            if not evidence:
                details.append(["Evidence", "No indexed call/reference locations are available."])
            for index, site in enumerate(evidence, 1):
                location = f"{site['repository_key']}/{site['relative_path']}"
                if site.get("source_line", 0) > 0:
                    location += f":{site['source_line']}"
                details.append([f"Evidence {index}",
                                f"{site['edge_kind']}: {site['source_identity']} → {site['target_identity']}\n{location}"])
            return {"rows": details}
        if self._focus.get("level") != "object":
            return None
        claim = self._claim(self._focus["claim_id"])
        if claim is None:
            return None
        return {"rows": [
            ["Object", claim.get("object_key")],
            ["Responsibility", claim.get("responsibility")],
            ["Evaluation", claim.get("evaluation_outcome")],
            ["Why", claim.get("evaluation_reason")],
            ["Owner file", claim.get("owner_subject_id")],
            ["Kind", claim.get("owner_kind")],
            ["Public seam", claim.get("public_seam")],
            ["Must not bypass", claim.get("must_not_bypass")],
            ["Consumer files", claim.get("consumer_files")],
            ["Dependencies", claim.get("dependencies")],
            ["Unresolved edges", claim.get("unresolved_edges")],
        ]}

    # -- the flow projection (#1304) --------------------------------------------

    def _flow(self, key: str) -> dict[str, Any] | None:
        """The discovered application entry ``key`` names, or None."""
        return next((f for f in self._read.get("flows", []) if f.get("flow_key") == key), None)

    def _holds_flow_node(self, node_id: str) -> bool:
        """Whether the flows screen or the focused flow's chart holds the node."""
        return any(element["id"] == node_id for element, _ in self._flow_scope()[0])

    def _flow_drill(self, node_id: str) -> None:
        """Declarations use proved guards; calls retain their exact continuation."""
        element = next((n for n, _ in self._flow_scope()[0] if n["id"] == node_id), {})
        if node_id.startswith(_APPLICATION):
            self.focus(application=node_id[len(_APPLICATION):])
        elif element.get("declaration_kind"):
            self.focus(flow=element["flow_key"],
                       **{f"{element['declaration_kind']}_id": element["declaration_id"]})
        elif node_id.startswith(_FLOW):
            self.focus(flow=node_id[len(_FLOW):])
        else:
            context = {k: v for k, v in self._focus.items()
                       if k in ("flow", "command_id", "operation_id", "request_id", "detail")}
            if element.get("expand_symbol_id") is not None:
                # Detail of an input's root keeps its input constraint. Passing
                # that dispatcher as a callee would reopen the entire app.
                input_root = (self._focus.get("symbol") is None and
                              any(k in self._focus for k in ("command_id", "request_id")))
                self.focus(**{k: v for k, v in context.items() if k != "detail"},
                           **({} if input_root else {"symbol": element["expand_symbol_id"]}), detail=True,
                           **({"call_site": self._focus["call_site"]} if self._focus.get("call_site") else {}))
            elif element.get("target_symbol_id") is not None:
                self.focus(**context, symbol=element["target_symbol_id"],
                           call_site=self._flow_call_site(node_id))
            elif element.get("kind") in ("call", "activity_boundary"):
                raise AppError("The index does not resolve this call's destination.")

    def _flow_call_site(self, node_id: str) -> dict[str, Any] | None:
        """Exact indexed caller/call/continuations, as transient navigation context."""
        nodes, edges = self._flow_scope_raw()
        row = next((row for node, row in nodes if node["id"] == node_id), None)
        if not row or row.get("kind") != "call":
            return None
        return {"caller_symbol_id": row["code_symbol_id"], "node_key": row["node_key"],
                "code_edge_id": row.get("code_edge_id"), "repository_key": row["repository_key"],
                "relative_path": row["relative_path"], "source_line": row.get("source_line"),
                "source_column": row.get("source_column"),
                "continuations": [{"node_key": evidence["to_key"], "kind": evidence["transition_kind"]}
                                  for edge, evidence in edges if edge["source"] == node_id]}

    def _flow_opening(self, node_id: str) -> dict[str, Any] | None:
        """What a right-click on a flow-chart node opens: a symbol's indexed source
        file, as its canonical identity; a flow opens nothing; a boundary has no file.

        Raises:
            AppError: When the node is absent or an indexing boundary.
        """
        pair = next(((n, row) for n, row in self._flow_scope()[0] if n["id"] == node_id), None)
        if pair is None:
            raise AppError(f"The map has no node {node_id!r}.")
        element, evidence = pair
        evidence = evidence or {}
        target = bool(evidence.get("target_relative_path") and evidence.get("target_start_line"))
        repository = (evidence.get("target_source_repository") if target else None) or element.get("repository_key") or evidence.get("repository_key")
        path = evidence.get("target_relative_path") if target else element.get("relative_path") or evidence.get("relative_path")
        line = evidence.get("target_start_line") if target else evidence.get("source_line")
        if path and repository:
            return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                                 "relative_path": f"{repository}/{path}"},
                    **({"focus": {"line": int(line)}} if line and int(line) > 0 else {})}
        if element["kind"] == "boundary":
            raise AppError("This boundary is where the index stops: it names no source file.")
        return None

    def _flow_scope_raw(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """Declarations or indexed control-flow nodes/transitions beside their evidence."""
        key = self._focus.get("flow")
        if not key:
            flows = self._read.get("flows", [])
            application = self._focus.get("application")
            if not application:
                applications = sorted({f["entry_repository_key"] for f in flows})
                return [({"id": f"{_APPLICATION}{name}", "kind": "flow", "label": name,
                          "role": ""}, {"application": name}) for name in applications], []
            if not any(f["entry_repository_key"] == application for f in flows):
                return [({"id": f"boundary:inputs:{application}", "kind": "boundary",
                          "label": "This application is not present in the code index", "role": "boundary"},
                         {"resolution": "application_not_indexed"})], []
            declarations: dict[str, _Mapped] = {}
            for kind, read, id_key, name_key in (
                    ("command", "flow_commands", "application_command_id", "command_name"),
                    ("request", "flow_requests", "application_request_id", "request_name"),
                    ("operation", "flow_operations", "application_operation_id", "operation_name")):
                for row in self._read.get(read, []):
                    if row["application_name"] != application:
                        continue
                    identity = f"{kind}:{row[id_key]}"
                    existing = declarations.get(identity)
                    resolved = bool(row.get("code_symbol_id")) or bool(existing and not existing[0]["role"])
                    declarations[identity] = ({"id": identity, "kind": "flow", "label": f"{dict(command='CLI', request='Request', operation='Operation')[kind]}: {row[name_key]}",
                        "role": "" if resolved else "unresolved", "declaration_kind": kind,
                        "declaration_id": row[id_key], "flow_key": next(
                            f["flow_key"] for f in flows if f["entry_repository_key"] == application)}, row)
            if declarations:
                return list(declarations.values()), []
            return [({"id": f"boundary:inputs:{application}", "kind": "boundary",
                      "label": "No indexed CLI commands or requests", "role": "boundary"},
                     {"resolution": "input_bindings_not_published"})], []
        operation_symbol = None
        if self._focus.get("operation_id") is not None and self._focus.get("symbol") is None:
            implementations = {r.get("code_symbol_id") for r in self._read.get("flow_operations", [])
                               if r["application_operation_id"] == self._focus["operation_id"]
                               and r.get("code_symbol_id") is not None}
            if len(implementations) == 1:
                operation_symbol = next(iter(implementations))
            else:
                return [({"id": f"boundary:operation:{self._focus['operation_id']}", "kind": "boundary",
                          "label": "No indexed declaration-to-implementation binding", "role": "boundary"},
                         {"resolution": "dynamic_operation_binding_unproved"})], []
        if (self._focus.get("command_id") is not None or self._focus.get("request_id") is not None) and self._focus.get("symbol") is None:
            return self._command_flow_scope()
        flow = self._flow(str(key)) or {}
        symbol_id = self._focus.get("symbol", operation_symbol or flow.get("entry_code_symbol_id"))
        if symbol_id is None:
            return [({"id": f"{_BOUNDARY}{key}:entry", "kind": "boundary",
                      "label": flow.get("entry_identity") or key, "role": "boundary"},
                     {"resolution": "declared_entry_not_indexed"})], []
        nodes: dict[str, _Mapped] = {}
        edges: dict[str, _Mapped] = {}
        for row in self._read.get("flow_steps", []):
            if row.get("code_symbol_id") != symbol_id:
                continue
            if row.get("node_key") is None:
                identity = f"{_BOUNDARY}{key}:coverage:{symbol_id}"
                nodes[identity] = ({"id": identity, "kind": "boundary",
                    "label": "Control-flow evidence is not published for this symbol",
                    "role": "boundary", "repository_key": row["repository_key"],
                    "relative_path": row["relative_path"]}, row)
                continue
            identity = f"control:{symbol_id}:{row['node_key']}"
            nodes[identity] = (self._flow_node(row), row)
            if row.get("to_key"):
                target = f"control:{symbol_id}:{row['to_key']}"
                transition = row["transition_kind"]
                edge_id = f"transition:{identity}:{transition}:{target}"
                edges[edge_id] = ({"id": edge_id, "kind": "transition", "label": transition,
                                   "source": identity, "target": target}, row)
        if not nodes:
            return [({"id": f"{_BOUNDARY}{key}:coverage:{symbol_id}", "kind": "boundary",
                      "label": "Control-flow evidence is not published for this symbol",
                      "role": "boundary"}, {"resolution": "control_flow_not_published"})], []
        return list(nodes.values()), list(edges.values())

    def _command_flow_scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """Each proved guard's true branch, never a guessed command-handler call."""
        request = self._focus.get("request_id") is not None
        command_id = self._focus["request_id" if request else "command_id"]
        bindings = [r for r in self._read.get("flow_requests" if request else "flow_commands", [])
                    if r["application_request_id" if request else "application_command_id"] == command_id]
        nodes: dict[str, _Mapped] = {}
        edges: dict[str, _Mapped] = {}
        rows = self._read.get("flow_steps", [])
        for binding in bindings:
            symbol_id = binding.get("code_symbol_id")
            if symbol_id is None:
                continue
            scope = [r for r in rows if r.get("code_symbol_id") == symbol_id]
            guard = binding["node_key"]
            entry_branch = binding.get("request_branch", "true") if request else "true"
            outgoing: dict[str, list[dict[str, Any]]] = {}
            for row in scope:
                if row.get("to_key"):
                    outgoing.setdefault(row["node_key"], []).append(row)
            root = next((r for r in scope if r.get("node_key") == guard), {})
            # A channel request ends at the next receive, rather than including
            # every other request on the following dispatch iteration.
            receive_loops = {r["node_key"] for r in scope if request and r.get("kind") == "loop"
                             and r.get("source_line", 0) <= root.get("source_line", -1)
                             and r.get("end_line", 0) >= root.get("end_line", -1)}
            def continuations(node_key):
                candidates = outgoing.get(node_key, [])
                selected = binding["request_name"] if request else None
                values = next((r.get("commands") for r in candidates if r.get("commands")), [])
                if request and any(v.startswith(("request:", "request-false:")) for v in values):
                    negative = any(v.startswith("request-false:") for v in values)
                    matched = ("request-false:" if negative else "request:") + selected in values
                    branch = "false" if matched == negative else "true"
                    return [r["to_key"] for r in candidates if r["transition_kind"] == branch]
                return [r["to_key"] for r in candidates]
            pending = [r["to_key"] for r in outgoing.get(guard, [])
                       if r["transition_kind"] == entry_branch or root.get("kind") == "entry"]
            reached = {guard}
            while pending:
                current = pending.pop()
                if current in reached or current in receive_loops:
                    continue
                reached.add(current)
                pending.extend(continuations(current))
            for row in scope:
                node_key = row.get("node_key")
                if node_key not in reached:
                    continue
                identity = f"control:{symbol_id}:{node_key}"
                returns = [r for r in outgoing.get(node_key, [])
                           if r["to_key"] in receive_loops and r["to_key"] in continuations(node_key)
                           and (node_key != guard or r["transition_kind"] == entry_branch)]
                nodes[identity] = ({**self._flow_node(row),
                                    **({"operation_root": True} if node_key == guard else {})},
                                   {**row, **({"request_return_evidence": returns} if returns else {})})
                if row.get("to_key") in reached and row["to_key"] in continuations(node_key) and (node_key != guard or row["transition_kind"] == entry_branch or root.get("kind") == "entry"):
                    target = f"control:{symbol_id}:{row['to_key']}"
                    edge_id = f"transition:{identity}:{row['transition_kind']}:{target}"
                    edges[edge_id] = ({"id": edge_id, "kind": "transition", "label": row["transition_kind"],
                                      "source": identity, "target": target}, row)
        if not nodes:
            return [({"id": f"boundary:command:{command_id}", "kind": "boundary", "role": "boundary",
                      "label": "No indexed declaration-to-implementation binding"},
                     {"resolution": "command_binding_unproved"})], []
        return list(nodes.values()), list(edges.values())

    @staticmethod
    def _flow_node(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": f"control:{row['code_symbol_id']}:{row['node_key']}", "kind": row["kind"],
                "label": str(row.get("text") or row["kind"]),
                "role": "entry" if row["kind"] == "entry" else
                        "unresolved" if row["kind"] == "call" and row.get("target_code_symbol_id") is None else "",
                "repository_key": row["repository_key"], "relative_path": row["relative_path"],
                "target_symbol_id": row.get("target_code_symbol_id"),
                "owner_keys": row.get("target_owner_keys") or row.get("source_owner_keys") or [],
                "target_repository_key": row.get("target_repository_key")}

    def _flow_scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        nodes, edges = self._flow_scope_raw()
        if not self._focus.get("flow") or self._focus.get("detail"):
            return nodes, edges
        bindings = [r for r in self._read.get("flow_commands", [])
                    if r["application_command_id"] == self._focus.get("command_id")]
        bindings += [r for r in self._read.get("flow_operations", [])
                     if r["application_operation_id"] == self._focus.get("operation_id")]
        bindings += [r for r in self._read.get("flow_requests", [])
                     if r["application_request_id"] == self._focus.get("request_id")]
        title = bindings[0].get("description") if bindings and self._focus.get("symbol") is None else None
        return self._flow_activities(nodes, edges, title=title,
            completion_label="Await next request" if self._focus.get("request_id") is not None else None)

    @staticmethod
    def _flow_activities(nodes: list[_Mapped], edges: list[_Mapped], *,
                         title: str | None = None, completion_label: str | None = None) -> tuple[list[_Mapped], list[_Mapped]]:
        """Object interactions and subprocesses connected by indexed normal paths.

        Object construction, decisions and explicit refusals stay visible.
        Predicate/argument calls and implementation statements remain arrow
        witnesses, not invented activities. Incidental exception fanout and the
        full source CFG stay accessible through the detail node. This does not infer call order
        from lines, classify domain objects by spelling or execute scanned code.
        """
        if not any(row and row.get("code_symbol_id") for _, row in nodes):
            return nodes, edges
        by_id = {node["id"]: (node, row) for node, row in nodes}
        scopes = sorted({row["code_symbol_id"] for _, row in nodes if row and row.get("code_symbol_id")})
        projected_nodes: list[_Mapped] = []
        projected_edges: list[_Mapped] = []
        for symbol_id in scopes:
            scope = {key: pair for key, pair in by_id.items()
                     if pair[1] and pair[1].get("code_symbol_id") == symbol_id}
            if not any(node["kind"] == "entry" or node.get("operation_root")
                       for node, _ in scope.values()):
                projected_nodes.extend(scope.values())
                continue
            if not all("parameter_names" in row for _, row in scope.values()):
                projected_nodes.append(({"id": f"operation-metadata:{symbol_id}", "kind": "activity_boundary",
                    "label": "Operation declaration metadata is not published", "role": "boundary",
                    "expand_symbol_id": symbol_id}, {"resolution": "operation_metadata_not_published"}))
                continue
            scoped_edges = list(edges)
            if completion_label:
                # The request scope deliberately cuts the edge back to receive.
                # Show that witnessed boundary as an end event, not a function exit.
                cuts = [(key, witness) for key, (_, row) in scope.items()
                        for witness in row.get("request_return_evidence", [])]
                if cuts:
                    completion = f"request-return:{symbol_id}"
                    scope[completion] = ({"id": completion, "kind": "exit", "label": completion_label},
                        {**cuts[0][1], "kind": "request_return", "text": completion_label})
                    for key, row in cuts:
                        scoped_edges.append(({"id": f"request-return:{key}", "kind": "transition",
                                              "label": "next", "source": key, "target": completion}, row))
            adjacency: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
            failure_edges = []
            for edge, evidence in scoped_edges:
                if edge["source"] not in scope or edge["target"] not in scope:
                    continue
                if edge["label"] == "exception":
                    failure_edges.append(evidence)
                adjacency.setdefault(edge["source"], []).append((edge, evidence))
            exits = {key for key, (node, _) in scope.items() if node["kind"] in ("exit", "exception_exit")}
            roots = [key for key, (node, _) in scope.items()
                     if node["kind"] == "entry" or node.get("operation_root")]
            reached = set(roots)
            pending = list(roots)
            while pending:
                for edge, _ in adjacency.get(pending.pop(), []):
                    if edge["target"] not in reached:
                        reached.add(edge["target"])
                        pending.append(edge["target"])
            # A handled request returns to the next channel receive, where its
            # scoped graph stops. It need not reach the serving function's exit.
            # Keep every reachable activity, including non-returning loops.
            successful = reached
            visible: dict[str, _Mapped] = {}
            conditions = [row for node, row in scope.values() if node["kind"] in ("condition", "loop")]
            calls = [row for node, row in scope.values() if node["kind"] == "call"]
            expressions = [row for node, row in scope.values() if node["kind"] == "boundary"]
            refusals = [row for node, row in scope.values() if node["kind"] == "raise"]

            def span(row):
                return ((row.get("source_line", 0), row.get("source_column", 0)),
                        (row.get("end_line", 0), row.get("end_column", 0)))

            for key, (node, row) in scope.items():
                if key not in successful:
                    continue
                if key in roots:
                    visible[key] = ({**node, "kind": "activity_entry", "label": title or row.get("text")
                                     or row.get("qualified_name"), "target_symbol_id": None}, row)
                elif key in exits:
                    visible[key] = ({**node, "kind": "activity_exit",
                                     "label": "Failure / refusal to caller" if node["kind"] == "exception_exit"
                                     else completion_label if row.get("kind") == "request_return" else "Return to caller"}, row)
                elif node["kind"] in ("condition", "loop", "raise", "context_enter", "cleanup"):
                    visible[key] = (dict(node), row)
                elif node["kind"] == "call":
                    start, end = span(row)
                    if any(span(parent)[0] <= start and end <= span(parent)[1]
                           for parent in conditions + refusals):
                        continue
                    if row.get("target_symbol_kind") != "class" and any(
                            span(parent)[0] <= start and end <= span(parent)[1]
                            and span(parent) != (start, end) for parent in calls):
                        continue
                    reference = str(row.get("callee_reference") or "")
                    receiver = reference.partition(".")[0] if "." in reference else None
                    on_input = receiver in row["parameter_names"]
                    target_kind = row.get("target_symbol_kind")
                    if not on_input and target_kind != "class" and any(
                            span(parent)[0] <= start and end <= span(parent)[1] for parent in expressions):
                        continue  # Expression mechanics remain in the indexed path witness.
                    if row.get("target_code_symbol_id") is not None:
                        if target_kind not in ("function", "method", "class"):
                            continue
                        name = row.get("target_symbol_name") or row.get("target_identity") or row["text"]
                        label = row.get("target_label") or name
                        if row.get("target_owner_name"):
                            method = f"{row['target_owner_name']}.{name}"
                            label = method
                        elif label == name or label == row.get("target_identity"):
                            label = str(name).replace("_", " ").strip()
                        if target_kind == "class":
                            label = f"Create {row.get('target_identity') or name}"
                        kind = "activity" if target_kind in ("function", "class") or on_input else "support_activity"
                        visible[key] = ({**node, "kind": kind, "label": label,
                                         **({"receiver": receiver} if on_input else {})}, row)
                    elif row.get("resolution") != "semantic_standard_library":
                        visible[key] = ({**node, "kind": "activity_boundary", "role": "boundary",
                                         "label": reference or "Unresolved application call",
                                         **({"receiver": receiver} if on_input else {})}, row)
            # Each arrow has an actual indexed witness through hidden nodes.
            # Do not enumerate all branch combinations or mix caller contexts.
            for source in visible:
                todo = [(edge["target"], [evidence]) for edge, evidence in adjacency.get(source, [])]
                visited = set()
                witnessed = set()
                while todo:
                    current, witness = todo.pop(0)
                    if current not in successful:
                        continue
                    branch = witness[0].get("transition_kind") if visible[source][0]["kind"] in ("condition", "loop") else None
                    route = (current, branch)
                    if current in visible:
                        if route not in witnessed:
                            identity = f"activity-path:{source}:{branch or 'next'}:{current}"
                            partial = any(row.get("kind") == "boundary" or row.get("transition_kind") == "unknown"
                                          for row in witness)
                            exceptional = any(row.get("transition_kind") == "exception" for row in witness)
                            label = branch if branch in ("true", "false", "iterate", "done") else "exception" if exceptional else ""
                            projected_edges.append(({"id": identity, "kind": "transition",
                                "label": label or ("partial" if partial else ""),
                                "source": source, "target": current}, {"transition_evidence": witness,
                                "evidence_scope": "One indexed normal-path witness; full alternatives are in function detail"}))
                            witnessed.add(route)
                        continue
                    if route in visited:
                        continue
                    visited.add(route)
                    todo.extend((edge["target"], witness + [evidence])
                                for edge, evidence in adjacency.get(current, []))
            # Native compound nodes group operations on the same formal input.
            # They assert no runtime identity or extra domain object.
            participants: dict[str, _Mapped] = {}
            for key, (node, evidence) in list(visible.items()):
                receiver = node.get("receiver")
                if not receiver:
                    continue
                group_id = f"participant:{symbol_id}:{receiver}"
                names = evidence["parameter_names"]
                annotations = evidence.get("parameter_annotations") or []
                position = names.index(receiver)
                annotation = annotations[position] if position < len(annotations) else ""
                participants.setdefault(group_id, ({"id": group_id, "kind": "participant",
                    "label": f"{receiver}: {annotation}" if annotation else receiver,
                    "repository_key": evidence.get("repository_key"),
                    "relative_path": evidence.get("relative_path")},
                    {**evidence, "receiver": receiver, "annotation": annotation,
                     "ordinal": position, "operations": []}))
                participants[group_id][1]["operations"].append({
                    "operation": evidence.get("target_identity"), "call": evidence.get("text"),
                    "source_line": evidence.get("source_line"), "code_edge_id": evidence.get("code_edge_id")})
                visible[key] = ({**node, "parent": group_id}, evidence)
            projected_nodes.extend(visible.values())
            projected_nodes.extend(sorted(participants.values(), key=lambda pair: pair[1]["ordinal"]))
            for index, (node, evidence) in enumerate(projected_nodes):
                if node["id"] not in roots:
                    continue
                projected_nodes[index] = ({**node, "expand_symbol_id": symbol_id},
                    {**evidence, "failure_edges": failure_edges, "implementation_nodes": len(scope),
                     "reading": "Operation flow; double-click the entry for detailed function control flow"})
        return projected_nodes, projected_edges

    @staticmethod
    def _process_reading(elements: list[_Mapped], relations: list[_Mapped]) -> dict[str, Any]:
        """Choose a source-witnessed backbone, never an observed/dominant variant.

        Condense loops before ranking paths so cycles cannot increase their own
        score. Prefer distinct domain responsibilities, then fewer steps; repeated
        accessor calls cannot make a branch more important. Alternatives remain in
        the projection and can be revealed without changing the layout.
        """
        nodes = {n["id"]: (n, row or {}) for n, row in elements if n["kind"] != "participant"}
        failure = {key for key, (n, row) in nodes.items()
                   if n["kind"] in ("raise", "exception_exit") or row.get("kind") == "exception_exit"}
        outgoing = {key: [] for key in nodes}
        incoming = {key: [] for key in nodes}
        exceptional = set()
        all_outgoing = {key: [] for key in nodes}
        for edge, row in relations:
            a, b = edge["source"], edge["target"]
            is_exception = edge.get("label") == "exception" or any(
                step.get("transition_kind") == "exception" for step in (row or {}).get("transition_evidence", []))
            if is_exception:
                exceptional.add(edge["id"])
            if a in nodes and b in nodes:
                all_outgoing[a].append(b)
                if not is_exception:
                    outgoing[a].append(b)
                    incoming[b].append(a)
        normal_reached = {key for key, (node, _) in nodes.items()
                          if node["kind"] in ("activity_entry", "entry") or node.get("operation_root")}
        pending = list(normal_reached)
        while pending:
            for target in outgoing[pending.pop()]:
                if target not in normal_reached:
                    normal_reached.add(target)
                    pending.append(target)
        exception_reached = {edge["target"] for edge, _ in relations if edge["id"] in exceptional}
        pending = list(exception_reached)
        while pending:
            for target in all_outgoing.get(pending.pop(), []):
                if target not in exception_reached:
                    exception_reached.add(target)
                    pending.append(target)
        failure |= exception_reached - normal_reached
        ends = {key for key, (n, _) in nodes.items()
                if key not in failure and (n["kind"] in ("activity_exit", "exit") or not outgoing[key])}
        success = set(ends)
        pending = list(ends)
        while pending:
            for key in incoming[pending.pop()]:
                if key not in success and key not in failure:
                    success.add(key)
                    pending.append(key)
        # A branch leading exclusively to refusal is a failure route as well.
        failure_reached = set(failure)
        pending = list(failure)
        while pending:
            for key in incoming[pending.pop()]:
                if key not in failure_reached:
                    failure_reached.add(key)
                    pending.append(key)
        failure |= failure_reached - success
        allowed = set(nodes) - failure
        seen, order = set(), []
        for root in sorted(allowed):
            stack = [(root, False)]
            while stack:
                key, finish = stack.pop()
                if finish:
                    order.append(key)
                elif key not in seen:
                    seen.add(key)
                    stack.append((key, True))
                    stack.extend((target, False) for target in outgoing[key]
                                 if target in allowed and target not in seen)
        components, membership = [], {}
        for root in reversed(order):
            if root in membership:
                continue
            number = len(components)
            component, stack = [], [root]
            membership[root] = number
            while stack:
                key = stack.pop()
                component.append(key)
                for before in incoming[key]:
                    if before in allowed and before not in membership:
                        membership[before] = number
                        stack.append(before)
            components.append(component)
        links = {i: set() for i in range(len(components))}
        indegree = {i: 0 for i in links}
        for key in allowed:
            for target in outgoing[key]:
                if target in allowed and membership[key] != membership[target]:
                    links[membership[key]].add(membership[target])
        for targets in links.values():
            for target in targets:
                indegree[target] += 1
        def responsibilities(component):
            owners = set()
            for key in component:
                node = nodes[key][0]
                if node.get("receiver"):
                    owners.add("input:" + node["receiver"])
                else:
                    owners.update(node.get("owner_keys") or [])
                    if not node.get("owner_keys") and node.get("target_symbol_id") is not None:
                        owners.add("callable:" + str(node["target_symbol_id"]))
            return frozenset(owners)
        roots = {membership[key] for key, (node, _) in nodes.items() if key in allowed
                 and (node["kind"] in ("activity_entry", "entry") or node.get("operation_root"))}
        if not roots:
            roots = {i for i in indegree if not indegree[i]}
        scores = {i: (responsibilities(components[i]), -len(components[i])) for i in roots}
        previous = {}
        pending = sorted(i for i in indegree if not indegree[i])
        while pending:
            at = pending.pop(0)
            for target in sorted(links[at]):
                if at in scores:
                    proposed = (scores[at][0] | responsibilities(components[target]),
                                scores[at][1] - len(components[target]))
                    rank = lambda score: (len(score[0]), score[1])
                    if target not in scores or rank(proposed) > rank(scores[target]):
                        scores[target], previous[target] = proposed, at
                indegree[target] -= 1
                if not indegree[target]:
                    pending.append(target)
        destinations = {membership[key] for key in ends if key in membership} & scores.keys()
        if not destinations:
            destinations = set(scores)
        chosen = []
        if destinations:
            at = max(sorted(destinations), key=lambda i: (len(scores[i][0]), scores[i][1]))
            while True:
                chosen.append(at)
                if at not in previous:
                    break
                at = previous[at]
            chosen.reverse()
        primary = {key for component in chosen for key in components[component]}
        pairs = set(zip(chosen, chosen[1:]))
        node_roles = {key: "failure" if key in failure else "primary" if key in primary else "alternative"
                      for key in nodes}
        for node, _ in elements:
            if node["kind"] == "participant":
                children = [node_roles[n["id"]] for n, _ in elements if n.get("parent") == node["id"]]
                node_roles[node["id"]] = ("primary" if "primary" in children else
                                          "failure" if children and all(r == "failure" for r in children) else "alternative")
        edge_roles = {}
        for edge, row in relations:
            a, b = edge["source"], edge["target"]
            if edge["id"] in exceptional or a in failure or b in failure:
                role = "failure"
            elif a in primary and b in primary and (membership[a] == membership[b] or
                                                      (membership[a], membership[b]) in pairs):
                role = "primary"
            else:
                role = "alternative"
            edge_roles[edge["id"]] = role
        return {"basis": "static", "nodes": node_roles, "edges": edge_roles,
                "order": [key for component in chosen for key in sorted(components[component])],
                "alternative_count": sum(role == "alternative" for role in edge_roles.values()),
                "failure_count": sum(role == "failure" for role in edge_roles.values()),
                "measurements": None}

    def _flow_projection(self) -> dict[str, Any]:
        """The flows screen, or the focused flow's chart, with what each draws."""
        elements, relations = self._flow_scope()
        nodes = [dict(element) for element, _ in elements]
        edges = [{**relation, "weight": 1}
                 for relation, row in relations]
        coverage = [row for _, row in elements if row and row.get("coverage_status")]
        partial = [row for row in coverage if row["coverage_status"] == "partial"]
        process = self._process_reading(elements, relations) if self._focus.get("flow") and not self._focus.get("detail") else None
        path = self._path()
        scope_key = lambda focus: json.dumps([self.subject, focus], sort_keys=True, separators=(",", ":"))
        current = scope_key(self._focus)
        ancestors = [{"key": scope_key(step["focus"]), "value": step["value"], "label": step["label"]}
                     for step in path if step["focus"].get("flow") and scope_key(step["focus"]) != current]
        return {"nodes": nodes, "edges": edges, "path": path,
                "process": process,
                "view": {"key": current, "label": path[-1]["label"] if path else "Process",
                         "ancestors": ancestors if self._focus.get("symbol") is not None or self._focus.get("detail") else []},
                "details": self._details(), "legend": self._legend(),
                "coverage_status": "partial" if partial else "complete" if coverage else None,
                "coverage_reason": "; ".join(sorted({str(row["coverage_reason"]) for row in partial
                                                       if row.get("coverage_reason")})) or None}

    def _flow_details(self) -> dict[str, Any] | None:
        """Source evidence for the selected step/transition; no behavioral inference."""
        elements, relations = self._flow_scope()
        selected = self._selection_in({e["id"] for e, _ in elements}
                                      | {r["id"] for r, _ in relations})
        if selected is None:
            site = self._focus.get("call_site")
            return {"rows": [["Caller", site["caller_symbol_id"]],
                             ["Call site", f"{site['repository_key']}/{site['relative_path']}:{site['source_line']}"],
                             ["Continuations", site["continuations"]]]} if site else None
        pair = next(((e, row) for e, row in elements if e["id"] == selected), None)
        if pair is None:
            relation, row = next((e, r) for e, r in relations if e["id"] == selected)
            names = {e["id"]: e.get("label") for e, _ in elements}
            return {"rows": [["Transition", relation["label"]],
                ["From", names.get(relation["source"])], ["To", names.get(relation["target"])],
                ["Indexed transitions", row.get("transition_evidence") or [row]],
                ["Evidence", "Static possible path, not a runtime trace"]]}
        element, row = pair
        row = row or {}
        if element["kind"] == "participant":
            return {"rows": [["Declared input", row["receiver"]], ["Declared type", row["annotation"]],
                ["Operations", row["operations"]],
                ["Evidence", "Grouped by the same formal input in this function; not a runtime identity claim"]]}
        if element["kind"] in ("activity", "support_activity"):
            return {"rows": [["Activity", element["label"]], ["Operation", row.get("target_identity")],
                ["Declared description", row.get("target_label")],
                ["Object evidence", element.get("owner_keys")], ["Caller", row.get("dotted_identity")],
                ["Call site", row.get("text")], ["File", row.get("relative_path")],
                ["Line", row.get("source_line")], ["Column", row.get("source_column")],
                ["Resolution", row.get("resolution")], ["Coverage", row.get("coverage_status")],
                ["Inspect", "Double-click to follow this operation; right-click to open its call site"]]}
        if element["kind"] == "activity_entry":
            return {"rows": [["Reading", row["reading"]], ["Implementation nodes", row["implementation_nodes"]],
                ["Indexed failure transitions", len(row["failure_edges"])],
                ["Inspect", "Double-click for the full indexed function control flow, including failures"]]}
        if element.get("declaration_kind"):
            return {"rows": [["Declaration", element["declaration_kind"]],
                ["Name", element["label"]], ["Description", row.get("description")],
                ["Binding", "Source-proved input handler" if not element["role"] else
                            "No indexed declaration-to-implementation binding"],
                ["Evidence", "Declaration order does not establish execution order"]]}
        if element["kind"] == "flow":
            return {"rows": [["Application / entry", element.get("label")],
                ["Entry", row.get("entry_identity")], ["Repository", row.get("entry_repository_key")]]}
        if element["kind"] == "boundary" and "coverage_status" not in row:
            return {"rows": [["Boundary", element["label"]], ["Resolution", row.get("resolution")]]}
        return {"rows": [["Code", row.get("text")], ["Kind", row.get("kind")],
            ["Function", row.get("dotted_identity")], ["Repository", row.get("repository_key")],
            ["File", row.get("relative_path")], ["Line", row.get("source_line")],
            ["Column", row.get("source_column")], ["End line", row.get("end_line")],
            ["End column", row.get("end_column")], ["Call target", row.get("target_identity")],
            ["Resolution", row.get("resolution")], ["Canonical owners", element.get("owner_keys")],
            ["Target repository", row.get("target_repository_key")], ["Coverage", row.get("coverage_status")],
            ["Coverage limitations", row.get("coverage_reason")],
            ["Evidence", "Static possible paths; exceptional destinations may be conservative"]]}

    # -- the schema projection -------------------------------------------------

    def _table_names(self) -> list[str]:
        return [table.identity.name for table in self._database.tables]

    def _holds_schema_node(self, node_id: str) -> bool:
        """Whether this schema holds the table, or references the external table, a node names."""
        if self._focus.get("column") and self._column_focus:
            return any(box["id"] == node_id for box in self._focus_boxes())
        if any(self._focus.get(key) for key in ("table", "node", "object")) and self._table_context:
            return any(node["id"] == node_id for node in self._table_context["nodes"])
        if node_id.startswith(_TABLE):
            return node_id[len(_TABLE):] in self._table_names()
        if node_id.startswith(_EXTERNAL):
            return any(f"{_EXTERNAL}{key.referenced_schema}.{key.referenced_table}" == node_id
                       for table in self._database.tables for key in table.foreign_keys)
        return False

    def _schema_drill(self, node_id: str) -> None:
        """Focus this object's indexed neighbourhood using the existing context read."""
        target = {"table": node_id[len(_TABLE):]} if node_id.startswith(_TABLE) else {"node": node_id}
        # Tables stay open by name; a node opened on the screen left behind does not
        # follow to the next one (#1426).
        names = set(self._table_names())
        self.focus(**target, opened=[one for one in self._focus.get("opened", []) if one in names])

    def _read_table_context(self, table: str | None = None, *, node: str | None = None,
                            schema: str | None = None, alias: str | None = None,
                            object_type: str = "table", signature: str = "") -> dict[str, Any]:
        """The existing index's bounded graph, on the host's shared index connection."""
        if self._control is None or not self._index_installation:
            raise AppError("This map has no configured index connection/installation for table context.")
        rows = list(self._control.object_map_table_context(
            self.subject["catalog"], self._database_provider, schema or self.subject["schema"],
            table, node, self._index_installation, object_type, signature))
        if len(rows) != 1 or rows[0]["root_count"] != 1:
            raise AppError("The index does not identify exactly one table/object for this context.")
        read = dict(rows[0])
        root = read["root_id"]
        root_id = alias or node or (f"{_TABLE}{table}" if object_type == "table" else root)
        nodes = [dict(node) for node in read.get("nodes") or []]
        for node in nodes:
            if (node.get("identity") or {}).get("object_type"):
                node["label"] = IndexedDatabaseObject(**node["identity"]).label
            elif (node.get("identity") or {}).get("relative_path"):
                node["role"] = "file"
            if node["id"] == root:
                node.update(id=root_id)
                if table is not None and alias is None and object_type == "table":
                    node["table"] = table
        edges = [{**edge,
                  "source": root_id if edge["source"] == root else edge["source"],
                  "target": root_id if edge["target"] == root else edge["target"]}
                 for edge in read.get("edges") or []]
        return {"root_id": root_id, "nodes": nodes, "edges": edges}

    def _schema_scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """The schema's tables, the external tables its foreign keys reach, and those keys.

        A foreign key to a table outside this schema is related to that table as
        an external element. Each table and key is beside the row it maps.
        On a column's focused lineage (#1426) the elements are its objects' boxes.
        """
        if self._focus.get("column") and self._column_focus:
            return [(box, None) for box in self._focus_boxes()], []
        held = self._database
        if any(self._focus.get(key) for key in ("table", "node", "object")) and self._table_context:
            return ([(dict(n), held.table(str(n.get("table", "")))) for n in self._table_context["nodes"]],
                    [(dict(e), None) for e in self._table_context["edges"]])
        schema = self.subject["schema"]
        here = set(self._table_names())
        elements: dict[str, _Mapped] = {}
        for table in held.tables:
            name = table.identity.name
            elements[f"{_TABLE}{name}"] = ({"id": f"{_TABLE}{name}", "kind": "table",
                                            "table": name, "label": name}, table)
        relations: list[_Mapped] = []
        for table in held.tables:
            name = table.identity.name
            for index, key in enumerate(table.foreign_keys):
                inside = key.referenced_schema == schema and key.referenced_table in here
                target = (f"{_TABLE}{key.referenced_table}" if inside else
                          f"{_EXTERNAL}{key.referenced_schema}.{key.referenced_table}")
                if not inside and target not in elements:
                    elements[target] = ({"id": target, "kind": "external",
                                         "label": f"{key.referenced_schema}.{key.referenced_table}"},
                                        None)
                relations.append(({
                    "id": f"fk:{name}:{key.name}:{index}", "kind": "foreign_key",
                    "source": f"{_TABLE}{name}", "target": target,
                }, key))
        return list(elements.values()), relations

    def _schema_projection(self) -> dict[str, Any]:
        """The schema's tables and their foreign keys; opened tables show their columns.

        A foreign key to a table outside this schema is drawn to that table as
        an external node.
        """
        self._column_rows = set()
        self._member_identities = {}
        if self._focus.get("column") and self._column_focus:
            return self._column_focus_projection()
        opened = set(self._focus.get("opened") or [])
        elements, relations = self._schema_scope()
        # Where the index can be read (#1426), every table and view is an ER box
        # with a +/- header; an expanded one shows its columns as rows, and
        # #1425's derivations attach to rows or boxes as each end is expanded.
        openable = ([element for element, _ in elements if self._opens(element)]
                    if self._control is not None else [])
        # Functions and procedures are the same panel (#1426 design): each opens
        # like a view, to its parameters -- IN, OUT and INOUT (#1440).
        panels = ([element for element, _ in elements
                   if self._is_code_object(element) and not self._opens(element)]
                  if self._control is not None else [])
        with_lineage = bool(openable)
        nodes = []
        for element, table in elements:
            if table is None:
                nodes.append(element)
                continue
            keyed = table.column_keys()["primary_key"]
            nodes.append({**element, "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable,
                 "primary_key": c.name in keyed}
                for c in sorted(table.columns, key=lambda c: c.ordinal)
            ] if element.get("table") in opened and not with_lineage else []})
        edges = [{**relation, **({"columns": list(key.columns),
                  "referenced_columns": list(key.referenced_columns)} if key else {})}
                 for relation, key in relations]
        self._lineage_ids = set()
        toggles: dict[str, Any] = {}
        expanded = {element["id"] for element in openable
                    if element["id"] in opened or element.get("table") in opened}
        self._er_headers(nodes, openable, expanded, panels)
        if with_lineage:
            toggles = {"toggles": [self._lineage_toggle()]}
            if expanded:
                rows = {element["id"]: self._schema_row(element, row) for element, row in elements}
                lineage = self._column_lineage(nodes, edges, openable, expanded, rows)
                if lineage is not None:
                    shown = self._drop_hidden_objects(nodes, edges)
                    self._drop_hidden_connections(nodes, edges)
                    return {"nodes": nodes, "edges": edges, "path": self._path(),
                            **toggles, **self._view_actions(
                                [one for one in openable if one["id"] in shown],
                                [one for one in panels if one["id"] in shown]), **lineage}
        details = None
        selected_edge = next(((edge, key) for edge, key in relations
                              if edge["id"] == self._selected), None)
        if selected_edge is not None:
            edge, key = selected_edge
            names = {node["id"]: node.get("label", node["id"]) for node in nodes}
            rows = [["Relationship", edge["kind"]],
                    ["Source", names.get(edge["source"], edge["source"])],
                    ["Target", names.get(edge["target"], edge["target"])]]
            if key and edge["kind"] == "foreign_key":
                rows.extend([["Constraint", key.name],
                             ["Columns", ", ".join(key.columns)],
                             ["Referenced columns", ", ".join(key.referenced_columns)]])
            details = {"rows": rows}
        shown = self._drop_hidden_objects(nodes, edges)
        self._drop_hidden_connections(nodes, edges)
        return {"nodes": nodes, "edges": edges, "path": self._path(), "details": details,
                **toggles, **self._view_actions([one for one in openable if one["id"] in shown],
                                                [one for one in panels if one["id"] in shown])}

    # -- column lineage on the schema projection (#1426) ----------------------

    def _view_actions(self, openable: list[dict[str, Any]],
                      panels: list[dict[str, Any]]) -> dict[str, Any]:
        """The whole view's controls (#1426), as declared actions (#1151): each its role
        and the complete command that takes it.

        The visibility toggles (Map Options): each generation -- on a map focused on
        one object -- and each object type, shown (``hide`` it) or hidden (``show``
        it). Then, of the objects shown, only the +/- that applies now -- expand (one
        closed) or collapse (all open); a hidden object keeps whether it is open.
        Then hide every shown object's connections (none hidden) or show them all.
        ``openable`` and ``panels`` are the objects shown."""
        actions: list[dict[str, Any]] = []
        focused = (not self._focus.get("column") and self._table_context is not None
                   and any(self._focus.get(key) for key in ("table", "node", "object")))
        for on, key, value in (_GENERATION_TOGGLES if focused else ()) + _TYPE_TOGGLES:
            hidden_values = list(self._focus.get(key) or [])
            hidden = value in hidden_values
            values = [one for one in hidden_values if one != value] if hidden else hidden_values + [value]
            focus = {name: held for name, held in self._focus.items() if name != key}
            actions.append({"role": {"act": _SHOW if hidden else _HIDE, "on": on},
                            "command": {"command": "focus", "focus": {
                                **focus, **({key: sorted(values)} if values else {})}}})
        if openable:
            every = [str(element.get("table") or element["id"]) for element in openable]
            opened = set(self._focus.get("opened") or [])
            closed = any(element["id"] not in opened and element.get("table") not in opened
                         for element in openable)
            # A hidden object stays as open or closed as it was.
            kept = [one for one in self._focus.get("opened") or []
                    if one not in every and not any(one == element["id"] for element in openable)]
            actions.append({"role": {"act": _EXPAND if closed else _COLLAPSE, "on": "opened"},
                            "command": {"command": "focus", "focus": {
                                **self._focus, "opened": (every if closed else []) + kept}}})
        objects = [element["id"] for element in openable + panels]
        # Column lines and header lines each hide on their own (#1426).
        for _, on in _HIDDEN_LINES if objects else ():
            hidden = bool(self._focus.get(on))
            focus = {key: value for key, value in self._focus.items() if key != on}
            actions.append({"role": {"act": _SHOW if hidden else _HIDE, "on": on},
                            "command": {"command": "focus", "focus": {
                                **focus, **({} if hidden else {on: objects})}}})
        return {"actions": actions} if actions else {}

    def _lineage_toggle(self) -> dict[str, Any]:
        """The toolbar's Transforms toggle: whether it is on, and the focus a press sends."""
        detail = self._focus.get("lineage") == _DERIVATION_DETAIL
        return {"id": "lineage", "label": _DERIVATION_DETAIL_LABEL, "pressed": detail,
                "focus": {**self._focus,
                          "lineage": _COLUMN_FLOW if detail else _DERIVATION_DETAIL}}

    def _er_headers(self, nodes: list[dict[str, Any]], openable: list[dict[str, Any]],
                    expanded: set[str], panels: list[dict[str, Any]]) -> None:
        """Every table, view and function one panel (#1426): a header row with its
        type and fully qualified name, and on it (``beside`` the header, right to
        left in ``ordinal`` order) a table's or view's +/- that shows or hides its
        columns, and every panel's controls that hide or restore its column lines
        and its header lines, each on its own."""
        boxes = {element["id"] for element in openable}
        code = {element["id"] for element in panels}
        hidden = set(self._focus.get("hidden_connections") or [])
        unrelated = set(self._focus.get("hidden_relationships") or [])
        for node in list(nodes):
            if node["id"] not in boxes | code:
                continue
            schema, name, object_type = self._opened_identity(node)
            node["expandable"] = True
            node["expanded"] = node["id"] in expanded
            header = f"{_HEADER}{node['id']}"
            nodes.append({"id": header, "kind": "header", "parent": node["id"],
                          "label": f"{schema}.{name}", "object_type": object_type,
                          "ordinal": 0})
            self._lineage_ids.add(header)
            if node["id"] in boxes:
                nodes.append({"id": f"{_TOGGLE}{node['id']}", "kind": "toggle",
                              "parent": node["id"], "beside": header, "label": "",
                              "expanded": node["id"] in expanded, "ordinal": 0})
                self._lineage_ids.add(f"{_TOGGLE}{node['id']}")
            nodes.append({"id": f"{_LINKS}{node['id']}", "kind": "links",
                          "parent": node["id"], "beside": header, "label": "",
                          "hidden": node["id"] in hidden, "ordinal": 1})
            self._lineage_ids.add(f"{_LINKS}{node['id']}")
            nodes.append({"id": f"{_RELATIONS}{node['id']}", "kind": "relations",
                          "parent": node["id"], "beside": header, "label": "",
                          "hidden": node["id"] in unrelated, "ordinal": 2})
            self._lineage_ids.add(f"{_RELATIONS}{node['id']}")
            # The pill the header's controls sit in, one outline around them all.
            nodes.append({"id": f"{_CONTROLS}{node['id']}", "kind": "controls",
                          "parent": node["id"], "wraps": header, "label": ""})

    @staticmethod
    def _is_code_object(element: dict[str, Any]) -> bool:
        """A database object on the screen that is no table or view (a function, a
        procedure): it has an indexed object type, and is no source file."""
        identity = element.get("identity") or {}
        return (element.get("kind") not in _OPENABLE and element.get("role") != "file"
                and bool(identity.get("object_type")))

    @classmethod
    def _opens(cls, element: dict[str, Any]) -> bool:
        """Whether a drawn object opens to show its rows (#1426): a table or view
        (its columns), or a function (its inputs and outputs)."""
        return element.get("kind") in _OPENABLE or (
            cls._is_code_object(element)
            and (element.get("identity") or {}).get("object_type") in _OPENABLE_ROUTINES)

    def _header_object(self, node_id: str) -> str:
        """A header row, its +/-, or a function's group heading stands for its
        object: right-click and drill act on the object, and a click selects it."""
        for prefix in (_HEADER, _TOGGLE, _LINKS, _RELATIONS,
                       *(group for group, _, _ in _PARAMETER_GROUPS)):
            if node_id.startswith(prefix) and node_id in self._lineage_ids:
                return node_id[len(prefix):]
        return node_id

    def _toggle_connections(self, element_id: str, key: str) -> None:
        """Hide or restore a drawn object's lines of one kind (#1426): its column
        lines (``hidden_connections``) or its header lines (``hidden_relationships``)."""
        hidden = list(self._focus.get(key) or [])
        if element_id in hidden:
            hidden.remove(element_id)
        else:
            hidden.append(element_id)
        self._focus = {one: value for one, value in self._focus.items() if one != key}
        if hidden:
            self._focus[key] = hidden

    def _drop_hidden_objects(self, nodes: list[dict[str, Any]],
                             edges: list[dict[str, Any]]) -> set[str]:
        """Take out the objects the Map Options hide (#1426): those of a hidden
        generation -- on a map focused on one object -- or of a hidden type, never
        the focused object itself; with them their rows, every line with an end
        among them, and every transform that lost a line. Filters what this map
        holds; reads nothing. Returns the ids of the objects still shown."""
        objects = [node for node in nodes if not node.get("parent") and node.get("kind") != "transform"]
        generations = set(self._focus.get("hidden_generations") or [])
        types = set(self._focus.get("hidden_object_types") or [])
        focused = (not self._focus.get("column") and self._table_context is not None
                   and any(self._focus.get(key) for key in ("table", "node", "object")))
        root = self._table_context["root_id"] if focused else None
        hidden = set() if self._focus.get("column") else {
            node["id"] for node in objects if node["id"] != root and (
                (focused and node.get("depth") in generations)
                or _object_type_of(node) in types)}
        if hidden:
            nodes[:] = [node for node in nodes
                        if node["id"] not in hidden and node.get("parent") not in hidden]
            transforms = {node["id"] for node in nodes if node.get("kind") == "transform"}
            gone = set(hidden)
            while True:
                kept = {node["id"] for node in nodes}
                lost = [edge for edge in edges
                        if edge["source"] not in kept or edge["target"] not in kept]
                if not lost:
                    break
                edges[:] = [edge for edge in edges if edge not in lost]
                # A transform with a line gone shows a derivation it no longer completes.
                broken = {end for edge in lost for end in (edge["source"], edge["target"])
                          if end in transforms and end not in gone}
                gone |= broken
                nodes[:] = [node for node in nodes if node["id"] not in broken]
        return {node["id"] for node in objects if node["id"] not in hidden}

    def _drop_hidden_connections(self, nodes: list[dict[str, Any]],
                                 edges: list[dict[str, Any]]) -> None:
        """Take out the hidden lines (#1426), each kind on its own: a header line
        (object to object) of an object whose relationships are hidden, and a
        column line (with a row or transform at an end) of an object whose
        connections are hidden. The objects, their rows and every other line stay."""
        hidden = set(self._focus.get("hidden_connections") or [])
        unrelated = set(self._focus.get("hidden_relationships") or [])
        if not hidden and not unrelated:
            return
        owner = {node["id"]: node.get("parent") or node["id"] for node in nodes}
        objects = {node["id"] for node in nodes
                   if not node.get("parent") and node.get("kind") != "transform"}

        def shown(edge: dict[str, Any]) -> bool:
            """Whether a line stays: no end's object hides lines of its kind."""
            header_line = edge["source"] in objects and edge["target"] in objects
            off = unrelated if header_line else hidden
            return (owner.get(edge["source"], edge["source"]) not in off
                    and owner.get(edge["target"], edge["target"]) not in off)

        edges[:] = [edge for edge in edges if shown(edge)]

    def _toggle_open(self, element_id: str) -> None:
        """Show or hide a drawn table's or view's columns: tables by name, others by node."""
        element = next(n for n, _ in self._schema_scope()[0] if n["id"] == element_id)
        name = str(element.get("table") or element_id)
        opened = [one for one in self._focus.get("opened") or [] if one not in (name, element_id)]
        if len(opened) == len(self._focus.get("opened") or []):
            opened.append(name)
        self._focus["opened"] = opened

    def _schema_row(self, element: dict[str, Any], row: Any) -> DatabaseTable | None:
        """The schema read's table for a table in the map's schema: its keys are known there.

        A table outside the map's schema, and every view, has none here.
        """
        if row is not None:
            return row
        identity = element.get("identity") or {}
        if (identity.get("schema") != self.subject["schema"]
                or identity.get("object_type") != "table"):
            return None
        return self._database.table(str(identity.get("name")))

    @staticmethod
    def _column_keys(row: dict[str, Any] | None) -> dict[str, set[str]]:
        """Which of a table's columns are primary key, foreign key and unique, from its read."""
        if row is None:
            return {"primary_key": set(), "foreign_key": set(), "unique": set()}
        unique = {column for index in row.get("indexes") or [] if index.get("unique")
                  for column in index.get("columns") or []}
        unique |= {column for constraint in row.get("constraints") or []
                   if constraint.get("kind") == DatabaseConstraint.UNIQUE
                   for column in constraint.get("columns") or []}
        return {"primary_key": set((row.get("primary_key") or {}).get("columns") or []),
                "foreign_key": {column for key in row.get("foreign_keys") or []
                                for column in key.get("columns") or []},
                "unique": unique}

    def _column_lineage(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                        openable: list[dict[str, Any]], expanded: set[str],
                        rows: dict[str, Any]) -> dict[str, Any] | None:
        """Draw the expanded tables' and views' column rows, and the edges that reach them.

        Read once per projection through the governed read
        (``code.f_object_map_column_lineage``) for every drawn table and view, and
        never kept. Each #1425 derivation path from a column to the next column
        is drawn when either end is expanded; each end attaches to its row, or to
        its object's box when that object is collapsed. Level 1 (column flow)
        hides the transforms between, level 2 (derivation detail) draws them, and
        a transform inside a collapsed object stays hidden. Foreign keys are drawn
        per column pair the same way. A selected row, transform or edge is
        marked -- upstream, downstream, the rest subdued -- and described in the
        details; the answer is None when the selection is none of these.
        """
        identities = {element["id"]: self._opened_identity(element) for element in openable}
        selected_member = (int(self._selected[3:]) if self._selected
                           and self._selected.startswith("db:")
                           and self._selected[3:].isdigit() else None)
        read = self._control.object_map_column_lineage(
            self.subject["catalog"], self._database_provider,
            list(identities.values()), selected_member)
        owner_of = {identity: element_id for element_id, identity in identities.items()}
        # Database objects only: a source file on the screen has no schema identity.
        drawn_owner = {self._opened_identity(n): n["id"] for n in nodes
                       if (n.get("identity") or {}).get("object_type")}
        columns: dict[str, dict[str, Any]] = {}
        display: dict[str, str] = {}
        member_of: dict[tuple[str, str], str] = {}
        names: dict[str, str] = {}
        for column in read.get("columns") or []:
            owner = owner_of.get((column["schema"], column["name"], column["object_type"]))
            if owner is None:
                continue
            member_of[(owner, column["column"])] = column["id"]
            names[column["id"]] = f"{column['schema']}.{column['name']}.{column['column']}"
            display[column["id"]] = column["id"] if owner in expanded else owner
            if owner not in expanded:
                continue
            # Its keys are its table's own answer (#1441); a table this map holds no
            # catalog read of -- outside the schema, or a view -- knows none.
            table = rows.get(owner)
            keys = table.column_keys() if table is not None else _NO_COLUMN_KEYS
            columns[column["id"]] = {
                "id": column["id"], "kind": "column", "parent": owner,
                "label": column["column"],
                "column": column["column"], "data_type": column["data_type"],
                "type_category": data_type_category(column["data_type"]),
                "ordinal": column["ordinal"],
                **({"mode": column["mode"]} if column.get("mode") else {}),
                **{key: column["column"] in held for key, held in keys.items()},
            }
            self._member_identities[column["id"]] = {
                "schema": column["schema"], "object": column["name"],
                "object_type": column["object_type"], "column": column["column"]}
        groups = self._parameter_groups(columns, identities)
        detail = self._focus.get("lineage") == _DERIVATION_DETAIL
        transforms = {
            one["id"]: {
                "id": one["id"], "kind": "transform", "role": one["role"],
                "label": _transform_label(one["role"], one["expression"], one["key"]),
                "expression": one["expression"], "key": one["key"],
                "owner": f"{one['schema']}.{one['name']}",
                "within": drawn_owner.get((one["schema"], one["name"], one["object_type"])),
            } for one in read.get("transforms") or []
        }
        shown = {key for key, one in transforms.items()
                 if detail and (one["within"] is None or one["within"] in expanded)
                 and _transforms_anything(one["role"], one["expression"])}
        drawn_edges = self._derivation_edges(read.get("edges") or [], display, columns,
                                             transforms, shown)
        drawn_edges.update(self._foreign_key_edges(edges, rows, identities, expanded,
                                                   display, member_of))
        used = {end for edge in drawn_edges.values() for end in (edge["from"], edge["to"])}
        drawn_transforms = [{k: v for k, v in one.items() if k != "within"}
                            for key, one in transforms.items() if key in shown and key in used]
        nodes.extend(groups)
        nodes.extend(columns.values())
        nodes.extend(drawn_transforms)
        edges.extend(drawn_edges.values())
        names.update({one["id"]: one["expression"] for one in drawn_transforms})
        self._lineage_ids |= (set(columns) | {one["id"] for one in drawn_transforms}
                              | set(drawn_edges) | {one["id"] for one in groups})
        self._column_rows = set(columns)
        if self._selected not in self._lineage_ids:
            return None
        upstream = {one["id"] for one in read.get("upstream") or []}
        downstream = {one["id"] for one in read.get("downstream") or []}
        self._lineage_marks(nodes, edges, upstream, downstream, display)
        return {"details": self._lineage_details(read, columns, transforms, drawn_edges,
                                                 set(display) | used, names),
                "marked": True, "selected": self._selected}

    @staticmethod
    def _parameter_groups(columns: dict[str, dict[str, Any]],
                          identities: dict[str, tuple[str, str, str]]) -> list[dict[str, Any]]:
        """An opened function's rows in their groups (#1426): its inputs under an
        Inputs heading at the top, then its outputs under an Outputs heading.

        The group is the parameter's mode (``parameter_role``), never its name or
        type. Each function's rows are numbered in that order, in their call order
        within a group; a group with no parameters has no heading. Returns the
        heading rows; the parameter rows are renumbered in place.
        """
        headings: list[dict[str, Any]] = []
        functions = {owner for owner, (_, _, kind) in identities.items()
                     if kind in _OPENABLE_ROUTINES}
        for owner in sorted(functions):
            held = sorted((one for one in columns.values() if one["parent"] == owner),
                          key=lambda one: one["ordinal"])
            ordinal = 0
            for prefix, label, roles in _PARAMETER_GROUPS:
                members = [one for one in held if parameter_role(one.get("mode")) in roles]
                if not members:
                    continue
                ordinal += 1
                headings.append({"id": f"{prefix}{owner}", "kind": "group", "parent": owner,
                                 "label": label, "ordinal": ordinal})
                for one in members:
                    ordinal += 1
                    one["ordinal"] = ordinal
        return headings

    # -- a column's focused lineage (#1426) ------------------------------------

    def _read_column_focus(self, column: str, depth: int | None = _COLUMN_FOCUS_DEPTH,
                           kinds: tuple[str, ...] = ("column",)) -> dict[str, Any]:
        """A member's lineage to ``depth`` -- the map's depth unless given, all of it
        for None (#1429) -- by the governed read; with its label.

        Raises:
            AppError: When the map cannot read the index, or the node is no
                indexed member of ``kinds``.
        """
        member = column[len("db:"):] if column.startswith("db:") else ""
        if self._control is None or not member.isdigit():
            raise AppError("This map can focus only an indexed column's lineage.")
        read = dict(self._control.object_map_column_focus(int(member), depth))
        start = next((one for one in read.get("nodes") or [] if one["id"] == column), None)
        if start is None or start["member_kind"] not in kinds:
            raise AppError("The index holds no column for this node.")
        read["label"] = f"{start['schema']}.{start['object']}.{start['name']}"
        return read

    def _focus_boxes(self) -> list[dict[str, Any]]:
        """The objects holding the focused lineage's columns, one ER box each."""
        boxes: dict[str, dict[str, Any]] = {}
        for one in (self._column_focus or {}).get("nodes") or []:
            if one["member_kind"] != "column" or one["object_id"] in boxes:
                continue
            boxes[one["object_id"]] = {
                "id": one["object_id"],
                "kind": one["object_type"] if one["object_type"] in _OPENABLE else "code",
                "label": f"{one['schema']}.{one['object']}",
                "identity": {"schema": one["schema"], "name": one["object"],
                             "object_type": one["object_type"],
                             "signature": one.get("signature") or ""}}
        return list(boxes.values())

    def _column_focus_projection(self) -> dict[str, Any]:
        """A double-clicked column's lineage within the map's depth (#1426).

        The approved visual: each object holding a participating column is an
        ER box of its name and those columns only; the transforms on the paths
        between those columns are small nodes along the lines, labelled by role
        and what they apply; arrows show the flow, left to right. A transform
        on no path between two drawn columns is not drawn. Right-click (the
        details) states a transform's or line's full expression and evidence.
        Depth is the read's own. A row double-clicked here focuses that column
        in turn; the path leads back.
        """
        read = self._column_focus or {}
        boxes = self._focus_boxes()
        nodes: list[dict[str, Any]] = []
        for box in boxes:
            nodes.append({**box, "expandable": True})
            nodes.append({"id": f"{_HEADER}{box['id']}", "kind": "header",
                          "parent": box["id"], "label": box["label"],
                          "object_type": box["identity"]["object_type"], "ordinal": 0})
        columns: dict[str, dict[str, Any]] = {}
        transforms: dict[str, dict[str, Any]] = {}
        for one in read.get("nodes") or []:
            reach = {"depth": one["depth"], "upstream": one["upstream"],
                     "downstream": one["downstream"],
                     "owner": f"{one['schema']}.{one['object']}"}
            if one["member_kind"] != "column":
                transforms[one["id"]] = {
                    "id": one["id"], "kind": "transform", "role": one["role"],
                    "label": _transform_label(one["role"], one["expression"], one["name"]),
                    "expression": one["expression"], **reach}
                continue
            box = next(b for b in boxes if b["id"] == one["object_id"])
            table = self._schema_row(box, None)
            keys = table.column_keys() if table is not None else _NO_COLUMN_KEYS
            columns[one["id"]] = {
                "id": one["id"], "kind": "column", "parent": one["object_id"],
                "label": one["name"], "column": one["name"],
                "data_type": one["data_type"], "ordinal": one["ordinal"],
                **{key: one["name"] in held for key, held in keys.items()}, **reach}
            self._member_identities[one["id"]] = {
                "schema": one["schema"], "object": one["object"],
                "object_type": one["object_type"], "column": one["name"]}
        raw = read.get("edges") or []
        # Every path between two drawn columns, its transforms shown -- but for one
        # that only carries a column forward: there the line runs column to column.
        drawn = self._derivation_edges(raw, {key: key for key in columns}, columns,
                                       transforms, {key for key, one in transforms.items()
                                                    if _transforms_anything(one["role"],
                                                                            one["expression"])})
        edges = [{key: value for key, value in edge.items() if key not in ("from", "to")}
                 for edge in drawn.values()]
        used = {end for edge in edges for end in (edge["source"], edge["target"])}
        nodes.extend(columns.values())
        nodes.extend(one for key, one in transforms.items() if key in used)
        self._column_rows = set(columns)
        self._lineage_ids = ({f"{_HEADER}{box['id']}" for box in boxes} | set(columns)
                             | (set(transforms) & used) | {edge["id"] for edge in edges})
        held = self._lineage_ids | {box["id"] for box in boxes}
        selected = self._selected if self._selected in held else self._focus["column"]
        self._focus_marks(nodes, edges, selected)
        return {"nodes": nodes, "edges": edges, "path": self._path(),
                "details": self._focus_details(selected, columns, transforms, edges),
                "marked": True, "selected": selected,
                "direction": _COLUMN_FOCUS_DIRECTION, "legend": _COLUMN_FOCUS_LEGEND}

    def _focus_marks(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                     selected: str) -> None:
        """The selection in focus; every member and line upstream or downstream of the column."""
        column = self._focus["column"]
        upstream = {node["id"] for node in nodes if node.get("upstream")}
        downstream = {node["id"] for node in nodes if node.get("downstream")}
        # A selected line's columns read as selected columns (#1426).
        joined = _line_columns(nodes, next((edge for edge in edges if edge["id"] == selected), None),
                               {})
        lit: set[str] = set()
        for node in nodes:
            node["highlight"] = (["focus"] if node["id"] == selected or node["id"] in joined else
                                 ["upstream"] if node["id"] in upstream else
                                 ["downstream"] if node["id"] in downstream else [])
            if node["highlight"] and node.get("parent"):
                lit.add(node["parent"])
        _mark_holding_objects(nodes, lit, selected)
        for edge in edges:
            edge["highlight"] = (
                ["focus"] if edge["id"] == selected else
                ["upstream"] if edge["source"] in upstream
                and edge["target"] in upstream | {column} else
                ["downstream"] if edge["target"] in downstream
                and edge["source"] in downstream | {column} else [])

    def _focus_details(self, selected: str, columns: dict[str, dict[str, Any]],
                       transforms: dict[str, dict[str, Any]],
                       edges: list[dict[str, Any]]) -> dict[str, Any]:
        """What is selected on the focused lineage: a transform or line states its
        full expression and evidence."""
        names = {**{key: f"{one['owner']}.{one['column']}" for key, one in columns.items()},
                 **{key: one["label"] for key, one in transforms.items()}}
        edge = next((e for e in edges if e["id"] == selected), None)
        if selected in columns:
            column = columns[selected]
            rows: list[list[Any]] = [["Column", column["column"]], ["Type", column["data_type"]],
                                     ["Object", column["owner"]], ["Depth", column["depth"]]]
        elif selected in transforms:
            one = transforms[selected]
            rows = [["Transform", one["label"]], ["Expression", one["expression"]],
                    ["Object", one["owner"]], ["Depth", one["depth"]]]
            for line in edges:
                if selected in (line["source"], line["target"]):
                    rows.extend(_evidence_rows(line))
        elif edge is not None:
            rows = [["Relationship", edge.get("reference_kind") or edge["kind"]],
                    ["Source", names.get(edge["source"], edge["source"])],
                    ["Target", names.get(edge["target"], edge["target"])]]
            rows.extend(["Expression", transforms[end]["expression"]]
                        for end in (edge["source"], edge["target"]) if end in transforms)
            rows.extend(_evidence_rows(edge))
        else:
            box = next((b for b in self._focus_boxes() if b["id"] == selected), {})
            rows = [["Object", box.get("label", selected)]]
        rows.append(["Lineage", f"{sum(1 for c in columns.values() if c['upstream'])} upstream, "
                                f"{sum(1 for c in columns.values() if c['downstream'])} downstream "
                                "columns in its lineage"])
        return {"rows": rows}

    def _opened_identity(self, element: dict[str, Any]) -> tuple[str, str, str]:
        """(schema, name, object_type) of a drawn table or view, as the index names it."""
        identity = element.get("identity") or {}
        if identity:
            return (str(identity["schema"]), str(identity["name"]), str(identity["object_type"]))
        return (self.subject["schema"], str(element.get("table") or element["label"]),
                str(element["kind"]))

    @staticmethod
    def _derivation_edges(raw: list[dict[str, Any]], display: dict[str, str],
                          columns: dict[str, Any], transforms: dict[str, Any],
                          shown: set[str]) -> dict[str, dict[str, Any]]:
        """Each derivation path between two columns, its ends where they are drawn.

        Computed from the read, never stored: from every column follow
        derivations through transforms to the first column reached. A path is
        drawn when either end column is a row; its ends attach to their rows or
        their objects' boxes, its shown transforms stay, and the hidden ones are
        kept as what it runs through. An edge is named by the underlying pair it
        joins, so edges converging on one collapsed box stay distinct.
        """
        onward: dict[str, list[dict[str, Any]]] = {}
        for edge in raw:
            onward.setdefault(edge["source"], []).append(edge)
        drawn: dict[str, dict[str, Any]] = {}
        for start in display:
            stack: list[tuple[str, tuple[str, ...], tuple[dict[str, Any], ...]]] = [
                (start, (start,), ())]
            while stack:
                node, path, hops = stack.pop()
                for edge in onward.get(node, ()):
                    target = edge["target"]
                    if target in display:
                        if start in columns or target in columns:
                            ObjectMap._path_segments(drawn, path + (target,), hops + (edge,),
                                                     display, shown)
                    elif target in transforms and target not in path:
                        stack.append((target, path + (target,), hops + (edge,)))
        return drawn

    @staticmethod
    def _path_segments(drawn: dict[str, dict[str, Any]], path: tuple[str, ...],
                       hops: tuple[dict[str, Any], ...], display: dict[str, str],
                       shown: set[str]) -> None:
        """One derivation path's edges: between its two ends and the transforms shown on it.

        Two neighbours on the path keep their own derivation (and its evidence);
        a gap over hidden transforms is a column flow through them.
        """
        kept = [at for at, node in enumerate(path)
                if at in (0, len(path) - 1) or node in shown]
        for first, second in zip(kept, kept[1:]):
            start, end = path[first], path[second]
            source, target = display.get(start, start), display.get(end, end)
            if source == target:
                continue
            if second == first + 1:
                hop = hops[first]
                drawn.setdefault(hop["id"], {**hop, "kind": "derivation",
                                             "reference_kind": hop["kind"], "source": source,
                                             "target": target, "from": start, "to": end})
                continue
            edge_id = f"{_COLUMN_FLOW_EDGE}{start}:{end}"
            flow = drawn.setdefault(edge_id, {"id": edge_id, "kind": "derivation",
                                              "source": source, "target": target,
                                              "from": start, "to": end, "via": []})
            flow["via"] = sorted(set(flow["via"]) | set(path[first + 1:second]))

    @staticmethod
    def _foreign_key_edges(edges: list[dict[str, Any]], rows: dict[str, Any],
                           identities: dict[str, tuple[str, str, str]], expanded: set[str],
                           display: dict[str, str],
                           member_of: dict[tuple[str, str], str]) -> dict[str, dict[str, Any]]:
        """A foreign key with an expanded end, drawn per column pair instead of per object.

        Only keys the schema read holds (tables in the map's schema) are known by
        column; each end attaches to its row, or to its object's box when that
        object is collapsed. Both ends collapsed: the object-level key stays.
        """
        by_identity = {identity: element_id for element_id, identity in identities.items()}
        ends = {edge["id"] for edge in edges}
        drawn: dict[str, dict[str, Any]] = {}
        replaced: set[tuple[str, str]] = set()
        for source, row in rows.items():
            if row is None or source not in identities:
                continue
            for index, key in enumerate(row.foreign_keys):
                target = by_identity.get((key.referenced_schema, key.referenced_table, "table"))
                if target is None:
                    outside = f"{_EXTERNAL}{key.referenced_schema}.{key.referenced_table}"
                    target = outside if any(edge["target"] == outside for edge in edges) else None
                if target is None or (source not in expanded and target not in expanded):
                    continue
                replaced |= {(source, target), (target, source)}
                for pair, (column, referenced) in enumerate(zip(key.columns, key.referenced_columns)):
                    start = member_of.get((source, column), source)
                    end = member_of.get((target, referenced), target)
                    edge_id = f"{_FK_COLUMN_EDGE}{source}:{key.name}:{index}:{pair}"
                    if edge_id in ends:
                        continue
                    drawn[edge_id] = {
                        "id": edge_id, "kind": "foreign_key",
                        "source": display.get(start, start), "target": display.get(end, end),
                        "from": start, "to": end, "constraint": key.name,
                        "columns": [column], "referenced_columns": [referenced]}
        edges[:] = [edge for edge in edges if not (
            edge["kind"] == "foreign_key" and (edge["source"], edge["target"]) in replaced)]
        return drawn

    def _lineage_marks(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                       upstream: set[str], downstream: set[str],
                       display: dict[str, str]) -> None:
        """Upstream and downstream of the selection marked; everything else subdued.

        A collapsed box is marked for the columns it holds; an edge by the
        underlying pair it joins.
        """
        selected = self._selected
        chosen_edge = next((edge for edge in edges if edge["id"] == selected), None)
        if chosen_edge is not None:
            # An edge selected: what its source derives from, and what its target feeds.
            upstream = upstream | {chosen_edge.get("from", chosen_edge["source"])}
            downstream = downstream | {chosen_edge.get("to", chosen_edge["target"])}
        before = upstream | {selected}
        after = downstream | {selected}
        shown_up = {display.get(one, one) for one in upstream}
        shown_down = {display.get(one, one) for one in downstream}
        # A selected line's columns read as selected columns (#1426).
        joined = _line_columns(nodes, chosen_edge, display)
        lit: set[str] = set()
        for node in nodes:
            if node["kind"] in _BOX_PARTS:
                node["highlight"] = []
                continue
            node["highlight"] = (["focus"] if node["id"] == selected or node["id"] in joined else
                                 ["upstream"] if node["id"] in shown_up else
                                 ["downstream"] if node["id"] in shown_down else [])
            if node["highlight"] and node.get("parent"):
                lit.add(node["parent"])
        _mark_holding_objects(nodes, lit, selected)
        for node in nodes:
            # An object holding a lit column is not subdued: its rows would be too.
            if not node["highlight"] and node["kind"] not in _BOX_PARTS and node["id"] not in lit:
                node["highlight"] = ["subdued"]
        for edge in edges:
            start = edge.get("from", edge["source"])
            end = edge.get("to", edge["target"])
            edge["highlight"] = (["focus"] if edge["id"] == selected else
                                 ["upstream"] if start in upstream and end in before else
                                 ["downstream"] if start in after and end in downstream
                                 else ["subdued"])

    def _lineage_details(self, read: dict[str, Any], columns: dict[str, Any],
                         transforms: dict[str, Any], drawn_edges: dict[str, dict[str, Any]],
                         drawn: set[str], names: dict[str, str]) -> dict[str, Any]:
        """What is selected, and how much of its lineage lies beyond the map (decision (e))."""
        selected = self._selected
        if selected in columns:
            column = columns[selected]
            rows: list[list[Any]] = [["Column", column["column"]], ["Type", column["data_type"]]]
            keys = [key.replace("_", " ") for key in ("primary_key", "foreign_key", "unique")
                    if column.get(key)]
            if keys:
                rows.append(["Keys", ", ".join(keys)])
        elif selected in transforms:
            one = transforms[selected]
            rows = [["Transform", one["key"]], ["Role", one["role"]],
                    ["Expression", one["expression"]], ["Object", one["owner"]]]
        else:
            edge = drawn_edges[selected]
            relationship = (edge["kind"] if edge["kind"] == "foreign_key"
                            else edge.get("reference_kind") or "column flow")
            rows = [["Relationship", relationship],
                    ["Source", names.get(edge["from"], edge["from"])],
                    ["Target", names.get(edge["to"], edge["to"])]]
            if edge["kind"] == "foreign_key":
                rows.append(["Constraint", edge["constraint"]])
                return {"rows": rows}
            if edge.get("via"):
                rows.append(["Through", "; ".join(transforms[one]["expression"]
                                                  for one in edge["via"] if one in transforms)])
            for evidence in edge.get("evidence") or []:
                rows.append(["Evidence", f"{evidence['fact_source']} · {evidence['resolution']}"
                                         f" · {evidence['evidence']}"])
        for direction in ("upstream", "downstream"):
            reached = read.get(direction) or []
            beyond = [one for one in reached
                      if one["member_kind"] == "column" and one["id"] not in drawn]
            objects = {(one["schema"], one["name"]) for one in beyond}
            rows.append([direction.capitalize(),
                         f"{sum(1 for one in reached if one['id'] in drawn)} on the map; "
                         f"{len(beyond)} columns in {len(objects)} objects beyond it"])
        return {"rows": rows}
