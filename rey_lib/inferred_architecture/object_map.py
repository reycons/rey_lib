"""The map of the code estate: one live object, two projections (#1119, #1120).

``ObjectMap`` is the first-class owner of a map's state: what it read, what is
in focus, how a press changes that focus, and the projection of the focused
screen. It is opened on one governed map subject and the subject decides which
projection it draws:

    inferred_architecture {installation}            -> the object projection
    database_schema {connection, catalog, schema}   -> the schema projection

The object projection reads the estate's canonical-object claims, their
relationships, and where the latest inferred-architecture generation places
them. The schema projection draws one database schema from the canonical
database read (``rey_lib.db.database_objects.DatabaseSchema``): the tables and
columns stay the database domain's objects; the map owns only how they are
drawn, related and focused.

A projection is semantic -- nodes, edges, the path back, the focused object's
details, and which node is selected with the connections it highlights. How it
is laid out and styled is the viewer's.

The map owns what an interaction means (#1099): a ``click`` selects a node; a
``double_click`` drills; a ``context_click`` (a right-click) on a claim opens
it. Only the last ever opens, and it answers what the claim IS as a governed
record and stops there: signing it, choosing what opens it and where, are not
the map's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, ClassVar, Final

from rey_lib.db.database_objects import DatabaseSchema
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import read_text_file

__all__ = ["CLAIM_NODE_TYPE", "OBJECT_PROJECTION", "SCHEMA_PROJECTION", "ObjectMap"]

#: The node type a claim is governed as -- the record ``record`` answers.
CLAIM_NODE_TYPE: Final = "canonical_object"

#: The two projections, named by the subject that opens them.
OBJECT_PROJECTION: Final = "object"
SCHEMA_PROJECTION: Final = "schema"

_SQL_DIR: Final = Path(__file__).parent / "sql"

#: The five reads the object projection is built from, in the order they are held.
_OBJECT_READS: Final = ("claims", "relationships", "placements", "areas", "area_links")

_AREA: Final = "area:"
_CLAIM: Final = "claim:"
_TABLE: Final = "table:"
_EXTERNAL: Final = "external:"


def _statement(name: str) -> str:
    """One of the object map's statements, read from its file."""
    return read_text_file(_SQL_DIR / f"object_map_{name}.sql")


class ObjectMap:
    """One map subject, read, held live, focused and projected."""

    #: The presentation a map requires (#1139): what the Console's registry
    #: presents it with. Which screen it shows -- object or schema -- is its own
    #: projection state, never a second presentation type.
    PANEL_TYPE: ClassVar[str] = "map"

    def __init__(self, subject: dict[str, Any], projection_kind: str,
                 read: dict[str, Any]) -> None:
        """Hold what was read. Use ``for_architecture`` or ``for_schema``."""
        self.subject = dict(subject)
        self.projection_kind = projection_kind
        self._read = read
        # The object projection opens on the estate; the schema projection on
        # every table closed to its name.
        self._focus: dict[str, Any] = (
            {"level": "estate"} if projection_kind == OBJECT_PROJECTION else {"opened": []}
        )
        self._project: dict[str, Callable[[], dict[str, Any]]] = {
            OBJECT_PROJECTION: self._object_projection,
            SCHEMA_PROJECTION: self._schema_projection,
        }
        self._drill: dict[str, Callable[[str], None]] = {
            OBJECT_PROJECTION: self._object_drill,
            SCHEMA_PROJECTION: self._schema_drill,
        }
        self._holds: dict[str, Callable[[str], bool]] = {
            OBJECT_PROJECTION: self._holds_object_node,
            SCHEMA_PROJECTION: self._holds_schema_node,
        }
        #: The selected node (#1099), which a click sets and the projection
        #: highlights. None when nothing is selected.
        self._selected: str | None = None

    # -- opening ---------------------------------------------------------------

    @classmethod
    def for_architecture(cls, connection: Any, installation: str) -> "ObjectMap":
        """The estate's object map, read on the shared ``connection``.

        Args:
            connection: The shared Connection the host resolved; held by the
                host, never opened or closed here.
            installation: The installation the map subject names.
        """
        handle = connection.handle()
        read = {name: [dict(row) for row in DatabaseQuery(_statement(name)).rows(handle)]
                for name in _OBJECT_READS}
        return cls({"kind": "inferred_architecture", "installation": installation},
                   OBJECT_PROJECTION, read)

    @classmethod
    def for_schema(cls, connection: Any, catalog: str, schema: str) -> "ObjectMap":
        """One database schema's map, from the canonical database read.

        The schema and its tables are read as the database domain's own objects
        (``DatabaseSchema.read``); the map keeps their serialization to draw.

        Args:
            connection: The shared Connection the subject names, resolved by the
                host; never opened or closed here.
            catalog: The schema's catalog, as the subject names it.
            schema: The schema.
        """
        read = DatabaseSchema.read(connection.handle(), connection.name, catalog, schema).to_dict()
        return cls({"kind": "database_schema", "connection": connection.name,
                    "catalog": catalog, "schema": schema}, SCHEMA_PROJECTION, read)

    # -- behaviour ---------------------------------------------------------------

    def focus(self, **focus: Any) -> None:
        """Move the focus to the screen named, as the path names it.

        Raises:
            ConfigError: When the screen names nothing this map holds.
        """
        if self.projection_kind == SCHEMA_PROJECTION:
            opened = [str(name) for name in focus.get("opened") or []]
            unknown = sorted(set(opened) - set(self._table_names()))
            if unknown:
                raise ConfigError(f"This schema holds no table {unknown}.")
            self._focus = {"opened": opened}
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
            return
        node_id = str(node_id)
        if not self._holds[self.projection_kind](node_id):
            raise ConfigError(f"The map has no node {node_id!r}.")
        self._selected = node_id

    def context_click(self, node_id: str) -> dict[str, Any] | None:
        """Open (#1099): a right-click on a claim opens the subject it represents.

        Validity is this map's own inventory's answer, never the screen on show,
        so the same claim opens from any focus. A node this map holds that is no
        claim -- an area, a table -- has no context-click behaviour: nothing
        changes.

        Returns:
            For a claim, its governed record -- what the claim IS: its node type
            and its own fields. Which action that record offers, and what opens
            it, are its declaration's, never the map's. None for any other node.

        Raises:
            ConfigError: When the node is none this map holds.
        """
        node_id = str(node_id or "")
        if not self._holds[self.projection_kind](node_id):
            raise ConfigError(f"The map has no node {node_id!r}.")
        claim = self._claim_keyed(node_id)
        return {"node_type": CLAIM_NODE_TYPE, **claim} if claim is not None else None

    def double_click(self, node_id: str) -> None:
        """Drill (#1099): move the focus through the node; the projection says where.

        Never opens. The selection belonged to the screen being left, so it
        clears.

        Raises:
            ConfigError: When the node is none this map holds.
        """
        node_id = str(node_id or "")
        if not self._holds[self.projection_kind](node_id):
            raise ConfigError(f"The map has no node {node_id!r}.")
        self._selected = None
        self._drill[self.projection_kind](node_id)

    def projection(self) -> dict[str, Any]:
        """The focused screen: nodes, edges, the path back, the details, and the
        selected node with the connections it highlights -- with the
        presentation the map requires (``panel_type``) beside which screen it
        is (``projection``)."""
        screen = self._project[self.projection_kind]()
        selected = self._marked(screen["nodes"], screen["edges"])
        return {
            "subject": dict(self.subject),
            "panel_type": self.PANEL_TYPE,
            "projection": self.projection_kind,
            "focus": dict(self._focus),
            "selected": selected,
            **screen,
        }

    def _marked(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str | None:
        """Mark the selected node and its connections on this screen; the selection shown.

        ``highlight`` on every node and edge: ``focus`` the selected node,
        ``first`` it and what it links to directly (and the links among those),
        ``second`` what those link to next -- empty when nothing on this screen
        is selected.
        """
        on_screen = {node["id"] for node in nodes}
        selected = self._selected if self._selected in on_screen else None
        near: dict[str, set[str]] = {}
        for edge in edges:
            near.setdefault(edge["source"], set()).add(edge["target"])
            near.setdefault(edge["target"], set()).add(edge["source"])
        first = (near.get(selected, set()) - {selected}) if selected else set()
        second = set().union(*(near.get(one, set()) for one in first)) - first - {selected}
        for node in nodes:
            node["highlight"] = (["focus", "first"] if node["id"] == selected else
                                 ["first"] if node["id"] in first else
                                 ["second"] if node["id"] in second else [])
        for edge in edges:
            ends = {edge["source"], edge["target"]}
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

    def _object_projection(self) -> dict[str, Any]:
        level = self._focus.get("level")
        if level == "area":
            nodes, edges = self._area_screen(self._focus["area_id"])
        elif level == "object":
            nodes, edges = self._object_screen(self._focus["claim_id"])
        else:
            nodes, edges = self._estate_screen()
        return {"nodes": nodes, "edges": edges, "path": self._path(),
                "details": self._details(), "legend": self._legend()}

    def _legend(self) -> list[dict[str, str]]:
        """The shape/color key carried with this map's projection."""
        return [
            {"label": "Architecture area", "shape": "ellipse", "color": "#4a6fa5"},
            {"label": "Object — unevaluated", "shape": "round-rectangle", "color": "#7a8794"},
            {"label": "Object — supported", "shape": "round-rectangle", "color": "#3f9b5f"},
            {"label": "Object — unprovable", "shape": "round-rectangle", "color": "#c79a2b"},
            {"label": "Object — contradicted", "shape": "round-rectangle", "color": "#c0453b"},
            {"label": "Selected / first-order links", "shape": "line", "color": "var(--selected-edge, var(--accent))"},
            {"label": "Second-order links", "shape": "line", "color": "var(--progress-fill, var(--accent))"},
        ]

    def _estate_screen(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every area with its counts, and every area-to-area link with its count."""
        nodes = [{
            "id": f"{_AREA}{area['inferred_architecture_area_id']}", "kind": "area",
            "area_id": area["inferred_architecture_area_id"], "label": area["area_label"],
            "claim_count": area["claim_count"],
            "internal_relationship_count": area["internal_relationship_count"],
            "crossing_relationship_count": area["crossing_relationship_count"],
            "role": "" if area["claim_count"] else "empty",
        } for area in self._read.get("areas", [])]
        edges = [{
            "id": f"link:{link['from_area_id']}:{link['to_area_id']}", "kind": "link",
            "source": f"{_AREA}{link['from_area_id']}", "target": f"{_AREA}{link['to_area_id']}",
            "weight": link["relationship_count"],
            "direct": bool(link["direct_relationship_count"]),
        } for link in self._read.get("area_links", [])]
        return nodes, edges

    def _claim_node(self, claim: dict[str, Any], role: str = "") -> dict[str, Any]:
        return {
            "id": f"{_CLAIM}{claim['canonical_object_id']}", "kind": "claim",
            "claim_id": claim["canonical_object_id"],
            "label": claim.get("owner_qualified_name") or claim.get("object_key"),
            "outcome": claim.get("evaluation_outcome") or "unevaluated",
            "role": role,
        }

    @staticmethod
    def _relationship_edge(row: dict[str, Any], edge_id: str, source: str,
                           target: str) -> dict[str, Any]:
        return {"id": edge_id, "kind": "relationship", "source": source,
                "target": target, "direct": bool(row.get("is_direct"))}

    def _area_screen(self, area_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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

        nodes: dict[str, dict[str, Any]] = {}
        for claim_id in sorted(inside):
            if claim_id in claims:
                nodes[f"{_CLAIM}{claim_id}"] = self._claim_node(claims[claim_id])

        def outside(claim_id: int) -> list[str]:
            elsewhere = sorted(areas_of.get(claim_id, set()))
            if not elsewhere:
                if claim_id in claims:
                    nodes[f"{_CLAIM}{claim_id}"] = self._claim_node(claims[claim_id], "outside")
                return [f"{_CLAIM}{claim_id}"]
            for other in elsewhere:
                nodes[f"{_AREA}{other}"] = {"id": f"{_AREA}{other}", "kind": "area",
                                            "area_id": other, "label": labels.get(other, ""),
                                            "role": "elsewhere"}
            return [f"{_AREA}{other}" for other in elsewhere]

        edges: list[dict[str, Any]] = []
        for row in self._read.get("relationships", []):
            source_id, target_id = row["canonical_object_id"], row["linked_canonical_object_id"]
            edge_id = f"relationship:{source_id}:{target_id}"
            if source_id in inside and target_id in inside:
                edges.append(self._relationship_edge(
                    row, edge_id, f"{_CLAIM}{source_id}", f"{_CLAIM}{target_id}"))
            elif source_id in inside:
                for target in outside(target_id):
                    edges.append(self._relationship_edge(
                        row, f"{edge_id}:{target}", f"{_CLAIM}{source_id}", target))
            elif target_id in inside:
                for source in outside(source_id):
                    edges.append(self._relationship_edge(
                        row, f"{edge_id}:{source}", source, f"{_CLAIM}{target_id}"))
        return list(nodes.values()), edges

    def _object_screen(self, claim_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """One claim and the claims it is directly linked with, in both directions."""
        claims = {c["canonical_object_id"]: c for c in self._read.get("claims", [])}
        nodes: dict[str, dict[str, Any]] = {}
        if claim_id in claims:
            nodes[f"{_CLAIM}{claim_id}"] = self._claim_node(claims[claim_id], "focus")
        edges: list[dict[str, Any]] = []
        for row in self._read.get("relationships", []):
            ends = (row["canonical_object_id"], row["linked_canonical_object_id"])
            if claim_id not in ends:
                continue
            for end in ends:
                if end != claim_id and end in claims:
                    nodes[f"{_CLAIM}{end}"] = self._claim_node(claims[end])
            edges.append(self._relationship_edge(
                row, f"relationship:{ends[0]}:{ends[1]}", f"{_CLAIM}{ends[0]}", f"{_CLAIM}{ends[1]}"))
        return list(nodes.values()), edges

    def _path(self) -> list[dict[str, Any]]:
        """The path to the focused screen, Estate first; each step names its focus."""
        steps = [{"value": "estate", "label": "Estate", "focus": {"level": "estate"}}]
        area_id = self._focus.get("area_id")
        if area_id is not None:
            area = self._area(area_id) or {}
            steps.append({"value": f"area:{area_id}", "label": area.get("area_label", ""),
                          "focus": {"level": "area", "area_id": area_id}})
        if self._focus.get("level") == "object":
            claim = self._claim(self._focus["claim_id"]) or {}
            steps.append({"value": f"object:{self._focus['claim_id']}",
                          "label": claim.get("owner_qualified_name") or claim.get("object_key", ""),
                          "focus": dict(self._focus)})
        return steps

    def _details(self) -> dict[str, Any] | None:
        """The focused claim's details, as its own row states them."""
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

    # -- the schema projection -------------------------------------------------

    def _table_names(self) -> list[str]:
        return [str(t["identity"]["name"]) for t in self._read.get("tables", [])]

    def _holds_schema_node(self, node_id: str) -> bool:
        """Whether this schema holds the table, or references the external table, a node names."""
        if node_id.startswith(_TABLE):
            return node_id[len(_TABLE):] in self._table_names()
        if node_id.startswith(_EXTERNAL):
            return any(f"{_EXTERNAL}{key['referenced_schema']}.{key['referenced_table']}" == node_id
                       for table in self._read.get("tables", [])
                       for key in table.get("foreign_keys") or [])
        return False

    def _schema_drill(self, node_id: str) -> None:
        """Drilling into a table opens it to its columns, or closes it again."""
        if not node_id.startswith(_TABLE):
            return
        name = node_id[len(_TABLE):]
        opened = list(self._focus.get("opened") or [])
        self.focus(opened=[t for t in opened if t != name] if name in opened else [*opened, name])

    def _schema_projection(self) -> dict[str, Any]:
        """The schema's tables and their foreign keys; opened tables show their columns.

        A foreign key to a table outside this schema is drawn to that table as
        an external node.
        """
        schema = self.subject["schema"]
        tables = self._read.get("tables", [])
        here = set(self._table_names())
        opened = set(self._focus.get("opened") or [])
        nodes: dict[str, dict[str, Any]] = {}
        for table in tables:
            name = str(table["identity"]["name"])
            keyed = set((table.get("primary_key") or {}).get("columns") or [])
            nodes[f"{_TABLE}{name}"] = {
                "id": f"{_TABLE}{name}", "kind": "table", "table": name, "label": name,
                "columns": [
                    {"name": c["name"], "type": c["type"], "nullable": c["nullable"],
                     "primary_key": c["name"] in keyed}
                    for c in sorted(table.get("columns") or [], key=lambda c: c["ordinal"])
                ] if name in opened else [],
            }
        edges: list[dict[str, Any]] = []
        for table in tables:
            name = str(table["identity"]["name"])
            for index, key in enumerate(table.get("foreign_keys") or []):
                inside = key["referenced_schema"] == schema and key["referenced_table"] in here
                target = (f"{_TABLE}{key['referenced_table']}" if inside else
                          f"{_EXTERNAL}{key['referenced_schema']}.{key['referenced_table']}")
                if not inside and target not in nodes:
                    nodes[target] = {"id": target, "kind": "external",
                                     "label": f"{key['referenced_schema']}.{key['referenced_table']}"}
                edges.append({
                    "id": f"fk:{name}:{key['name']}:{index}", "kind": "foreign_key",
                    "source": f"{_TABLE}{name}", "target": target,
                    "columns": list(key["columns"]),
                    "referenced_columns": list(key["referenced_columns"]),
                })
        return {"nodes": list(nodes.values()), "edges": edges,
                "path": [{"value": "schema", "label": schema, "focus": dict(self._focus)}],
                "details": None}
