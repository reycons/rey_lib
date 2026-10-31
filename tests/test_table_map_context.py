"""A table context changes the same live map; no selection opens another panel."""
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.inferred_architecture import object_map as module
from tests.test_object_map import _schema


def _index(monkeypatch, rows):
    calls = []

    class Query:
        def __init__(self, statement, params):
            calls.append((statement, params))

        def rows(self, handle):
            assert handle == "index-handle"
            return rows

    monkeypatch.setattr(module, "DatabaseQuery", Query)
    held = _schema()
    held._index_connection = SimpleNamespace(handle=lambda: "index-handle")
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
        ],
        "edges": [{"id": "fk:1", "kind": "foreign_key", "source": "db:2", "target": "db:1"}],
    }])
    assert held.context_click("table:run") is None
    projected = held.projection()
    assert projected["focus"] == {"table": "run", "opened": []}
    assert projected["selected"] == "table:run"
    assert {n["kind"] for n in projected["nodes"]} == {"table", "view", "code"}
    assert projected["edges"][0]["target"] == "table:run"
    assert calls[0][1] == {"catalog": "rey_apps", "provider": "postgres",
                           "schema": "control", "table": "run", "installation": "admin"}
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
        held.context_click("table:run")
    assert held.projection() == before


def test_no_index_connection_refuses_instead_of_showing_an_empty_neighbourhood():
    held = _schema()
    with pytest.raises(AppError, match="index connection"):
        held.context_click("table:run")
    assert held.projection()["focus"] == {"opened": []}
