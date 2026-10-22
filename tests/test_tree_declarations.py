"""The governed declaration authority, read and interpreted in the tree domain (#1165).

What a node type declares, whether a placement applies to a record's state, and the
identity a declaration binds a record to -- an unsigned plain fact. Nothing here
signs, launches or presents.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import declarations
from rey_lib.tree.declarations import (
    NODE_TYPE_ACTION_VIEW_NAME,
    NodeTypeActionDataObject,
    declaration,
    declared_fact,
    offered,
)


def _row(node_type: str, placement: str | None, action_key: str = "", **values: Any) -> dict:
    return {"node_type_key": node_type, "selectable": True, "placement": placement,
            "sort_order": values.pop("sort_order", None), "action_key": action_key,
            "action_label": action_key.title(), "action_icon": "file",
            "action_placement": "menu", "route": None, "action_target": None,
            "requires_state": values.pop("requires_state", None),
            "target": values.pop("target", None)}


def _reader(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> NodeTypeActionDataObject:
    asked: list[str] = []
    monkeypatch.setattr(declarations, "shared_connection",
                        lambda ctx, name: asked.append(name) or SimpleNamespace(handle=lambda: None))
    monkeypatch.setattr(declarations, "DatabaseQuery",
                        lambda statement: SimpleNamespace(rows=lambda handle: rows))
    reader = NodeTypeActionDataObject(identity=DatabaseObjectIdentity(
        connection="tree", catalog="rey_apps", schema="tree", name=NODE_TYPE_ACTION_VIEW_NAME))
    reader.asked = asked  # type: ignore[attr-defined]
    return reader


def test_declarations_are_grouped_by_node_type_and_placement(monkeypatch) -> None:
    reader = _reader(monkeypatch, [
        _row("run", "text_click", "open_database_object", target={"kind": "database_object"}),
        _row("run", "button", "open_rollback", sort_order=1, requires_state="rollbackable"),
        _row("run", "panel", "refresh", sort_order=2),
        _row("folder", None),
    ])

    found = reader.declarations(object())

    assert reader.asked == ["tree"]  # the reader's own connection
    assert found["run"]["text_click"]["action_key"] == "open_database_object"
    assert [b["action_key"] for b in found["run"]["buttons"]] == ["open_rollback"]
    assert found["run"]["buttons"][0]["sort_order"] == 1
    assert [c["action_key"] for c in found["run"]["panel_controls"]] == ["refresh"]
    assert found["folder"] == {"selectable": True, "text_click": None,
                               "buttons": [], "panel_controls": []}


def test_a_declaration_is_the_view_row_s_own_settings() -> None:
    assert declaration(_row("run", "text_click", "open", target={"kind": "k"},
                            requires_state="done")) == {
        "action_key": "open", "label": "Open", "icon": "file", "placement": "menu",
        "route": None, "action_target": None, "requires_state": "done",
        "target": {"kind": "k"},
    }


@pytest.mark.parametrize("declared, state, applies", [
    (None, "", False),
    ({"requires_state": None}, "", True),
    ({"requires_state": None}, "rollbackable", True),
    ({"requires_state": "rollbackable"}, "rollbackable", True),
    ({"requires_state": "rollbackable"}, "", False),
])
def test_a_declaration_applies_only_in_the_state_it_requires(declared, state, applies) -> None:
    assert offered(declared, state) is applies


def test_a_declaration_binds_flat_fields_and_stated_values() -> None:
    declared = {"target": {"kind": "database_object", "connection": "rey_apps",
                           "fields": {"run_id": "run_id"}}}

    assert declared_fact(declared, {"run_id": 7, "other": 1}) == {
        "kind": "database_object", "connection": "rey_apps", "run_id": 7,
    }


def test_field_references_inside_stated_values_are_read_from_the_record() -> None:
    declared = {"target": {"kind": "database_object", "filters": [
        {"column": "backlog_key", "operator": "equals", "value": {"$field": "subject_id"}}]}}

    fact = declared_fact(declared, {"subject_id": "1143"})

    assert fact == {"kind": "database_object", "filters": [
        {"column": "backlog_key", "operator": "equals", "value": "1143"}]}


def test_a_field_reference_the_record_lacks_is_refused() -> None:
    with pytest.raises(ConfigError, match="record field 'subject_id'"):
        declared_fact({"target": {"kind": "k", "value": {"$field": "subject_id"}}}, {})


@pytest.mark.parametrize("target", [None, {}, {"kind": "k"}, {"fields": {"a": "a"}}])
def test_no_identity_is_declared_without_a_kind_and_something_to_bind(target) -> None:
    assert declared_fact({"target": target}, {"a": 1}) is None


def test_a_column_reading_wins_over_a_stated_value_of_the_same_name() -> None:
    declared = {"target": {"kind": "k", "name": "stated", "fields": {"name": "label"}}}

    assert declared_fact(declared, {"label": "from the row"})["name"] == "from the row"


def test_the_fact_is_plain_and_unsigned() -> None:
    fact = declared_fact({"target": {"kind": "k", "fields": {"a": "a"}}}, {"a": 1})

    assert isinstance(fact, dict) and fact == {"kind": "k", "a": 1}
