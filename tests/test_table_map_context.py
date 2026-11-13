"""A table context changes the same live map; no selection opens another panel."""
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.inferred_architecture import object_map as module
from tests.test_object_map import _schema


def _index(monkeypatch, rows):
    """The shared Control's governed table-context read (#1360): its calls recorded."""
    calls = []

    class Control:
        def object_map_table_context(self, catalog, provider, schema, table, node, installation):
            calls.append(("object_map_table_context", {
                "catalog": catalog, "provider": provider, "schema": schema, "table": table,
                "node": node, "installation": installation}))
            return rows

    held = _schema()
    held._index_connection = SimpleNamespace(handle=lambda: "index-handle")
    held._control = Control()
    held._index_installation = "admin"
    held._database_provider = "postgres"
    return held, calls


def test_context_projects_the_index_graph_and_returns_to_the_same_schema(monkeypatch):
    held, calls = _index(monkeypatch, [{
        "root_count": 1, "root_id": "db:1",
        "nodes": [
            {"id": "db:1", "kind": "table", "label": "control.run", "depth": 0},
            {"id": "db:2", "kind": "table", "label": "control.run_log", "depth": 1},
            {"id": "db:3", "kind": "view", "label": "control.run_vw", "depth": 2},
            {"id": "symbol:4", "kind": "code", "label": "rey_lib:run.py:read", "depth": 2},
            {"id": "db:6", "kind": "code", "label": "old signature label(bigint)",
             "identity": {"schema": "control", "name": "rate", "object_type": "function",
                          "signature": "bigint"}, "depth": 1},
        ],
        "edges": [{"id": "fk:1", "kind": "foreign_key", "source": "db:2", "target": "db:1"}],
    }])
    assert held.double_click("table:run") is None
    projected = held.projection()
    assert projected["focus"] == {"table": "run", "opened": []}
    assert projected["selected"] is None
    assert {n["kind"] for n in projected["nodes"]} == {"table", "view", "code"}
    assert projected["edges"][0]["target"] == "table:run"
    routine = next(n for n in projected["nodes"] if n["id"] == "db:6")
    assert routine["label"] == "control.rate"
    assert routine["identity"]["signature"] == "bigint"
    assert calls[0][1] == {"catalog": "rey_apps", "provider": "postgres",
                           "schema": "control", "table": "run", "node": None, "installation": "admin"}
    assert {e["id"] for e in held.inspect()["elements"]} == {n["id"] for n in projected["nodes"]}
    held.click("symbol:4")
    assert held.projection()["selected"] == "symbol:4"
    held.focus(**projected["path"][0]["focus"])
    assert len(held.projection()["path"]) == 1
    assert {n["kind"] for n in held.projection()["nodes"]} <= {"table", "external"}


@pytest.mark.parametrize("count", [0, 2])
def test_an_absent_or_ambiguous_index_root_refuses_without_changing_focus(monkeypatch, count):
    held, _ = _index(monkeypatch, [{"root_count": count}])
    before = held.projection()
    with pytest.raises(AppError, match="exactly one table"):
        held.double_click("table:run")
    assert held.projection() == before


def test_no_index_connection_refuses_instead_of_showing_an_empty_neighbourhood():
    held = _schema()
    with pytest.raises(AppError, match="index connection"):
        held.double_click("table:run")
    assert held.projection()["focus"] == {"opened": []}


def _graph(root, nodes):
    return {"root_count": 1, "root_id": root,
            "nodes": [{"id": key, "kind": kind, "label": key} for key, kind in nodes],
            "edges": []}


def test_double_clicks_continue_from_view_to_code_and_breadcrumbs_keep_the_path(monkeypatch):
    rows = [_graph("db:1", [("db:1", "table"), ("db:3", "view")])]
    held, calls = _index(monkeypatch, rows)
    held.double_click("table:run")

    rows[:] = [_graph("db:3", [("db:3", "view"), ("symbol:4", "code")])]
    held.double_click("db:3")
    assert calls[-1][1]["node"] == "db:3"
    assert calls[-1][1]["table"] is None
    assert held.projection()["selected"] is None
    view_focus = held.projection()["path"][-1]["focus"]

    rows[:] = [_graph("symbol:4", [("symbol:4", "code"), ("symbol:5", "code")])]
    held.double_click("symbol:4")
    assert calls[-1][1]["node"] == "symbol:4"
    assert [p["value"] for p in held.projection()["path"]] == [
        "schema", "table:run", "db:3", "symbol:4"]

    rows[:] = [_graph("symbol:5", [("symbol:5", "code")])]
    held.double_click("symbol:5")
    # The view is no longer displayed; its existing breadcrumb still navigates back.
    rows[:] = [_graph("db:3", [("db:3", "view"), ("symbol:4", "code")])]
    held.focus(**view_focus)
    assert [p["value"] for p in held.projection()["path"]] == ["schema", "table:run", "db:3"]
    held.focus(opened=[])
    assert [p["value"] for p in held.projection()["path"]] == ["schema"]


def test_a_related_table_can_be_the_next_root(monkeypatch):
    rows = [_graph("db:1", [("db:1", "table"), ("db:2", "table")])]
    held, calls = _index(monkeypatch, rows)
    held.double_click("table:run")
    rows[:] = [_graph("db:2", [("db:2", "table"), ("db:8", "view")])]
    held.double_click("db:2")
    assert calls[-1][1]["node"] == "db:2"
    assert held.projection()["selected"] is None
    assert held.projection()["focus"]["node"] == "db:2"


def test_a_known_external_table_uses_its_own_schema_identity(monkeypatch):
    rows = [_graph("db:9", [("db:9", "table")])]
    held, calls = _index(monkeypatch, rows)
    held.double_click("external:code.installation")
    assert calls[-1][1]["schema"] == "code"
    assert calls[-1][1]["table"] == "installation"
    assert calls[-1][1]["node"] is None
    assert held.projection()["selected"] is None


def test_an_unseen_node_cannot_be_used_to_jump_to_arbitrary_index_objects(monkeypatch):
    held, calls = _index(monkeypatch, [])
    before = held.projection()
    with pytest.raises(AppError, match="holds no indexed node"):
        held.focus(node="symbol:999")
    assert calls == []
    assert held.projection() == before


@pytest.mark.parametrize("kind", ["table", "view", "function", "procedure"])
def test_right_click_returns_the_standard_database_opening_without_moving_focus(monkeypatch, kind):
    rows = [_graph("db:1", [("db:1", "table")])]
    rows[0]["nodes"].append({
        "id": "db:9", "kind": kind if kind in ("table", "view") else "code",
        "identity": {"schema": "other", "name": "named_object", "object_type": kind,
                     "signature": "integer" if kind in ("function", "procedure") else ""},
    })
    held, calls = _index(monkeypatch, rows)
    held.double_click("table:run")
    before = held.projection()
    expected = {"kind": "database_object", "subject_kind": kind + "s",
                "connection": held.subject["connection"], "catalog": "rey_apps",
                "schema": "other", "name": "named_object"}
    if kind in ("function", "procedure"):
        expected["signature"] = "integer"
    assert held.context_click("db:9") == {"identity": expected}
    assert held.projection() == before
    assert len(calls) == 1  # Opening is the existing opener's job; the map reads nothing again.


def test_right_click_on_code_returns_the_existing_configured_file_opening(monkeypatch):
    rows = [_graph("db:1", [("db:1", "table")])]
    rows[0]["nodes"].append({"id": "symbol:4", "kind": "code",
        "identity": {"repository_key": "rey_lib", "relative_path": "rey_lib/db/database_query.py"}})
    held, _ = _index(monkeypatch, rows)
    held.double_click("table:run")
    assert next(n for n in held.projection()["nodes"] if n["id"] == "symbol:4")["role"] == "file"
    assert held.context_click("symbol:4") == {"identity": {
        "kind": "configured_file", "root": "{apps}",
        "relative_path": "rey_lib/rey_lib/db/database_query.py"}}


def test_overview_table_and_external_table_use_the_same_database_opening():
    held = _schema()
    assert held.context_click("table:run") == {"identity": {
        "kind": "database_object", "subject_kind": "tables", "connection": "rey apps",
        "catalog": "rey_apps", "schema": "control", "name": "run"}}
    assert held.context_click("external:code.installation")["identity"]["schema"] == "code"


@pytest.mark.parametrize("kind", ["code", "database"])
def test_both_maps_select_on_single_drill_on_double_and_open_on_right(monkeypatch, kind):
    from tests.test_object_map import _architecture

    if kind == "code":
        held = _architecture()
        held.focus(level="area", area_id=10)
        node = "claim:1"
    else:
        held, _ = _index(monkeypatch, [_graph("db:1", [("db:1", "table")])])
        node = "table:run"
    before = dict(held.projection()["focus"])
    assert held.click(node) is None
    assert held.projection()["selected"] == node
    assert held.projection()["focus"] == before
    assert held.double_click(node) is None
    assert held.projection()["focus"] != before
    focused = held.projection()
    assert "identity" in held.context_click(node)
    assert held.projection() == focused
