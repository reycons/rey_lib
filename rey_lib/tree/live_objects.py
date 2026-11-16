"""BFF live-reference metadata projected through the canonical Tree source seam.

The source owns panel-type grouping. The BFF owns only references;
TreeTraversal owns generated node identities and parentage.
"""
from __future__ import annotations

from typing import Any, Callable

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree.sources import LIVE_OBJECTS_SOURCE

__all__ = ["LiveObjectsDataObject"]


class LiveObjectsDataObject:
    """Declared panel type -> BFF-exposed live objects, across applications."""

    def __init__(self, request: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._request = request

    def state_key(self, _record: dict[str, Any]) -> None:
        """Membership is read from its owner on every expansion/refresh."""
        return None

    def children(self, _ctx: Any, record: dict[str, Any], _state: Any = None) -> list[dict[str, Any]]:
        """Return one configured level, without Tree keys or actions."""
        if self._request is None:
            raise ConfigError("Live Objects has no bound owner in this Tree runtime.")
        answer = self._request({'command': 'live_references'})
        entries = answer['references']
        selected = answer.get('selected')
        scope = dict(record.get('tree_configuration') or {})
        panel = scope.get('panel')
        if panel is None:
            return [self._group('live_panel', str(name).replace('_', ' ').title() or 'No declared panel',
                               {'panel': name})
                    for name in sorted({e['panel'] for e in entries}, key=str.casefold)]
        return [
            {'row_type': 'node', 'node_type': 'live_object', 'source_id': entry['id'],
             'label': entry['label'] + (' *' if entry['modified'] else ''),
             'has_children': False, 'is_enabled': True, 'selectable': True,
             'subject_id': entry['id'],
             'actionable_in': 'clean' if entry['can_close'] else 'modified',
             'selected': entry['id'] == selected}
            for entry in sorted(entries, key=lambda entry: (entry['label'].casefold(), entry['id']))
            if entry['panel'] == panel]

    def _group(self, node_type: str, label: str, scope: dict[str, Any]) -> dict[str, Any]:
        """Explicit source scope, like a filesystem directory's configuration."""
        return {'row_type': 'node', 'node_type': node_type, 'label': label,
                'source_id': scope['panel'],
                'has_children': True, 'is_enabled': True, 'selectable': False,
                'tree_object_key': LIVE_OBJECTS_SOURCE, 'tree_configuration': scope}
