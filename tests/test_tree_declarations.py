"""The governed declaration authority, read and interpreted in the tree domain (#1165).

What a node type declares and whether a placement applies to a record's state.
Nothing here binds, signs, launches or presents.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.tree import declarations
from rey_lib.tree.declarations import (
    NODE_TYPE_ACTION_VIEW_NAME,
    NodeTypeActionDataObject,
    declaration,
    interacted_declaration,
    offered,
    offered_actions,
)


def _row(node_type: str, placement: str | None, action_key: str = "", **values: Any) -> dict:
    return {"node_type_key": node_type, "selectable": True, "placement": placement,
            "sort_order": values.pop("sort_order", None), "action_key": action_key,
            "action_label": action_key.title(), "action_icon": "file",
            "action_placement": "menu", "action_target": None,
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
        "action_target": None, "requires_state": "done",
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


# -- the declaration a reported interaction names (#1218) ----------------------

_DECLARED = {
    "run": {
        "selectable": True,
        "text_click": {"action_key": "open_database_object", "requires_state": None},
        "buttons": [{"action_key": "open_rollback", "requires_state": "rollbackable"}],
        "panel_controls": [{"action_key": "refresh", "requires_state": None}],
    },
}


def test_a_text_click_names_the_types_text_click_declaration() -> None:
    found = interacted_declaration(_DECLARED, {"node_type": "run"}, interaction="text_click")

    assert found["action_key"] == "open_database_object"


def test_a_control_names_the_button_or_panel_control_it_was_offered_under() -> None:
    record = {"node_type": "run", "actionable_in": "rollbackable"}

    assert interacted_declaration(_DECLARED, record, control="open_rollback")["action_key"] \
        == "open_rollback"
    assert interacted_declaration(_DECLARED, record, control="refresh")["action_key"] \
        == "refresh"


def test_a_control_not_offered_in_the_records_state_matches_nothing() -> None:
    assert interacted_declaration(_DECLARED, {"node_type": "run", "actionable_in": "done"},
                                  control="open_rollback") is None


def test_a_control_the_type_never_declared_matches_nothing() -> None:
    assert interacted_declaration(_DECLARED, {"node_type": "run"}, control="delete") is None


def test_an_unknown_type_or_an_unnamed_interaction_matches_nothing() -> None:
    assert interacted_declaration(_DECLARED, {"node_type": "area"}, interaction="text_click") \
        is None
    assert interacted_declaration(_DECLARED, {"node_type": "run"}) is None


# -- what one record offers, in its state (#1218) -------------------------------

def test_a_record_offers_what_its_type_declares_in_its_state() -> None:
    offers = offered_actions(_DECLARED, {"node_type": "run", "actionable_in": "rollbackable"})

    assert offers == {
        "selectable": True,
        "text_click": {"action_key": "open_database_object", "requires_state": None},
        "buttons": [{"action_key": "open_rollback", "requires_state": "rollbackable"}],
        "panel_controls": [{"action_key": "refresh", "requires_state": None}],
    }


def test_a_declaration_needing_another_state_is_absent() -> None:
    offers = offered_actions(_DECLARED, {"node_type": "run", "actionable_in": "done"})

    assert offers["buttons"] == []
    assert offers["text_click"]["action_key"] == "open_database_object"


def test_an_undeclared_type_offers_nothing() -> None:
    assert offered_actions(_DECLARED, {"node_type": "area"}) == {
        "selectable": False, "text_click": None, "buttons": [], "panel_controls": []}
