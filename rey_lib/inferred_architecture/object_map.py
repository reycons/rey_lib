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

from pathlib import Path
from typing import Any, Callable, Final

from rey_lib.db.database_objects import DatabaseObjectListing, DatabaseSchema
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files import read_text_file
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

_SQL_DIR: Final = Path(__file__).parent / "sql"

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

#: A claim's owner file is a configured file beneath the apps root: its
#: ``owner_subject_id`` is the path relative to it (#1218).
_CLAIM_FILE_KIND: Final = "configured_file"
_CLAIM_FILE_ROOT: Final = "{apps}"


def _statement(name: str) -> str:
    """One of the object map's statements, read from its file."""
    return read_text_file(_SQL_DIR / f"object_map_{name}.sql")


class ObjectMap:
    """One map subject, read, held live, focused and projected.

    It states no presentation (#1215): its governed subject's presentation is
    resolved by Projection.for_subject.
    """

    def __init__(self, subject: dict[str, Any], projection_kind: str,
                 read: dict[str, Any]) -> None:
        """Hold what was read. Use ``for_architecture`` or ``for_schema``."""
        self.subject = dict(subject)
        self.projection_kind = projection_kind
        self._read = read
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
        self._index_installation = ""
        self._database_provider = ""
        self._table_context: dict[str, Any] | None = None
        self._context_path: list[dict[str, Any]] = []
        self._edge_inspection: dict[str, Any] | None = None

    # -- opening ---------------------------------------------------------------

    @classmethod
    def for_architecture(cls, connection: Any, installation: str) -> "ObjectMap":
        """The estate's map, read on the shared ``connection``: its object reading
        and its flow reading (#1304), the object reading active.

        Args:
            connection: The shared Connection the host resolved; held by the
                host, never opened or closed here.
            installation: The installation the map subject names.
        """
        handle = connection.handle()
        read = {name: [dict(row) for row in DatabaseQuery(_statement(name)).rows(handle)]
                for name in _OBJECT_READS + _FLOW_READS}
        held = cls({"kind": "inferred_architecture", "installation": installation},
                   OBJECT_PROJECTION, read)
        held._index_connection = connection
        return held

    @classmethod
    def for_schema(cls, connection: Any, catalog: str, schema: str, *,
                   index_connection: Any = None, installation: str = "") -> "ObjectMap":
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
        held = cls({"kind": "database_schema", "connection": connection.name,
                    "catalog": catalog, "schema": schema}, SCHEMA_PROJECTION, read)
        held._index_connection = index_connection
        held._index_installation = installation
        held._database_provider = str(getattr(connection, "provider", "") or "")
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
            opened = [str(name) for name in focus.get("opened") or []]
            unknown = sorted(set(opened) - set(self._table_names()))
            if unknown:
                raise ConfigError(f"This schema holds no table {unknown}.")
            table = str(focus.get("table") or "")
            node = str(focus.get("node") or "")
            step = None
            if table:
                if table not in self._table_names():
                    raise AppError(f"This schema holds no table {table!r}.")
                context = self._read_table_context(table)
                step = {"value": f"table:{table}", "label": table, "focus": {"table": table}}
            elif node:
                known = next((n for n, _ in self._schema_scope()[0] if n["id"] == node), None)
                previous = next((s for s in self._context_path if s["value"] == node), None)
                if known is None and previous is None:
                    raise AppError(f"This map holds no indexed node {node!r}.")
                label = (known or previous or {}).get("label", node)
                if node.startswith(_EXTERNAL):
                    target = next((key for t in self._read.get("tables", [])
                                   for key in t.get("foreign_keys") or []
                                   if f"{_EXTERNAL}{key['referenced_schema']}.{key['referenced_table']}" == node), None)
                    if target is None:
                        raise AppError(f"This map holds no external table {node!r}.")
                    context = self._read_table_context(target["referenced_table"],
                                                       schema=target["referenced_schema"], alias=node)
                else:
                    context = self._read_table_context(node=node)
                step = {"value": node, "label": label, "focus": {"node": node}}
            if step is not None:
                # Keep only the visited path, not another object or transport registry.
                at = next((i for i, s in enumerate(self._context_path)
                           if s["value"] == step["value"]), len(self._context_path))
                self._context_path = self._context_path[:at] + [step]
                self._table_context = context
            else:
                self._context_path = []
            self._focus = {"opened": opened, **({"table": table} if table else {}),
                           **({"node": node} if node else {})}
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
        if not key:
            if application and not any(f["entry_repository_key"] == application
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
            # Read each proved implementation through the existing DatabaseQuery owner.
            # A leaf row still carries its indexed source metadata.
            if self._index_connection is not None and source_ids:
                steps = []
                for current_id in source_ids:
                    rows = [dict(row) for row in DatabaseQuery(_statement("flow_steps"),
                            {"symbol_id": current_id}).rows(self._index_connection.handle())]
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
        if not self._holds[self.projection_kind](node_id) and not any(e["id"] == node_id for e, _ in self._scope()[1]):
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
        node_id = str(node_id or "")
        if self.projection_kind in (SCHEMA_PROJECTION, FLOW_PROJECTION) and any(
                edge["id"] == node_id for edge, _ in self._scope()[1]):
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
            identity = dict(element.get("identity") or (entry or {}).get("identity") or {})
            if element["kind"] == "external":
                key = next(k for t in self._read.get("tables", []) for k in t.get("foreign_keys") or []
                           if f"{_EXTERNAL}{k['referenced_schema']}.{k['referenced_table']}" == node_id)
                identity = {"schema": key["referenced_schema"], "name": key["referenced_table"],
                            "object_type": "table"}
            if not identity:
                raise AppError("This map node carries no declared opening identity.")
            if identity.get("repository_key") and identity.get("relative_path"):
                return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                                     "relative_path": f"{identity['repository_key']}/{identity['relative_path']}"}}
            kind = str(identity.get("object_type") or element["kind"])
            owner = DatabaseObjectListing.object_type(f"{kind}s")
            return {"identity": {"kind": "database_object", "subject_kind": owner.KIND,
                                 "connection": self.subject["connection"], "catalog": self.subject["catalog"],
                                 "schema": identity["schema"], "name": identity["name"],
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
        node_id = str(node_id or "")
        if self.projection_kind in (SCHEMA_PROJECTION, FLOW_PROJECTION) and any(
                edge["id"] == node_id for edge, _ in self._scope()[1]):
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
        if row is None or self._index_connection is None:
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
        statement = _statement("edge_evidence")
        params = {}
        for name, values in (("sources", sources), ("targets", targets)):
            bindings = {f"{name}_{i}": value for i, value in enumerate(values)}
            statement = statement.replace(f":{name}", ", ".join(f":{key}" for key in bindings))
            params.update(bindings)
        return [dict(r) for r in DatabaseQuery(statement, params).rows(self._index_connection.handle())]

    def projection(self) -> dict[str, Any]:
        """The focused screen: nodes, edges, the path back, the details, and the
        selected node with the connections it highlights -- beside its subject
        identity (``subject``) and which screen it is (``projection``)."""
        screen = self._project[self.projection_kind]()
        selected = self._marked(screen["nodes"], screen["edges"])
        return {
            "subject": dict(self.subject),
            "projection": self.projection_kind,
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
        element = next((n for n, _ in self._flow_scope()[0] if n["id"] == node_id), None)
        if element is None:
            raise AppError(f"The map has no node {node_id!r}.")
        if element.get("relative_path") and element.get("repository_key"):
            return {"identity": {"kind": _CLAIM_FILE_KIND, "root": _CLAIM_FILE_ROOT,
                                 "relative_path": f"{element['repository_key']}/"
                                                  f"{element['relative_path']}"}}
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
                selected = "request:" + bindings[0]["request_name"] if request else None
                values = next((r.get("commands") for r in candidates if r.get("commands")), [])
                if request and any(v.startswith("request:") for v in values):
                    branch = "true" if selected in values else "false"
                    return [r["to_key"] for r in candidates if r["transition_kind"] == branch]
                return [r["to_key"] for r in candidates]
            pending = [r["to_key"] for r in outgoing.get(guard, [])
                       if r["transition_kind"] == "true" or root.get("kind") == "entry"]
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
                nodes[identity] = ({**self._flow_node(row),
                                    **({"operation_root": True} if node_key == guard else {})}, row)
                if row.get("to_key") in reached and row["to_key"] in continuations(node_key) and (node_key != guard or row["transition_kind"] == "true" or root.get("kind") == "entry"):
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
        return self._flow_activities(nodes, edges, title=title)

    @staticmethod
    def _flow_activities(nodes: list[_Mapped], edges: list[_Mapped], *,
                         title: str | None = None) -> tuple[list[_Mapped], list[_Mapped]]:
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
            adjacency: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
            incoming: dict[str, list[str]] = {}
            failure_edges = []
            for edge, evidence in edges:
                if edge["source"] not in scope or edge["target"] not in scope:
                    continue
                if edge["label"] == "exception":
                    failure_edges.append(evidence)
                    if scope[edge["source"]][0]["kind"] != "raise":
                        continue
                adjacency.setdefault(edge["source"], []).append((edge, evidence))
                incoming.setdefault(edge["target"], []).append(edge["source"])
            exits = {key for key, (node, _) in scope.items() if node["kind"] in ("exit", "exception_exit")}
            successful = set(exits)
            pending = list(exits)
            while pending:
                for before in incoming.get(pending.pop(), []):
                    if before not in successful:
                        successful.add(before)
                        pending.append(before)
            roots = [key for key, (node, _) in scope.items()
                     if node["kind"] == "entry" or node.get("operation_root")]
            reached = set(roots)
            pending = list(roots)
            while pending:
                for edge, _ in adjacency.get(pending.pop(), []):
                    if edge["target"] not in reached:
                        reached.add(edge["target"])
                        pending.append(edge["target"])
            successful.intersection_update(reached)
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
                                     else "Return to caller"}, row)
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
                            label = branch if branch in ("true", "false", "iterate", "done") else ""
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

    def _flow_projection(self) -> dict[str, Any]:
        """The flows screen, or the focused flow's chart, with what each draws."""
        elements, relations = self._flow_scope()
        nodes = [dict(element) for element, _ in elements]
        edges = [{**relation, "weight": 1}
                 for relation, row in relations]
        coverage = [row for _, row in elements if row and row.get("coverage_status")]
        partial = [row for row in coverage if row["coverage_status"] == "partial"]
        return {"nodes": nodes, "edges": edges, "path": self._path(),
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
        return [str(t["identity"]["name"]) for t in self._read.get("tables", [])]

    def _holds_schema_node(self, node_id: str) -> bool:
        """Whether this schema holds the table, or references the external table, a node names."""
        if (self._focus.get("table") or self._focus.get("node")) and self._table_context:
            return any(node["id"] == node_id for node in self._table_context["nodes"])
        if node_id.startswith(_TABLE):
            return node_id[len(_TABLE):] in self._table_names()
        if node_id.startswith(_EXTERNAL):
            return any(f"{_EXTERNAL}{key['referenced_schema']}.{key['referenced_table']}" == node_id
                       for table in self._read.get("tables", [])
                       for key in table.get("foreign_keys") or [])
        return False

    def _schema_drill(self, node_id: str) -> None:
        """Focus this object's indexed neighbourhood using the existing context read."""
        target = {"table": node_id[len(_TABLE):]} if node_id.startswith(_TABLE) else {"node": node_id}
        self.focus(**target, opened=self._focus.get("opened", []))

    def _read_table_context(self, table: str | None = None, *, node: str | None = None,
                            schema: str | None = None, alias: str | None = None) -> dict[str, Any]:
        """The existing index's bounded graph, on the host's shared index connection."""
        if self._index_connection is None or not self._index_installation:
            raise AppError("This map has no configured index connection/installation for table context.")
        rows = list(DatabaseQuery(_statement("table_context"), {
            "catalog": self.subject["catalog"], "provider": self._database_provider,
            "schema": schema or self.subject["schema"], "table": table, "node": node,
            "installation": self._index_installation,
        }).rows(self._index_connection.handle()))
        if len(rows) != 1 or rows[0]["root_count"] != 1:
            raise AppError("The index does not identify exactly one table/object for this context.")
        read = dict(rows[0])
        root = read["root_id"]
        root_id = alias or node or f"{_TABLE}{table}"
        nodes = [dict(node) for node in read.get("nodes") or []]
        for node in nodes:
            if (node.get("identity") or {}).get("object_type"):
                node["label"] = IndexedDatabaseObject(**node["identity"]).label
            elif (node.get("identity") or {}).get("relative_path"):
                node["role"] = "file"
            if node["id"] == root:
                node.update(id=root_id)
                if table is not None and alias is None:
                    node["table"] = table
        edges = [{**edge,
                  "source": root_id if edge["source"] == root else edge["source"],
                  "target": root_id if edge["target"] == root else edge["target"]}
                 for edge in read.get("edges") or []]
        return {"nodes": nodes, "edges": edges}

    def _schema_scope(self) -> tuple[list[_Mapped], list[_Mapped]]:
        """The schema's tables, the external tables its foreign keys reach, and those keys.

        A foreign key to a table outside this schema is related to that table as
        an external element. Each table and key is beside the row it maps.
        """
        if (self._focus.get("table") or self._focus.get("node")) and self._table_context:
            tables = {str(t["identity"]["name"]): t for t in self._read.get("tables", [])}
            return ([(dict(n), tables.get(n.get("table", ""))) for n in self._table_context["nodes"]],
                    [(dict(e), None) for e in self._table_context["edges"]])
        schema = self.subject["schema"]
        tables = self._read.get("tables", [])
        here = set(self._table_names())
        elements: dict[str, _Mapped] = {}
        for table in tables:
            name = str(table["identity"]["name"])
            elements[f"{_TABLE}{name}"] = ({"id": f"{_TABLE}{name}", "kind": "table",
                                            "table": name, "label": name}, table)
        relations: list[_Mapped] = []
        for table in tables:
            name = str(table["identity"]["name"])
            for index, key in enumerate(table.get("foreign_keys") or []):
                inside = key["referenced_schema"] == schema and key["referenced_table"] in here
                target = (f"{_TABLE}{key['referenced_table']}" if inside else
                          f"{_EXTERNAL}{key['referenced_schema']}.{key['referenced_table']}")
                if not inside and target not in elements:
                    elements[target] = ({"id": target, "kind": "external",
                                         "label": f"{key['referenced_schema']}."
                                                  f"{key['referenced_table']}"}, None)
                relations.append(({
                    "id": f"fk:{name}:{key['name']}:{index}", "kind": "foreign_key",
                    "source": f"{_TABLE}{name}", "target": target,
                }, key))
        return list(elements.values()), relations

    def _schema_projection(self) -> dict[str, Any]:
        """The schema's tables and their foreign keys; opened tables show their columns.

        A foreign key to a table outside this schema is drawn to that table as
        an external node.
        """
        opened = set(self._focus.get("opened") or [])
        elements, relations = self._schema_scope()
        nodes = []
        for element, table in elements:
            if table is None:
                nodes.append(element)
                continue
            keyed = set((table.get("primary_key") or {}).get("columns") or [])
            nodes.append({**element, "columns": [
                {"name": c["name"], "type": c["type"], "nullable": c["nullable"],
                 "primary_key": c["name"] in keyed}
                for c in sorted(table.get("columns") or [], key=lambda c: c["ordinal"])
            ] if element["table"] in opened else []})
        edges = [{**relation, **({"columns": list(key["columns"]),
                  "referenced_columns": list(key["referenced_columns"])} if key else {})}
                 for relation, key in relations]
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
                rows.extend([["Constraint", key.get("name", "")],
                             ["Columns", ", ".join(key.get("columns", []))],
                             ["Referenced columns", ", ".join(key.get("referenced_columns", []))]])
            details = {"rows": rows}
        return {"nodes": nodes, "edges": edges, "path": self._path(), "details": details}
