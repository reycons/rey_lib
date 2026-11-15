"""The Tree's public node projection (#1218): what a node is outside the Tree."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from rey_lib.tree import PUBLIC_NODE_FIELDS, public_node

_DECLARATION = {
    "action_key": "open_database_object", "label": "Open", "icon": "database",
    "placement": "menu", "action_target": None, "requires_state": None,
    "target": {"subject": "database.table", "fields": {"name": "subject_id"}},
}

_RECORD = {
    "node_key": "5:5:1", "parent_node_key": "5:5", "label": "Runs", "icon": "run",
    "tone": "success", "has_children": True, "is_enabled": True, "selectable": True,
    "text_click": _DECLARATION, "buttons": [{**_DECLARATION, "sort_order": 1}],
    "panel_controls": [],
    # Traversal and ordering internals the record also carries.
    "tree_object_key": "files", "tree_configuration": {"connection": "rey_apps"},
    "expansion_tvw": "tree.run_history_tvw", "page_size": None,
    "sort_number": Decimal("1.5"), "sort_ts": datetime(2026, 10, 24, tzinfo=timezone.utc),
    "subject_type": "run", "subject_id": "42",
}


def test_a_public_node_is_exactly_the_declared_fields() -> None:
    assert tuple(public_node(_RECORD)) == PUBLIC_NODE_FIELDS
    assert public_node(_RECORD)["text_click"] == _DECLARATION
    assert public_node(_RECORD)["buttons"] == [{**_DECLARATION, "sort_order": 1}]


def test_right_click_is_carried_to_the_existing_domain_projection():
    right = {**_DECLARATION, "target": {"kind": "database_schema"}}
    assert public_node({**_RECORD, "right_click": right})["right_click"] == right


def test_traversal_and_ordering_internals_stay_inside_the_tree() -> None:
    shown = public_node(_RECORD)
    for internal in ("tree_object_key", "tree_configuration", "expansion_tvw",
                     "page_size", "sort_number", "sort_ts"):
        assert internal not in shown


def test_a_public_node_is_plain_json() -> None:
    # The ordering internals are the record's only non-JSON values.
    assert json.loads(json.dumps(public_node(_RECORD)))["node_key"] == "5:5:1"


def test_a_field_the_record_lacks_is_stated_as_absent() -> None:
    assert public_node({"node_key": "1:1"})["tone"] is None


def test_source_identity_is_internal_and_keys_never_target_a_different_entity_after_refresh():
    from rey_lib.tree import TreeProjection
    projection = TreeProjection()
    root = {'node_key': 'root'}
    projection.seed([root])
    before = projection.append([{'source_id': 'a', 'node_type': 'item', 'label': 'A'}], 'root')[0]
    projection.seed([root])
    after = projection.append([{'source_id': 'b', 'node_type': 'item', 'label': 'B'}], 'root')[0]
    assert before['node_key'] != after['node_key']
    assert projection.record(before['node_key']) is None
    assert 'source_id' not in public_node(after)
    projection.seed([root])
    again = projection.append([{'source_id': 'a', 'node_type': 'item', 'label': 'Renamed'}], 'root')[0]
    assert again['node_key'] == before['node_key']


def test_an_unidentified_generated_key_is_not_recycled_after_seed():
    from rey_lib.tree import TreeProjection
    projection = TreeProjection()
    first = projection.append([{'label': 'A'}], 'root')[0]['node_key']
    projection.seed([{'node_key': 'root'}])
    second = projection.append([{'label': 'B'}], 'root')[0]['node_key']
    assert first != second
