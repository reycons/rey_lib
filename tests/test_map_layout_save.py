"""Existing live maps own layout edits and Save; Control only persists them."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rey_lib.control.control import Control
from rey_lib.errors.error_utils import ConfigError
from rey_lib.inferred_architecture.object_map import ObjectMap
from rey_lib.workflow.workflow import Workflow


GEOMETRY = {"elements": ["a"], "positions": {"a": {"x": 12, "y": 34}},
            "pan": {"x": 5, "y": 6}, "zoom": 1.5}


class Store:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.fail_read = False

    def map_layout(self, kind, subject, screen):
        if self.fail_read:
            raise ConfigError("layout read failed")
        return deepcopy(self.rows.get((kind, repr(subject), repr(screen))))

    def save_map_layout(self, kind, subject, screen, geometry, revision):
        key = kind, repr(subject), repr(screen)
        prior = self.rows.get(key)
        if revision != (prior["revision"] if prior else 0):
            raise ConfigError("stale layout")
        self.writes.append((kind, deepcopy(geometry), revision))
        self.rows[key] = {"geometry": deepcopy(geometry), "revision": revision + 1}


def mapped(store):
    held = ObjectMap({"kind": "application", "application": "example"}, "flow", {
        "flows": [], "flow_commands": [], "flow_operations": [], "flow_requests": []})
    held.bind_layout(store)
    return held


def saving(projected):
    return [a for a in projected["actions"] if a["role"]["act"] == "save"]


def test_map_edit_does_not_write_and_save_is_declared_only_while_dirty():
    store = Store()
    held = mapped(store)
    projected = held.layout_projection()
    assert not saving(projected)
    edit = projected["actions"][0]
    geometry = deepcopy(GEOMETRY)
    held.edit_layout(geometry, edit["command"]["scope"])
    geometry["positions"]["a"]["x"] = 999
    assert store.writes == []
    assert saving(held.layout_projection())
    held.save(edit["command"]["scope"])
    assert store.writes == [("flow", GEOMETRY, 0)]
    assert not saving(held.layout_projection())
    assert mapped(store).layout_projection()["geometry"] == GEOMETRY


def loading(projected):
    return [a for a in projected["actions"] if a["role"]["act"] == "load"]


def test_open_from_save_holds_the_saved_arrangement_again_and_writes_nothing():
    """Open from save (#1426): declared only with a saved arrangement; restores it, saves nothing."""
    store = Store()
    held = mapped(store)
    scope = held.layout_projection()["actions"][0]["command"]["scope"]
    assert not loading(held.layout_projection())
    with pytest.raises(ConfigError, match="no saved layout"):
        held.load(scope)
    held.edit_layout(GEOMETRY, scope)
    held.save(scope)
    writes = deepcopy(store.writes)
    rows = deepcopy(store.rows)
    held.edit_layout({**GEOMETRY, "zoom": 3}, scope)
    [load] = loading(held.layout_projection())
    assert load["command"] == {"command": "map", "action": "load", "scope": scope}
    held.load(scope)
    projected = held.layout_projection()
    assert projected["geometry"] == GEOMETRY
    assert not saving(projected)
    assert store.writes == writes and store.rows == rows


def test_map_stale_save_is_refused_and_keeps_the_unsaved_geometry():
    store = Store()
    first, second = mapped(store), mapped(store)
    scope = first.layout_projection()["actions"][0]["command"]["scope"]
    second.layout_projection()
    first.edit_layout(GEOMETRY, scope)
    first.save(scope)
    second.edit_layout({**GEOMETRY, "zoom": 2}, scope)
    with pytest.raises(ConfigError, match="stale"):
        second.save(scope)
    assert saving(second.layout_projection())
    assert mapped(store).layout_projection()["geometry"] == GEOMETRY


def test_a_read_failure_is_not_treated_as_an_unsaved_map():
    store = Store()
    store.fail_read = True
    with pytest.raises(ConfigError, match="read failed"):
        mapped(store).layout_projection()


def test_map_refuses_domain_data_in_a_geometry_edit():
    held = mapped(Store())
    scope = held.layout_projection()["actions"][0]["command"]["scope"]
    with pytest.raises(ConfigError, match="geometry only"):
        held.edit_layout({**GEOMETRY, "nodes": []}, scope)


def test_a_lines_routing_is_layout_saved_and_restored_with_the_positions():
    """A moved line's turn (#1426) is geometry: saved with the layout, read back."""
    store = Store()
    held = mapped(store)
    scope = held.layout_projection()["actions"][0]["command"]["scope"]
    routed = {**GEOMETRY, "routes": {"reference:7": 0.6}}
    held.edit_layout(routed, scope)
    held.save(scope)
    assert mapped(store).layout_projection()["geometry"] == routed
    workflow = Workflow(workflow_id=11, name="example", enabled=True,
                        config={}, processes=[], control=Store())
    workflow.bind_map_layout()
    workflow.edit_map_layout(routed)
    assert workflow.map_layout_projection()["geometry"] == routed


def test_a_panels_width_is_layout_saved_and_restored_with_the_positions() -> None:
    """A panel the reader widened (#1459) is geometry: its width saved with the layout,
    read back; a layout without one is unchanged."""
    store = Store()
    held = mapped(store)
    scope = held.layout_projection()["actions"][0]["command"]["scope"]
    widened = {**GEOMETRY, "widths": {"table:run": 420.5}}
    held.edit_layout(widened, scope)
    held.save(scope)
    assert mapped(store).layout_projection()["geometry"] == widened


def test_workflow_layout_save_keeps_its_live_definition_and_is_recovered():
    store = Store()
    def workflow():
        held = Workflow(workflow_id=11, name="example", enabled=True,
                        config={}, processes=[], control=store)
        held.bind_map_layout()
        return held
    held = workflow()
    definition = held.configured_definition()
    assert not saving(held.map_layout_projection())
    held.edit_map_layout(GEOMETRY)
    assert store.writes == []
    assert saving(held.map_layout_projection())
    held.save_map_layout()
    assert held.configured_definition() == definition
    assert not held.unsaved()
    assert not saving(held.map_layout_projection())
    assert workflow().map_layout_projection()["geometry"] == GEOMETRY


def test_control_uses_the_existing_dataset_bindings_and_does_not_swallow_errors():
    calls = Mock(return_value=[{"geometry": GEOMETRY, "revision": 1}])
    control = SimpleNamespace(_call_rows=calls)
    assert Control.map_layout(control, "flow", {}, {})["revision"] == 1
    calls.assert_called_with("get_map_layout", {
        "map_kind": "flow", "subject_identity": {}, "screen_identity": {}}, required=True)
    Control.save_map_layout(control, "flow", {}, {}, GEOMETRY, 0)
    assert calls.call_args.args[0] == "save_map_layout"
    calls.side_effect = ConfigError("unavailable")
    with pytest.raises(ConfigError, match="unavailable"):
        Control.map_layout(control, "flow", {}, {})
