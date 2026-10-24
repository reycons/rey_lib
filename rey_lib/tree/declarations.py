"""What each kind of node can do: the governed declaration authority (#1165).

``tree.node_type_action_vw`` resolves a node type into the actions it offers --
where, in what order, in which state, and the identity each declares::

    tree.node_type            what a node is, and whether it takes the selection
    tree.action               the settings an action is built from
    tree.node_type_action     which actions a type offers, where, in what order
      -> tree.node_type_action_vw
      -> NodeTypeActionDataObject   every node type's declarations
      -> offered_actions            what one record offers, in its state
      -> interacted_declaration     the declaration a reported interaction names
      -> offered / declared_fact    one declaration, applied to one record

The tree data objects all name ``tree.node_type_action`` as the answer to what a
node can do; this is where that answer is read and interpreted. It is tree-domain
meaning only: what a record's type declares, whether a placement applies to the
record's state, and the identity a declaration binds the record to -- an
unsigned plain fact. How that fact crosses a browser boundary, which process it
launches and what presents it are not this module's and appear nowhere in it.

**It is not a second registry.** It carries no executable and no predicate:
which node offers what is the database's answer, read through the view alone --
the tables beneath it are never queried from here, as the tree is read through
``tree_vw`` alone.
"""

from __future__ import annotations

from typing import Any, Mapping

from rey_lib.db.connection import shared_connection
from rey_lib.db.database_objects import DatabaseView
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "BUTTON_PLACEMENT",
    "FIELD_REFERENCE",
    "NODE_TYPE_ACTION_VIEW_NAME",
    "NodeTypeActionDataObject",
    "PANEL_PLACEMENT",
    "TEXT_CLICK_PLACEMENT",
    "declaration",
    "declared_fact",
    "interacted_declaration",
    "offered",
    "offered_actions",
]

#: The view the declarations are read from, in the tree schema.
NODE_TYPE_ACTION_VIEW_NAME = "node_type_action_vw"

#: The three placements a node type can give an action. The database enforces
#: the vocabulary; these are the words for reading it back. ``panel`` is the
#: panel header, where a control acting on the selection once belongs.
TEXT_CLICK_PLACEMENT = "text_click"
BUTTON_PLACEMENT = "button"
PANEL_PLACEMENT = "panel"

#: The one form of a field reference inside a declared target (#1143): an object
#: that is exactly ``{"$field": "<record column>"}`` stands for that column's
#: value, read from the record when a declaration is bound to it.
FIELD_REFERENCE = "$field"

_SELECT = """
SELECT node_type_key,
       selectable,
       placement,
       sort_order,
       requires_state,
       target,
       placement_is_enabled,
       action_key,
       action_label,
       action_icon,
       action_placement,
       route,
       action_target
  FROM tree.node_type_action_vw
 WHERE node_type_is_enabled
   AND (placement IS NULL OR (placement_is_enabled AND action_is_enabled))
 ORDER BY node_type_key, placement, sort_order
"""


class NodeTypeActionDataObject(DatabaseView):
    """``tree.node_type_action_vw``: the governed answer to what each node type can do.

    Everything a ``DatabaseView`` owns is retained, because it is one; what it
    adds is the read, shaped by node type. It interprets nothing beyond grouping
    the view's rows: whether a declaration applies to a record, and what it binds
    the record to, are :func:`offered` and :func:`declared_fact`.
    """

    def declarations(self, ctx: Any) -> dict[str, dict[str, Any]]:
        """Every enabled node type, with the actions it declares.

        Args:
            ctx: Finalized installation context, which resolves the connection.

        Returns:
            One entry per node type, keyed by its own key: ``selectable``
            (whether the node takes the selection -- a local behaviour, not an
            action), ``text_click`` (the one declaration a click on the row
            means, or None), ``buttons`` and ``panel_controls`` (in declared
            order, each with its ``sort_order``). Each declaration is
            :func:`declaration`'s. A disabled placement or action is absent
            rather than present and off.
        """
        handle = shared_connection(ctx, self.identity.connection).handle()
        rows = DatabaseQuery(_SELECT).rows(handle)

        declared: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("node_type_key") or "")
            entry = declared.setdefault(key, {
                "selectable": bool(row.get("selectable")),
                "text_click": None,
                "buttons": [],
                "panel_controls": [],
            })
            placement = row.get("placement")
            if placement == TEXT_CLICK_PLACEMENT:
                entry["text_click"] = declaration(row)
            elif placement == BUTTON_PLACEMENT:
                entry["buttons"].append({**declaration(row), "sort_order": row.get("sort_order")})
            elif placement == PANEL_PLACEMENT:
                entry["panel_controls"].append(
                    {**declaration(row), "sort_order": row.get("sort_order")})
        return declared


def declaration(row: Mapping[str, Any]) -> dict[str, Any]:
    """One declared action, as the view states it.

    ``target`` is the identity the declaration binds a record to -- its kind, the
    record columns its fields are read from, and anything it states outright.
    ``action_target`` is the panel the action names, where one still does.
    ``requires_state`` is the record state the placement needs, or None.
    """
    return {
        "action_key": row.get("action_key"),
        "label": row.get("action_label"),
        "icon": row.get("action_icon"),
        "placement": row.get("action_placement"),
        "route": row.get("route"),
        "action_target": row.get("action_target"),
        "requires_state": row.get("requires_state"),
        "target": row.get("target"),
    }


def offered(declared: Any, state: str) -> bool:
    """Whether one declaration applies to a record in ``state``.

    A declaration requiring no state always applies; one that requires a state
    applies where the record is in it -- and is absent otherwise rather than
    present and disabled. No declaration never applies.
    """
    if not declared:
        return False
    required = str(declared.get("requires_state") or "")
    return not required or required == state


def offered_actions(
    declarations: Mapping[str, Any], record: Mapping[str, Any],
) -> dict[str, Any]:
    """Everything one record's type offers it, in the record's state (#1218).

    The selection semantics a Tree node carries: whether it takes the selection,
    the declaration a click on its text names, and the buttons and panel controls
    it offers, each in declared order. A declaration whose required state the
    record is not in (``actionable_in``) is absent. Each is the unsigned
    declaration as :func:`declaration` states it; nothing here signs or opens.

    Args:
        declarations: Every node type's declarations
            (:meth:`NodeTypeActionDataObject.declarations`).
        record: The record, which names its ``node_type``.

    Returns:
        ``selectable``, ``text_click`` (one declaration or None), ``buttons`` and
        ``panel_controls``.
    """
    declared = declarations.get(str(record.get("node_type") or "")) or {}
    state = str(record.get("actionable_in") or "")
    clicked = declared.get("text_click")
    return {
        "selectable": bool(declared.get("selectable")),
        "text_click": clicked if offered(clicked, state) else None,
        "buttons": [button for button in declared.get("buttons") or []
                    if offered(button, state)],
        "panel_controls": [control for control in declared.get("panel_controls") or []
                           if offered(control, state)],
    }


def interacted_declaration(
    declarations: Mapping[str, Any],
    record: Mapping[str, Any],
    interaction: str = "",
    control: str = "",
) -> dict[str, Any] | None:
    """The declaration a reported interaction with one record names (#1218).

    The Tree-domain meaning of an interaction: a click on the record's text names
    its type's ``text_click`` declaration; a pressed control names the button or
    panel control its type declares under that ``action_key``. Either applies only
    where :func:`offered` says it does in the record's state (``actionable_in``),
    so a control never offered for this record matches nothing. What the matched
    declaration binds the record to is :func:`declared_fact`'s; nothing here signs
    or opens anything.

    Args:
        declarations: Every node type's declarations
            (:meth:`NodeTypeActionDataObject.declarations`).
        record: The record interacted with, which names its ``node_type``.
        interaction: :data:`TEXT_CLICK_PLACEMENT` where the record's text was clicked.
        control: The ``action_key`` a pressed control was offered under.

    Returns:
        The matched declaration, or None when the interaction names nothing this
        record's type offers in its state.
    """
    declared = declarations.get(str(record.get("node_type") or "")) or {}
    state = str(record.get("actionable_in") or "")
    if control:
        for candidate in [*(declared.get("buttons") or []),
                          *(declared.get("panel_controls") or [])]:
            if str(candidate.get("action_key")) == control:
                return candidate if offered(candidate, state) else None
        return None
    if interaction == TEXT_CLICK_PLACEMENT:
        clicked = declared.get("text_click")
        return clicked if offered(clicked, state) else None
    return None


def declared_fact(
    declared: Mapping[str, Any], record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The identity one declaration binds one record to, as an unsigned plain fact.

    The declaration's ``target`` names a kind, the record column each of its
    ``fields`` is read from, and values it states outright -- which may name record
    fields inside them as ``{"$field": "<column>"}`` (#1143), read here so the fact
    holds values and no reference. A column reading wins over a stated value of
    the same name: the row is the more specific answer.

    Returns:
        ``{"kind": ..., ...}``, or None when the declaration names no target.

    Raises:
        ConfigError: When a field reference names a column the record lacks.
    """
    target = declared.get("target") or {}
    fields = target.get("fields") or {}
    if not target.get("kind") or not (fields or _references_a_field(target)):
        return None
    return {
        "kind": target["kind"],
        **{name: _bound(value, record) for name, value in target.items()
           if name not in ("kind", "fields")},
        **{field: record.get(column) for field, column in fields.items()},
    }


def _is_field_reference(value: Any) -> bool:
    """Whether a declared value is a field reference: exactly ``{"$field": <string>}``."""
    return (isinstance(value, dict) and set(value) == {FIELD_REFERENCE}
            and isinstance(value[FIELD_REFERENCE], str))


def _references_a_field(value: Any) -> bool:
    """Whether a declared value names a record field anywhere inside it."""
    if _is_field_reference(value):
        return True
    if isinstance(value, dict):
        return any(_references_a_field(item) for item in value.values())
    if isinstance(value, list):
        return any(_references_a_field(item) for item in value)
    return False


def _bound(value: Any, record: Mapping[str, Any]) -> Any:
    """A declared value with each field reference in it read from the record (#1143).

    Only an object that is exactly ``{"$field": "<column>"}`` is a reference.
    Objects and lists are walked; every other value is returned as stated. No
    dotted paths, defaults, expressions, interpolation or templating.

    Raises:
        ConfigError: When a reference names a column the record does not have.
    """
    if _is_field_reference(value):
        column = value[FIELD_REFERENCE]
        if column not in record:
            raise ConfigError(f"The declaration names the record field '{column}', "
                              "which this record does not have.")
        return record[column]
    if isinstance(value, dict):
        return {key: _bound(item, record) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound(item, record) for item in value]
    return value
