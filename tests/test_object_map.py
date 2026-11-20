"""The ObjectMap: one live map object, two projections (#1119, #1120).

No database: the five object-map reads and the schema read are stand-ins. What
is proved is the object -- what it reads, how a click or double-click changes it, what each
focused screen projects, and that opening a claim answers the governed subject
and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.inferred_architecture import object_map as module
from rey_lib.inferred_architecture import (
    CLAIM_NODE_TYPE,
    FLOW_PROJECTION,
    OBJECT_PROJECTION,
    SCHEMA_PROJECTION,
    ObjectMap,
)
from tests.schema_objects import schema_objects


def _claim(claim_id: int, name: str, outcome: str | None = "supported") -> dict[str, Any]:
    return {"canonical_object_id": claim_id, "object_key": f"rey_lib:{name}",
            "owner_qualified_name": name, "owner_subject_id": f"rey_lib/{name}.py",
            "evaluation_outcome": outcome, "responsibility": f"{name} things"}


READ: dict[str, Any] = {
    "claims": [_claim(1, "Alpha"), _claim(2, "Beta"), _claim(3, "Gamma", None), _claim(4, "Delta")],
    # 1 -> 2 inside area 10; 1 -> 3 leaves for area 20; 4 is placed nowhere.
    "relationships": [
        {"canonical_object_id": 1, "linked_canonical_object_id": 2, "is_direct": True},
        {"canonical_object_id": 1, "linked_canonical_object_id": 3, "is_direct": False},
        {"canonical_object_id": 4, "linked_canonical_object_id": 2, "is_direct": True},
    ],
    "placements": [
        {"canonical_object_id": 1, "inferred_architecture_area_id": 10},
        {"canonical_object_id": 2, "inferred_architecture_area_id": 10},
        {"canonical_object_id": 3, "inferred_architecture_area_id": 20},
    ],
    "areas": [
        {"inferred_architecture_area_id": 10, "area_label": "Core", "claim_count": 2,
         "internal_relationship_count": 1, "crossing_relationship_count": 1},
        {"inferred_architecture_area_id": 20, "area_label": "Edge", "claim_count": 1,
         "internal_relationship_count": 0, "crossing_relationship_count": 1},
        {"inferred_architecture_area_id": 30, "area_label": "Empty", "claim_count": 0,
         "internal_relationship_count": 0, "crossing_relationship_count": 0},
    ],
    "area_links": [
        {"from_area_id": 10, "to_area_id": 20, "relationship_count": 1,
         "direct_relationship_count": 0},
    ],
}


def _architecture() -> ObjectMap:
    return ObjectMap({"kind": "inferred_architecture", "installation": "admin"},
                     OBJECT_PROJECTION, {name: [dict(r) for r in rows] for name, rows in READ.items()})


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


# -- reading ------------------------------------------------------------------

def test_the_architecture_map_reads_its_five_reads_through_the_shared_control() -> None:
    """#1360: the five reads are Control's governed get_object_map_* reads, in order."""
    ran: list[str] = []

    class _Control:
        def __getattr__(self, name: str) -> Callable[[], list[dict[str, Any]]]:
            if not name.startswith("object_map_"):
                raise AttributeError(name)
            read = name.removeprefix("object_map_")
            return lambda: ran.append(read) or [dict(row) for row in READ[read]]

    connection = SimpleNamespace(handle=lambda: "handle")

    held = ObjectMap.for_architecture(connection, "admin", _Control())

    assert held.subject == {"kind": "inferred_architecture", "installation": "admin"}
    assert held.projection_kind == OBJECT_PROJECTION
    assert ran == ["claims", "relationships", "placements", "areas", "area_links"]
    assert held._read == {name: [dict(r) for r in rows] for name, rows in READ.items()}


def test_the_five_reads_are_the_governed_bindings() -> None:
    """Control.object_map_<name> calls get_object_map_<name>, required, no inputs (#1360)."""
    from unittest.mock import patch

    from rey_lib.control.control import Control

    for name in ("claims", "relationships", "placements", "areas", "area_links"):
        rows = [dict(row) for row in READ[name]]
        with patch.object(Control, "_call_rows", return_value=rows) as called:
            answered = getattr(Control.__new__(Control), f"object_map_{name}")()
        called.assert_called_once_with(f"get_object_map_{name}", {}, required=True)
        assert answered == rows and (not rows or answered[0] is not rows[0])


def test_a_schema_map_reads_the_schema_as_the_database_domains_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[Any, ...]] = []
    read_schema = module.DatabaseSchema(connection="rey apps", catalog="rey_apps", schema="control")

    def read(handle: Any, connection: str, catalog: str, schema: str) -> Any:
        asked.append((handle, connection, catalog, schema))
        return read_schema

    monkeypatch.setattr(module.DatabaseSchema, "read", read)
    connection = SimpleNamespace(handle=lambda: "handle", name="rey apps")

    held = ObjectMap.for_schema(connection, "rey_apps", "control")

    assert asked == [("handle", "rey apps", "rey_apps", "control")]
    # Held as the domain's object (#1441), never its serialization.
    assert held._schema is read_schema
    assert held.subject == {"kind": "database_schema", "connection": "rey apps",
                            "catalog": "rey_apps", "schema": "control"}
    assert held.projection_kind == SCHEMA_PROJECTION


# -- the object projection -----------------------------------------------------

def test_the_map_opens_on_the_estate() -> None:
    projected = _architecture().projection()

    assert projected["focus"] == {"level": "estate"}
    assert _ids(projected["nodes"]) == ["area:10", "area:20", "area:30"]
    assert [n["role"] for n in projected["nodes"]] == ["", "", "empty"]
    assert projected["edges"] == [{"id": "link:10:20", "kind": "link", "source": "area:10",
                                   "target": "area:20", "weight": 1, "direct": False,
                                   "highlight": []}]
    assert projected["selected"] is None
    assert projected["path"] == [{"value": "estate", "label": "Estate",
                                  "focus": {"level": "estate"}}]
    assert projected["details"] is None


def test_object_path_retains_the_visited_objects_and_can_retrace_to_root() -> None:
    held = _architecture()
    held.double_click("area:10")
    held.double_click("claim:1")
    alpha = held.projection()["path"][-1]["focus"]
    held.double_click("claim:2")
    assert [s["value"] for s in held.projection()["path"]] == [
        "estate", "area:10", "object:1", "object:2"]
    held.focus(**alpha)
    assert [s["value"] for s in held.projection()["path"]] == ["estate", "area:10", "object:1"]
    held.focus(**held.projection()["path"][0]["focus"])
    assert [s["value"] for s in held.projection()["path"]] == ["estate"]


def test_double_clicking_an_area_drills_into_its_claims_and_their_links() -> None:
    held = _architecture()

    assert held.double_click("area:10") is None
    projected = held.projection()

    assert projected["focus"] == {"level": "area", "area_id": 10}
    nodes = {n["id"]: n for n in projected["nodes"]}
    # Inside: 1 and 2. 1 -> 3 leaves to area 20, drawn as that area; 4 is placed
    # nowhere, so it is drawn as itself, outside.
    assert set(nodes) == {"claim:1", "claim:2", "area:20", "claim:4"}
    assert nodes["area:20"]["role"] == "elsewhere"
    assert nodes["claim:4"]["role"] == "outside"
    assert _ids(projected["edges"]) == [
        "relationship:1:2", "relationship:1:3:area:20", "relationship:4:2:claim:4"]
    assert [step["value"] for step in projected["path"]] == ["estate", "area:10"]


def test_double_clicking_a_claim_drills_into_it_with_its_neighbours_and_details() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.double_click("claim:1") is None
    projected = held.projection()

    assert projected["focus"] == {"level": "object", "claim_id": 1, "area_id": 10}
    roles = {n["id"]: n["role"] for n in projected["nodes"]}
    assert roles == {"claim:1": "focus", "claim:2": "", "claim:3": ""}
    assert [step["value"] for step in projected["path"]] == ["estate", "area:10", "object:1"]
    assert ["Object", "rey_lib:Alpha"] in projected["details"]["rows"]


# -- interaction (#1099): click selects, double-click drills, right-click opens a claim --

def test_a_first_click_selects_and_the_projection_highlights_its_connections() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.click("claim:1") is None
    projected = held.projection()

    assert projected["selected"] == "claim:1"
    marks = {n["id"]: n["highlight"] for n in projected["nodes"]}
    # 1 links to 2 and (through area 20) out; 4 links to 2 -- the second ring.
    assert marks == {"claim:1": ["focus", "first"], "claim:2": ["first"],
                     "area:20": ["first"], "claim:4": ["second"]}
    edges = {e["id"]: e["highlight"] for e in projected["edges"]}
    assert edges == {"relationship:1:2": ["first"], "relationship:1:3:area:20": ["first"],
                     "relationship:4:2:claim:4": ["second"]}


@pytest.mark.parametrize("drill", [[], ["area:10"], ["area:10", "claim:1"]],
                         ids=["from the estate", "from an area", "from the claim itself"])
def test_a_right_click_on_a_claim_identifies_the_same_file_at_any_focus(
    drill: list[str],
) -> None:
    held = _architecture()
    for node in drill:
        held.double_click(node)

    # The File the claim shows (#1218): its owner file beneath the apps root.
    assert held.context_click("claim:1") == {"identity": {
        "kind": "configured_file", "root": "{apps}", "relative_path": "rey_lib/Alpha.py"}}


def test_a_right_click_on_a_held_non_claim_changes_nothing() -> None:
    held = _architecture()
    before = held.projection()

    assert held.context_click("area:10") is None
    assert held.projection() == before


def test_a_click_never_opens_however_often_it_repeats() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.click("claim:1") is None
    assert held.click("claim:1") is None
    assert held.projection()["selected"] == "claim:1"


def test_a_click_on_another_node_moves_the_selection() -> None:
    held = _architecture()
    held.double_click("area:10")
    held.click("claim:1")

    held.click("claim:2")

    assert held.projection()["selected"] == "claim:2"


def test_a_click_on_no_node_clears_the_selection() -> None:
    held = _architecture()
    held.click("area:10")

    assert held.click(None) is None
    assert held.projection()["selected"] is None
    assert held.click("area:10") is None  # selected again, not opened


def test_a_double_click_drills_clears_the_selection_and_never_opens() -> None:
    held = _architecture()
    held.click("claim:1")

    assert held.double_click("claim:1") is None
    assert held.projection()["focus"]["level"] == "object"
    assert held.projection()["selected"] is None
    assert held.double_click("claim:1") is None  # a focused claim is never opened by drilling


@pytest.mark.parametrize("node", ["claim:99", "area:99", "claim:x", "table:run"])
def test_a_click_or_double_click_on_a_node_the_map_does_not_hold_is_refused(node: str) -> None:
    held = _architecture()

    with pytest.raises(ConfigError, match="no node"):
        held.click(node)
    with pytest.raises(ConfigError, match="no node"):
        held.double_click(node)
    with pytest.raises(ConfigError, match="no node"):
        held.context_click(node)
    assert held.projection()["selected"] is None


def test_an_opened_claim_names_only_the_file_never_its_host_or_panel() -> None:
    opened = _architecture().context_click("claim:1")

    assert set(opened) == {"identity"}
    assert not {"application", "operation", "application_command_id", "panel",
                "declaration"} & set(opened["identity"])


def test_a_claim_naming_no_owner_file_is_refused() -> None:
    held = _architecture()
    held._read["claims"][0]["owner_subject_id"] = ""  # noqa: SLF001

    with pytest.raises(ConfigError, match="no owner file"):
        held.context_click("claim:1")


def test_the_map_reads_no_tree_declarations() -> None:
    # A File opening is the hosting application's, not a Tree placement's (#1218).
    assert not {"interacted_declaration", "declared_fact", "NodeTypeActionDataObject"} & set(
        vars(module))


def test_the_map_has_no_separate_record_lookup() -> None:
    assert not hasattr(_architecture(), "record")


def test_the_path_moves_the_focus_back() -> None:
    held = _architecture()
    held.double_click("area:10")
    held.double_click("claim:1")

    held.focus(level="estate")

    assert held.projection()["focus"] == {"level": "estate"}


@pytest.mark.parametrize("focus", [
    {"level": "area", "area_id": 99}, {"level": "object", "claim_id": 99}, {"level": "x"},
])
def test_a_focus_on_nothing_the_map_holds_is_refused(focus: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        _architecture().focus(**focus)


# -- the schema projection -------------------------------------------------------

SCHEMA_READ = {"tables": [
    {"identity": {"schema": "control", "name": "run"},
     "columns": [{"name": "run_id", "type": "bigint", "nullable": False, "ordinal": 1},
                 {"name": "note", "type": "text", "nullable": True, "ordinal": 2}],
     "primary_key": {"name": "run_pk", "columns": ["run_id"]},
     "foreign_keys": []},
    {"identity": {"schema": "control", "name": "run_log"},
     "columns": [{"name": "run_id", "type": "bigint", "nullable": False, "ordinal": 1}],
     "primary_key": None,
     "foreign_keys": [
         {"name": "run_log_run_fk", "columns": ["run_id"], "referenced_schema": "control",
          "referenced_table": "run", "referenced_columns": ["run_id"]},
         {"name": "run_log_installation_fk", "columns": ["installation_id"],
          "referenced_schema": "code", "referenced_table": "installation",
          "referenced_columns": ["installation_id"]},
     ]},
]}


def _schema() -> ObjectMap:
    return ObjectMap({"kind": "database_schema", "connection": "rey apps",
                      "catalog": "rey_apps", "schema": "control"},
                     SCHEMA_PROJECTION,
                     schema_objects(SCHEMA_READ, connection="rey apps", catalog="rey_apps",
                                    schema="control"))


def test_a_schema_projects_its_tables_and_foreign_keys_closed() -> None:
    projected = _schema().projection()

    assert projected["projection"] == SCHEMA_PROJECTION
    assert _ids(projected["nodes"]) == ["table:run", "table:run_log", "external:code.installation"]
    assert all(not n.get("columns") for n in projected["nodes"])
    assert [(e["source"], e["target"]) for e in projected["edges"]] == [
        ("table:run_log", "table:run"), ("table:run_log", "external:code.installation")]


def test_schema_focus_opens_a_table_to_its_columns_and_closes_it() -> None:
    held = _schema()

    held.focus(opened=["run"])
    run = next(n for n in held.projection()["nodes"] if n["id"] == "table:run")
    assert run["columns"] == [
        {"name": "run_id", "type": "bigint", "nullable": False, "primary_key": True},
        {"name": "note", "type": "text", "nullable": True, "primary_key": False}]

    held.focus(opened=[])
    assert held.projection()["focus"] == {"opened": []}


def test_a_schema_map_has_no_claims_to_open() -> None:
    with pytest.raises(ConfigError, match="no node"):
        _schema().context_click("claim:1")


def test_trigger_opening_keeps_the_parent_from_its_indexed_relation() -> None:
    from rey_lib.db.database_objects import DatabaseObjectListing

    held = _schema()
    held._focus = {"node": "db:trigger"}
    held._table_context = {"nodes": [
        {"id": "db:trigger", "kind": "code", "identity": {
            "schema": "code", "name": "refuse_change", "object_type": "trigger", "signature": ""}},
        {"id": "db:parent", "kind": "table", "identity": {
            "schema": "code", "name": "generation", "object_type": "table", "signature": ""}},
    ], "edges": [{"id": "reference:owns", "source": "db:trigger", "target": "db:parent",
                  "kind": "trigger"}]}
    opened = held.context_click("db:trigger")["identity"]
    assert opened == {"kind": "database_object", "subject_kind": "triggers",
                      "connection": "rey apps", "catalog": "rey_apps", "schema": "code",
                      "name": "refuse_change", "table": "generation"}
    owner = DatabaseObjectListing.object_type(opened["subject_kind"])
    identified = owner.identify(**{k: v for k, v in opened.items() if k not in ("kind", "subject_kind")})
    assert identified.table.name == "generation"
    assert identified.table.schema == "code"
    held._table_context["edges"] = []
    with pytest.raises(module.AppError, match="parent table"):
        held.context_click("db:trigger")


@pytest.mark.parametrize("held, screen", [(_architecture, "object"), (_schema, "schema")])
def test_a_map_states_no_presentation_and_carries_its_own_subject_and_screen(
    held: Callable[[], ObjectMap], screen: str,
) -> None:
    """#1215: presentation is the governed subject's; the map's screen is its own state."""
    held_map = held()
    projected = held_map.projection()

    assert "panel_type" not in projected
    assert not hasattr(ObjectMap, "PANEL_TYPE")
    assert projected["projection"] == screen
    assert projected["subject"] == held_map.subject


def test_a_schema_map_selects_a_table_and_never_opens() -> None:
    held = _schema()

    assert held.click("table:run") is None
    projected = held.projection()
    assert projected["selected"] == "table:run"
    assert {n["id"]: n["highlight"] for n in projected["nodes"]}["table:run_log"] == ["first"]


def test_object_map_projects_its_legend_at_every_focus():
    obj = _architecture()
    legend = obj.projection()["legend"]
    assert {entry["label"] for entry in legend} >= {
        "Architecture area", "Object — supported", "Second-order links",
    }
    obj.double_click("area:10")
    assert obj.projection()["legend"] == legend
    obj.double_click("claim:1")
    assert obj.projection()["legend"] == legend


# -- inspect (#1199 S4): the live map's own semantic state, for the focused scope --

#: What inspect may never carry: presentation, or the mapped things' own figures.
_NOT_MAP_STATE = {"panel_type", "legend", "highlight", "details", "nodes", "edges",
                  "projection", "outcome", "claim_count", "internal_relationship_count",
                  "crossing_relationship_count", "weight", "direct", "columns",
                  "referenced_columns"}


def _keys(value: Any) -> set[str]:
    """Every key anywhere inside ``value``."""
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(one) for one in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(one) for one in value)) if value else set()
    return set()


def test_inspect_states_the_map_its_focus_and_the_estate_as_map_identities() -> None:
    assert _architecture().inspect() == {
        "subject": {"kind": "inferred_architecture", "installation": "admin"},
        "map": OBJECT_PROJECTION,
        "focus": {"level": "estate"},
        "path": [{"value": "estate", "label": "Estate"}],
        "selected": None,
        "elements": [
            {"id": "area:10", "kind": "area", "area_id": 10, "label": "Core", "role": ""},
            {"id": "area:20", "kind": "area", "area_id": 20, "label": "Edge", "role": ""},
            {"id": "area:30", "kind": "area", "area_id": 30, "label": "Empty", "role": "empty"},
        ],
        "relations": [{"id": "link:10:20", "kind": "link", "source": "area:10",
                       "target": "area:20"}],
    }


def test_inspect_follows_the_live_focus_and_carries_no_presentation_or_source_figures() -> None:
    held = _architecture()
    held.double_click("area:10")
    area = held.inspect()
    held.double_click("claim:1")
    claim = held.inspect()

    assert area["focus"] == {"level": "area", "area_id": 10}
    assert {e["id"]: e["role"] for e in area["elements"]} == {
        "claim:1": "", "claim:2": "", "area:20": "elsewhere", "claim:4": "outside"}
    assert [r["id"] for r in area["relations"]] == [
        "relationship:1:2", "relationship:1:3:area:20", "relationship:4:2:claim:4"]
    assert claim["path"] == [{"value": "estate", "label": "Estate"},
                             {"value": "area:10", "label": "Core"},
                             {"value": "object:1", "label": "Alpha"}]
    assert {e["id"]: e["role"] for e in claim["elements"]} == {
        "claim:1": "focus", "claim:2": "", "claim:3": ""}
    assert not (_keys(area) | _keys(claim)) & _NOT_MAP_STATE


def test_inspect_reports_the_selection_under_the_maps_own_rule() -> None:
    held = _architecture()
    held.click("area:10")
    assert held.inspect()["selected"] == "area:10"

    # A drill clears the selection; the next inspection says so.
    held.double_click("area:10")
    assert held.inspect()["selected"] is None


def test_inspect_moves_nothing() -> None:
    held = _architecture()
    held.click("area:10")
    before = held.projection()

    held.inspect()

    assert held.projection() == before


def test_inspect_of_a_schema_relates_its_tables_and_never_carries_their_columns() -> None:
    held = _schema()
    held.focus(opened=["run"])
    inspected = held.inspect()

    assert inspected["map"] == SCHEMA_PROJECTION
    assert inspected["focus"] == {"opened": ["run"]}
    assert inspected["path"] == [{"value": "schema", "label": "control"}]
    assert inspected["elements"] == [
        {"id": "table:run", "kind": "table", "table": "run", "label": "run"},
        {"id": "table:run_log", "kind": "table", "table": "run_log", "label": "run_log"},
        {"id": "external:code.installation", "kind": "external", "label": "code.installation"},
    ]
    assert [(r["source"], r["target"]) for r in inspected["relations"]] == [
        ("table:run_log", "table:run"), ("table:run_log", "external:code.installation")]
    assert not _keys(inspected) & _NOT_MAP_STATE


# -- the flow reading (#1304): one object, two readings; a flow is read from the index --

def _flow_rows(symbol_id=100):
    base = {"code_symbol_id": symbol_id, "repository_key": "example",
            "relative_path": "example/main.py", "qualified_name": "run",
            "dotted_identity": "example.main.run", "source_line": 10, "source_column": 4,
            "end_line": 10, "end_column": 20, "coverage_status": "partial",
            "coverage_reason": "context-manager exit/suppression",
            "target_code_symbol_id": None, "target_identity": None,
            "resolution": None, "is_resolved": None, "code_edge_id": None,
            "parameter_names": ["arg"], "symbol_name":"run", "symbol_kind":"function",
            "callee_reference":"next_task", "target_symbol_kind":"function",
            "target_symbol_name":"next_task", "target_label":"Perform next task."}
    return [
        {**base,"node_key":"entry","kind":"entry","text":"run","to_key":"test","transition_kind":"next"},
        {**base,"node_key":"test","kind":"condition","text":"flag","end_column":8,"to_key":"call","transition_kind":"true"},
        {**base,"node_key":"test","kind":"condition","text":"flag","end_column":8,"to_key":"exit","transition_kind":"false"},
        {**base,"node_key":"call","kind":"call","text":"next_task(arg)","to_key":"exit","transition_kind":"next",
         "target_code_symbol_id":200,"target_identity":"other.next_task","resolution":"semantic_binding","code_edge_id":42},
        {**base,"node_key":"exit","kind":"exit","text":"end","to_key":None,"transition_kind":None},
    ]


def _with_flows():
    read={**READ,"flows":[{"flow_key":"example","label":"main","entry_repository_key":"example",
                          "entry_code_symbol_id":100,"entry_identity":"example.main.run"},
                         {"flow_key":"missing","label":"main.py","entry_repository_key":"missing",
                          "entry_code_symbol_id":None,"entry_identity":"main.py"}],
          "flow_steps":_flow_rows()}
    return ObjectMap({"kind":"inferred_architecture","installation":"admin"},OBJECT_PROJECTION,read)


def test_application_without_input_evidence_never_falls_back_to_whole_main():
    held=_with_flows();held.focus(reading="flow")
    assert [n["id"] for n in held.projection()["nodes"]]==["application:example","application:missing"]
    held.double_click("application:example")
    projection = held.projection()
    assert projection["nodes"][0]["kind"] == "boundary"
    assert "CLI commands or requests" in projection["nodes"][0]["label"]
    assert projection["edges"] == []


def test_application_overview_lists_cli_and_requests_without_main():
    held = _with_flows()
    held._read["flow_commands"] = [{"application_name":"example", "application_command_id":7,
        "command_name":"run", "description":"CLI run", "code_symbol_id":100, "node_key":"test"}]
    held._read["flow_requests"] = [{"application_name":"example", "application_request_id":"100:run",
        "request_name":"run", "description":"Requested run", "code_symbol_id":100, "node_key":"test"}]
    held.focus(reading="flow", application="example")
    assert {n["label"] for n in held.projection()["nodes"]} == {"CLI: run", "Request: run"}
    held.double_click("request:100:run")
    projection = held.projection()
    assert projection["focus"] == {"flow":"example", "request_id":"100:run"}
    assert not any(e["source"] == "control:100:test" and e["label"] == "false"
                   for e in projection["edges"])
    assert projection["path"][-1]["label"] == "run"


def test_cli_dictionary_binding_enters_handler_without_a_true_guard():
    held = _with_flows()
    held._read["flow_commands"] = [{"application_name":"example", "application_command_id":7,
        "command_name":"run", "description":"CLI run", "code_symbol_id":100, "node_key":"entry"}]
    held.focus(reading="flow", flow="example", command_id=7, detail=True)
    assert {n["id"] for n in held.projection()["nodes"]} == {
        "control:100:entry", "control:100:test", "control:100:call", "control:100:exit"}


def test_one_request_stops_at_next_receive_and_excludes_sibling_dispatch():
    held = _with_flows()
    base = _flow_rows()[0]
    held._read["flow_steps"] = [
        {**base,"node_key":"loop","kind":"loop","source_line":1,"end_line":30,"to_key":"test","transition_kind":"body"},
        {**base,"node_key":"test","kind":"condition","text":"name == 'run'","to_key":"run","transition_kind":"true"},
        {**base,"node_key":"test","kind":"condition","text":"name == 'run'","to_key":"other","transition_kind":"false"},
        {**base,"node_key":"run","kind":"call","to_key":"loop","transition_kind":"next"},
        {**base,"node_key":"other","kind":"call","to_key":"loop","transition_kind":"next"}]
    held._read["flow_requests"] = [{"application_name":"example", "application_request_id":"100:run",
        "request_name":"run", "code_symbol_id":100,"node_key":"test"}]
    held.focus(reading="flow", flow="example", request_id="100:run", detail=True)
    assert {n["id"] for n in held.projection()["nodes"]} == {"control:100:test", "control:100:run"}
    assert len(held.projection()["edges"]) == 1


def test_step_inspection_exposes_actual_code_source_and_coverage():
    held=_with_flows();held.focus(reading="flow",flow="example",detail=True)
    held.click("control:100:call")
    rows=held.projection()["details"]["rows"]
    assert ["Code","next_task(arg)"] in rows
    assert ["Call target","other.next_task"] in rows
    assert ["File","example/main.py"] in rows
    assert ["Line",10] in rows
    assert ["Coverage","partial"] in rows
    assert held.context_click("control:100:call")=={"identity":{
        "kind":"configured_file","root":"{apps}","relative_path":"example/example/main.py"}, "focus":{"line":10}}


def test_transition_inspection_reports_indexed_branch_not_fabricated_call_order():
    held=_with_flows();held.focus(reading="flow",flow="example",detail=True)
    edge=next(e for e in held.projection()["edges"] if e["label"]=="false")
    held.double_click(edge["id"])
    assert ["Transition","false"] in held.projection()["details"]["rows"]
    assert held.projection()["focus"]=={"flow":"example","detail":True}


def test_drill_enters_exact_resolved_callee_and_breadcrumb_returns(monkeypatch):
    held=_with_flows()
    held._index_connection=SimpleNamespace(handle=lambda:"existing-index")
    calls=[]
    class Control:  # the shared Control's governed flow-steps read (#1360)
        def object_map_flow_steps(self,symbol_id):
            calls.append(symbol_id)
            return _flow_rows(symbol_id)
    held._control=Control()
    held.focus(reading="flow",flow="example")
    held.double_click("control:100:call")
    focus = held.projection()["focus"]
    assert {k: v for k, v in focus.items() if k != "call_site"} == {"flow":"example","symbol":200}
    assert focus["call_site"]["caller_symbol_id"] == 100
    assert focus["call_site"]["code_edge_id"] == 42
    assert focus["call_site"]["continuations"] == [{"node_key":"exit","kind":"next"}]
    held.focus(**held.projection()["path"][-2]["focus"])
    assert calls==[100,200,100]
    assert held.projection()["focus"]=={"flow":"example"}


def test_missing_evidence_is_a_boundary_not_a_fallback_call_graph():
    held=_with_flows();held._read["flow_steps"]=[]
    held.focus(reading="flow",flow="example")
    assert held.projection()["nodes"][0]["kind"]=="boundary"
    assert held.projection()["edges"]==[]
    held.focus(flow="missing")
    assert held.projection()["nodes"][0]["kind"]=="boundary"


def test_unknown_call_refuses_drilling_but_keeps_source_inspectable():
    held=_with_flows()
    held._read["flow_steps"][3]["target_code_symbol_id"]=None
    held.focus(reading="flow",flow="example")
    with pytest.raises(module.AppError,match="does not resolve"):
        held.double_click("control:100:call")
    assert held.context_click("control:100:call")["identity"]["kind"]=="configured_file"


def test_switching_readings_retains_same_map_and_its_flow_path():
    held=_with_flows();held.focus(reading="flow",flow="example")
    held.focus(reading="object")
    held.focus(reading="flow")
    assert held.projection()["focus"]=={"flow":"example"}
    assert held.projection()["readings"]==[OBJECT_PROJECTION,FLOW_PROJECTION]


def test_failed_flow_read_keeps_previous_focus(monkeypatch):
    held=_with_flows();held.focus(reading="flow",flow="example")
    before=held.projection()
    held._index_connection=SimpleNamespace(handle=lambda:"handle")
    class Control:
        def object_map_flow_steps(self,symbol_id): raise module.AppError("read refused")
    held._control=Control()
    with pytest.raises(module.AppError,match="read refused"):
        held.double_click("control:100:call")
    assert held.projection()["focus"]==before["focus"]
    assert held.projection()["nodes"]==before["nodes"]


def test_command_and_workflow_declarations_are_discovered_without_name_inference():
    held = _with_flows()
    held._read["flow_commands"] = [{"application_name":"example", "application_command_id":7,
        "command_name":"serve", "description":"Serve the application", "ordinal":1,
        "code_symbol_id":100,"node_key":"test"}]
    held._read["flow_operations"] = [{"application_name":"example", "application_operation_id":8,
        "operation_name":"serve", "description":"Workflow declaration", "ordinal":1}]
    held.focus(reading="flow", application="example")
    nodes = {n["id"]:n for n in held.projection()["nodes"]}
    assert nodes["command:7"]["role"] == ""
    assert nodes["operation:8"]["role"] == "unresolved"
    # Equal names never make the workflow declaration an implementation edge.
    held.double_click("operation:8")
    assert held.projection()["nodes"][0]["kind"] == "boundary"
    assert held.projection()["edges"] == []
    held.focus(application="example")
    held.double_click("command:7")
    p = held.projection()
    assert p["focus"] == {"flow":"example", "command_id":7}
    assert {n["id"] for n in p["nodes"]} == {"control:100:test","control:100:call","control:100:exit"}
    assert not any(e["source"] == "control:100:test" and e["label"] == "false" for e in p["edges"])


def test_call_sites_to_same_callee_remain_distinct_with_their_return_paths(monkeypatch):
    held = _with_flows()
    first = dict(_flow_rows()[3])
    first.update(node_key="first",to_key="second")
    second = {**first,"node_key":"second","to_key":"exit","code_edge_id":43,"source_line":11,"end_line":11}
    caller = [_flow_rows()[0] | {"to_key":"first"},first,second,_flow_rows()[-1]]
    held._read["flow_steps"] = caller
    class Control:
        def object_map_flow_steps(self, symbol_id):
            return caller if symbol_id == 100 else _flow_rows(200)
    held._control = Control()
    held._index_connection = SimpleNamespace(handle=lambda:"index")
    held.focus(reading="flow",flow="example")
    held.double_click("control:100:first")
    first_focus = held.projection()["focus"]
    first_path = held.projection()["path"][-1]["value"]
    held.focus(flow="example")
    held.double_click("control:100:second")
    assert held.projection()["path"][-1]["value"] != first_path
    assert first_focus["call_site"]["continuations"] == [{"node_key":"second","kind":"next"}]
    assert held.projection()["focus"]["call_site"]["continuations"] == [{"node_key":"exit","kind":"next"}]


def test_object_interactions_are_the_overview_and_cfg_is_explicit_detail():
    held = _with_flows()
    base = {**_flow_rows()[0], "parameter_names":["source","transform","target"],
            "parameter_annotations":["Source","Transform","Target"]}
    rows = [{**base,"node_key":"entry","kind":"entry","text":"Execute one load", "to_key":"source","transition_kind":"next"}]
    for index,(owner,after) in enumerate((("Source","transform"),("Transform","target"),("Target","exit")),1):
        rows.append({**base,"node_key":owner.lower(),"kind":"call","text":owner.lower()+".resolve(ctx)",
            "source_line":10+index,"end_line":10+index,"target_symbol_kind":"method",
            "callee_reference":owner.lower()+".resolve", "target_symbol_name":"resolve",
            "target_owner_name":owner,"target_label":owner+".resolve", "target_code_symbol_id":200+index,
            "to_key":after,"transition_kind":"next"})
    rows.append({**base,"node_key":"exit","kind":"exit","to_key":None,"transition_kind":None})
    held._read["flow_steps"] = rows
    held.focus(reading="flow",flow="example")
    projection=held.projection()
    assert [n["label"] for n in projection["nodes"] if n["kind"]=="activity"] == ["Source.resolve","Transform.resolve","Target.resolve"]
    assert len(projection["edges"])==4
    assert {n["label"] for n in projection["nodes"] if n["kind"]=="participant"} == {
        "source: Source", "transform: Transform", "target: Target"}
    assert all(n.get("parent", "").startswith("participant:100:")
               for n in projection["nodes"] if n["kind"]=="activity")
    held.double_click("control:100:entry")
    assert held.projection()["focus"]["detail"] is True
    assert {n["kind"] for n in held.projection()["nodes"]}=={"entry","call","exit"}


def test_activity_overview_does_not_show_unreachable_code_or_exception_spaghetti():
    held=_with_flows()
    base=_flow_rows()[0]
    held._read["flow_steps"]=_flow_rows()+[
        {**base,"node_key":"dead","kind":"call","text":"never_reached()","to_key":"exit","transition_kind":"next","target_code_symbol_id":999},
        {**base,"node_key":"call","kind":"call","text":"next_task(arg)","to_key":"error","transition_kind":"exception","target_code_symbol_id":200},
        {**base,"node_key":"error","kind":"exception_exit","to_key":None,"transition_kind":None}]
    held.focus(reading="flow",flow="example")
    projection=held.projection()
    assert not any(n["id"].endswith(":dead") or n["kind"]=="exception_exit" for n in projection["nodes"])
    assert all(projection["process"]["edges"][e["id"]] == "failure"
               for e in projection["edges"] if e["label"] == "exception")
    held.click("control:100:entry")
    assert dict(held.projection()["details"]["rows"])["Indexed failure transitions"]==1


def test_activity_arrows_keep_actual_condition_witnesses_and_partial_boundaries():
    held=_with_flows()
    base=_flow_rows()[0]
    held._read["flow_steps"]=_flow_rows()+[
        {**base,"node_key":"entry","kind":"entry","to_key":"gap","transition_kind":"next"},
        {**base,"node_key":"gap","kind":"boundary","text":"unsupported comprehension","source_column":0,"end_column":1,"to_key":"test","transition_kind":"unknown"}]
    # Replace the old entry edge: no alternative bypass is fabricated.
    held._read["flow_steps"]=[r for r in held._read["flow_steps"] if not(r["node_key"]=="entry" and r["to_key"]=="test")]
    held.focus(reading="flow",flow="example")
    projection=held.projection()
    assert not any(n["kind"] in ("boundary","code_section") for n in projection["nodes"])
    assert any(n["kind"] == "condition" and n["label"] == "flag" for n in projection["nodes"])
    to_call=next(e for e in projection["edges"] if e["target"]=="control:100:call")
    assert to_call["label"]=="true"
    partial=next(e for e in projection["edges"] if e["target"]=="control:100:test")
    assert partial["label"]=="partial"
    held.click(partial["id"])
    witness=dict(held.projection()["details"]["rows"])["Indexed transitions"]
    assert any(r["transition_kind"]=="unknown" for r in witness)
    assert {e["label"] for e in projection["edges"] if e["source"]=="control:100:test"} == {"true","false"}


def test_missing_function_coverage_remains_an_explicit_boundary_in_the_overview():
    held=_with_flows()
    held._read["flow_steps"]=[{**_flow_rows()[0],"node_key":None,"kind":None,
                              "to_key":None,"coverage_status":None}]
    held.focus(reading="flow",flow="example")
    assert held.projection()["nodes"][0]["kind"]=="boundary"
    assert "not published" in held.projection()["nodes"][0]["label"]


def test_operation_uses_registered_index_binding_and_reads_exact_callable(monkeypatch):
    held = _with_flows()
    held._read["flow_operations"] = [{"application_name":"example", "application_operation_id":8,
        "operation_name":"export", "description":"Export database DDL", "ordinal":1,
        "code_symbol_id":200}]
    reads = []
    class Control:
        def object_map_flow_steps(self, symbol_id):
            reads.append({"symbol_id": symbol_id})
            return _flow_rows(200)
    held._control = Control()
    held._index_connection = SimpleNamespace(handle=lambda: "handle")
    held.focus(reading="flow", application="example")
    held.double_click("operation:8")
    projection = held.projection()
    assert reads == [{"symbol_id":200}]
    assert any(n["id"] == "control:200:entry" for n in projection["nodes"])
    assert not any(n["kind"] == "boundary" for n in projection["nodes"])
    assert projection["path"][-1]["label"] == "export"


def test_overview_retains_construction_decision_and_explicit_refusal():
    held = _with_flows()
    base = _flow_rows()[0]
    held._read["flow_steps"] = [
        {**base,"node_key":"entry","kind":"entry","text":"Export","to_key":"create","transition_kind":"next"},
        {**base,"node_key":"create","kind":"call","text":"Request()","callee_reference":"Request", "source_line":9,"end_line":9,
         "target_symbol_kind":"class","target_symbol_name":"Request","target_identity":"domain.Request",
         "target_code_symbol_id":201,"to_key":"test","transition_kind":"next"},
        {**base,"node_key":"test","kind":"condition","text":"not selected","to_key":"refuse","transition_kind":"true"},
        {**base,"node_key":"test","kind":"condition","text":"not selected","to_key":"exit","transition_kind":"false"},
        {**base,"node_key":"refuse","kind":"raise","text":"raise AppError('No selection')","to_key":"error","transition_kind":"exception"},
        {**base,"node_key":"error","kind":"exception_exit","to_key":None,"transition_kind":None},
        {**base,"node_key":"exit","kind":"exit","to_key":None,"transition_kind":None}]
    held.focus(reading="flow", flow="example")
    projection = held.projection()
    labels = {n["id"]:n["label"] for n in projection["nodes"]}
    assert labels["control:100:create"] == "Create domain.Request"
    assert labels["control:100:refuse"] == "raise AppError('No selection')"
    assert labels["control:100:error"] == "Failure / refusal to caller"
    assert {(e["label"], e["target"]) for e in projection["edges"]
            if e["source"]=="control:100:test"} == {
                ("true","control:100:refuse"), ("false","control:100:exit")}


def test_request_root_detail_keeps_the_selected_request_instead_of_full_dispatcher():
    held = _with_flows()
    held._read['flow_requests'] = [{'application_name':'example', 'application_request_id':'100:run',
        'request_name':'run', 'code_symbol_id':100, 'node_key':'test'}]
    held.focus(reading='flow', flow='example', request_id='100:run')
    held.double_click('control:100:test')
    projection = held.projection()
    assert projection['focus'] == {'flow':'example', 'request_id':'100:run', 'detail':True}
    assert {n['id'] for n in projection['nodes']} == {
        'control:100:test', 'control:100:call', 'control:100:exit'}
    assert not any(e['label'] == 'false' for e in projection['edges'])


def test_request_overview_keeps_activities_that_return_to_the_command_loop():
    held = _with_flows()
    base = _flow_rows()[0]
    held._read['flow_steps'] = [
        {**base, 'node_key':'receive', 'kind':'loop', 'source_line':1, 'end_line':30,
         'to_key':'test', 'transition_kind':'body'},
        {**base, 'node_key':'test', 'kind':'condition', 'text':"name == 'execute'",
         'to_key':'execute', 'transition_kind':'true'},
        {**base, 'node_key':'test', 'kind':'condition', 'text':"name == 'execute'",
         'to_key':'other', 'transition_kind':'false'},
        {**base, 'node_key':'execute', 'kind':'call', 'text':'query.execute()',
         'source_line':11, 'end_line':11,
         'to_key':'receive', 'transition_kind':'next'},
        {**base, 'node_key':'other', 'kind':'call', 'text':'query.lint()',
         'source_line':12, 'end_line':12,
         'to_key':'receive', 'transition_kind':'next'},
    ]
    held._read['flow_requests'] = [{'application_name':'example',
        'application_request_id':'100:execute', 'request_name':'execute',
        'code_symbol_id':100, 'node_key':'test'}]
    held.focus(reading='flow', application='example')
    held.double_click('request:100:execute')
    projection = held.projection()
    assert {n['id'] for n in projection['nodes']} == {'control:100:test', 'control:100:execute', 'request-return:100'}
    assert next(n for n in projection['nodes'] if n['id']=='request-return:100')['label'] == 'Await next request'
    assert len(projection['edges']) == 2
    held.double_click('control:100:test')
    assert held.projection()['focus']['detail'] is True
    assert {n['id'] for n in held.projection()['nodes']} == {'control:100:test', 'control:100:execute'}


def test_flow_right_click_opens_resolved_function_definition_in_its_own_file():
    held = _with_flows()
    held._read['flow_steps'] = [
        {**row, **({'target_source_repository':'other', 'target_relative_path':'other/worker.py',
                   'target_start_line':72} if row['node_key']=='call' else {})}
        for row in _flow_rows()]
    held.focus(reading='flow', flow='example', detail=True)
    assert held.context_click('control:100:call') == {
        'identity': {'kind':'configured_file', 'root':'{apps}', 'relative_path':'other/other/worker.py'},
        'focus': {'line':72}}


def test_flow_input_right_click_opens_the_bound_handler_at_its_indexed_line():
    held = _with_flows()
    held._read['flow_commands'] = [{'application_name':'example', 'application_command_id':7,
        'command_name':'scan', 'code_symbol_id':100, 'node_key':'entry',
        'repository_key':'example', 'relative_path':'main.py', 'source_line':52}]
    held.focus(reading='flow', application='example')
    assert held.context_click('command:7') == {
        'identity': {'kind':'configured_file', 'root':'{apps}', 'relative_path':'example/main.py'},
        'focus': {'line':52}}


def test_application_map_is_scoped_and_has_no_estate_navigation(monkeypatch):
    flows = _with_flows()._read['flows']
    class Control:  # the shared Control's governed flow reads (#1360)
        def object_map_flows(self): return flows
        def object_map_flow_commands(self): return []
        def object_map_flow_operations(self): return []
        def object_map_flow_requests(self): return []
    held = ObjectMap.for_application(SimpleNamespace(handle=lambda:'index'), 'example', Control())
    assert held.projection()['subject'] == {'kind':'application', 'application':'example'}
    assert held.projection()['focus'] == {'flow':None, 'application':'example'}
    assert all(s['label'] != 'Applications' for s in held.projection()['path'])
    held.focus()
    assert held.projection()['focus']['application'] == 'example'
    with pytest.raises(module.AppError, match='another application'):
        held.focus(application='missing')
    with pytest.raises(module.AppError, match='no flow'):
        held.focus(flow='missing')


def test_architecture_factory_holds_only_the_object_reading():
    reads = []
    class Control:
        def __getattr__(self, name):
            if not name.startswith('object_map_'):
                raise AttributeError(name)
            return lambda: reads.append(name) or []
    held = ObjectMap.for_architecture(SimpleNamespace(handle=lambda:None), 'local', Control())
    assert held.projection()['readings'] == ['object']
    assert len(reads) == 5
    assert not any(name.startswith('object_map_flow') for name in reads)


def test_process_backbone_keeps_alternatives_and_never_claims_run_frequencies():
    held = _with_flows()
    held.focus(reading='flow', flow='example')
    projection = held.projection()
    process = projection['process']
    assert process['basis'] == 'static'
    assert process['measurements'] is None
    assert process['nodes']['control:100:call'] == 'primary'
    assert process['alternative_count'] > 0
    assert set(process['edges']) == {e['id'] for e in projection['edges']}
    assert all(role in ('primary','alternative','failure') for role in process['edges'].values())


def test_failure_paths_are_revealable_without_changing_the_successful_route():
    held = _with_flows()
    base = _flow_rows()[0]
    held._read['flow_steps'] = _flow_rows() + [
        {**base, 'node_key':'call', 'kind':'call', 'text':'next_task(arg)',
         'to_key':'failed', 'transition_kind':'exception', 'target_code_symbol_id':200},
        {**base, 'node_key':'failed', 'kind':'exception_exit', 'to_key':None, 'transition_kind':None}]
    held.focus(reading='flow', flow='example')
    projection = held.projection()
    assert projection['process']['nodes']['control:100:failed'] == 'failure'
    assert projection['process']['nodes']['control:100:exit'] == 'primary'
    assert projection['process']['failure_count'] > 0
    for edge in projection['edges']:
        if edge['target'] == 'control:100:failed':
            assert projection['process']['edges'][edge['id']] == 'failure'


def test_function_detail_has_an_authoritative_path_back_to_its_parent():
    held = _with_flows()
    held.focus(reading='flow', flow='example')
    parent = held.projection()
    held.double_click('control:100:entry')
    child = held.projection()
    assert child['view']['key'] != parent['view']['key']
    assert parent['view']['key'] in [step['key'] for step in child['view']['ancestors']]
    assert child['path'][-1]['focus']['detail'] is True
    held.focus(**parent['focus'])
    assert held.projection()['view']['key'] == parent['view']['key']


def test_backbone_does_not_prefer_a_branch_for_repeated_accessors_on_the_same_owner():
    # All routes are source-witnessed. Repeated property reads must not make
    # a longer optional branch more prominent than the simpler successful one.
    elements = [({'id':key,'kind':kind, **({'receiver':'source'} if key in ('work','accessor') else {})}, {})
                for key,kind in [('start','activity_entry'),('work','activity'),('choice','condition'),
                                 ('accessor','activity'),('end','activity_exit')]]
    relations = [({'id':f'{a}-{b}','source':a,'target':b,'label':''}, {}) for a,b in
                 [('start','work'),('work','choice'),('choice','accessor'),('accessor','end'),('choice','end')]]
    process = ObjectMap._process_reading(elements, relations)
    assert process['nodes']['work'] == 'primary'
    assert process['nodes']['accessor'] == 'alternative'
    assert process['edges']['choice-end'] == 'primary'


def test_negative_request_guard_follows_its_false_branch_and_preserves_request_context():
    held = _with_flows()
    base = _flow_rows()[0]
    held._read['flow_steps'] = [
        {**base, 'node_key':'guard', 'kind':'condition', 'text':"name != 'task'",
         'commands':['request-false:task'], 'to_key':'reject', 'transition_kind':'true'},
        {**base, 'node_key':'guard', 'kind':'condition', 'text':"name != 'task'",
         'commands':['request-false:task'], 'to_key':'create', 'transition_kind':'false'},
        {**base, 'node_key':'reject', 'kind':'raise', 'text':'Unknown request',
         'to_key':'error', 'transition_kind':'exception'},
        {**base, 'node_key':'create', 'kind':'call', 'text':'hydrate_task()',
         'to_key':'exit', 'transition_kind':'next'},
        {**base, 'node_key':'error', 'kind':'exception_exit', 'to_key':None, 'transition_kind':None},
        {**base, 'node_key':'exit', 'kind':'exit', 'to_key':None, 'transition_kind':None},
    ]
    held._read['flow_requests'] = [{'application_name':'example', 'application_request_id':'100:task',
        'request_name':'task', 'request_branch':'false', 'code_symbol_id':100, 'node_key':'guard'}]
    held.focus(reading='flow', flow='example', request_id='100:task', detail=True)
    projection = held.projection()
    assert {n['id'] for n in projection['nodes']} == {'control:100:guard','control:100:create','control:100:exit'}
    assert next(e for e in projection['edges'] if e['source']=='control:100:guard')['label'] == 'false'
    assert projection['path'][-1]['focus']['request_id'] == '100:task'
