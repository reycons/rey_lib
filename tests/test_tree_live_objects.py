"""Live reference hierarchy follows the canonical external-source contract."""
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import TreeProjection, TreeTraversal, rey_lib_sources
from rey_lib.tree.sources import LIVE_OBJECTS_SOURCE


def refs():
    return {'references': [
        {'id': 'customers', 'application': 'rey_db_admin', 'panel': 'data_workbench',
         'label': 'Customer query', 'modified': False, 'can_close': True},
        {'id': 'orders', 'application': 'rey_db_admin', 'panel': 'data_workbench',
         'label': 'Orders query', 'modified': True, 'can_close': False},
        {'id': 'workflow', 'application': 'workflow_app', 'panel': 'workflow_panel',
         'label': 'Prepare files', 'modified': False, 'can_close': True},
        {'id': 'loader', 'application': 'rey_db_admin', 'panel': 'loader_panel',
         'label': 'Load', 'modified': False, 'can_close': True}], 'selected': 'orders'}


def test_registered_source_requires_a_bound_owner():
    with pytest.raises(ConfigError, match='no bound owner'):
        rey_lib_sources()[LIVE_OBJECTS_SOURCE].children(SimpleNamespace(), {})


def test_application_panel_type_object_hierarchy_uses_canonical_node_identity():
    state = refs()
    asked = []
    def read(command):
        asked.append(command)
        return state
    projection = TreeProjection()
    root = {'node_key': 'governed-root', 'tree_object_key': LIVE_OBJECTS_SOURCE}
    projection.seed([root])
    traversal = TreeTraversal(None, rey_lib_sources(read))
    apps = traversal.children(None, root, projection)
    assert [row['label'] for row in apps] == ['rey_db_admin', 'workflow_app']
    panels = traversal.children(None, apps[0], projection)
    assert [row['label'] for row in panels] == ['Data Workbench', 'Loader Panel']
    objects = traversal.children(None, panels[0], projection)
    assert [row['label'] for row in objects] == ['Customer query', 'Orders query *']
    assert [row['subject_id'] for row in objects] == ['customers', 'orders']
    assert objects[1]['selected'] is True
    assert objects[1]['actionable_in'] == 'modified'
    assert all(row['node_key'] != row['subject_id'] for row in objects)
    assert all(row['parent_node_key'] == panels[0]['node_key'] for row in objects)
    assert all(command == {'command': 'live_references'} for command in asked)


def test_source_returns_unkeyed_records_and_rereads_membership():
    state = refs()
    source = rey_lib_sources(lambda _: state)[LIVE_OBJECTS_SOURCE]
    assert source.state_key({}) is None
    raw = source.children(None, {})
    assert all('node_key' not in row and 'parent_node_key' not in row for row in raw)
    assert all('buttons' not in row and 'panel_controls' not in row for row in raw)
    state['references'] = []
    assert source.children(None, {}) == []


def test_panel_scope_is_explicit_and_cannot_exist_without_its_application():
    source = rey_lib_sources(lambda _: refs())[LIVE_OBJECTS_SOURCE]
    with pytest.raises(ConfigError, match='application scope'):
        source.children(None, {'tree_configuration': {'panel': 'data_workbench'}})
    assert source.children(None, {'tree_configuration': {'application': 'ended-app'}}) == []


def test_hierarchy_order_is_declared_alphabetically_not_registration_order():
    state = refs()
    state['references'].reverse()
    source = rey_lib_sources(lambda _: state)[LIVE_OBJECTS_SOURCE]
    assert [row['label'] for row in source.children(None, {})] == ['rey_db_admin', 'workflow_app']
    objects = source.children(None, {'tree_configuration': {
        'application': 'rey_db_admin', 'panel': 'data_workbench'}})
    assert [row['label'] for row in objects] == ['Customer query', 'Orders query *']


def test_reordering_live_objects_keeps_the_clicked_key_bound_to_the_same_reference():
    state = refs()
    projection = TreeProjection()
    root = {'node_key': 'governed-root', 'tree_object_key': LIVE_OBJECTS_SOURCE}
    traversal = TreeTraversal(None, rey_lib_sources(lambda _: state))
    def rebuild():
        projection.seed([root])
        app = next(row for row in traversal.children(None, root, projection)
                   if row['label'] == 'rey_db_admin')
        panel = next(row for row in traversal.children(None, app, projection)
                     if row['label'] == 'Data Workbench')
        return traversal.children(None, panel, projection)
    first = {row['subject_id']: row['node_key'] for row in rebuild()}
    state['references'].append({'id': 'new', 'application': 'rey_db_admin', 'panel': 'data_workbench',
                                'label': 'A new query', 'modified': False, 'can_close': True})
    state['references'][1]['label'] = 'Renamed orders'
    second = {row['subject_id']: row['node_key'] for row in rebuild()}
    assert second['orders'] == first['orders']
    assert second['customers'] == first['customers']
    assert projection.record(first['orders'])['subject_id'] == 'orders'
    state['references'] = [row for row in state['references'] if row['id'] != 'customers']
    rebuild()
    assert projection.record(first['customers']) is None
    assert projection.record(first['orders'])['subject_id'] == 'orders'
