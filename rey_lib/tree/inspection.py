"""What a model is allowed to see of the tree, and how far one expansion reaches.

Moved from the Console's tree inspection (backlog 1054): the tree owner's
disclosure rule, not AI tool metadata (#742).

    what reaches the provider is a fixed set of fields
    what may be expanded is what was already shown

The second is why the ceiling is applied by the traversal before admission
(``TreeTraversal.bounded_children``): admitting every child and returning the
first fifty would leave the projection authorizing the rest.
"""

from __future__ import annotations

from typing import Any

from rey_lib.errors.error_utils import AppError

__all__ = ["EXPANSION_LIMIT", "INSPECTED_FIELDS", "expand", "inspected", "subject"]


#: What a model is shown of any node. Named rather than derived from a record:
#: a field arriving on a record must not become visible to a provider because
#: something upstream started carrying it.
INSPECTED_FIELDS = (
    "node_key",
    "parent_node_key",
    "label",
    "node_type",
    "has_children",
    "subject_type",
    "subject_id",
)

#: The most children one expansion admits and returns. Server-side, and not
#: reachable by anything the model can say.
#:
#: **An initial safety ceiling, not a measured one.** Nothing here was derived
#: from how wide real branches are or how much a turn can carry -- there is no
#: measurement yet, and a number presented as if there were would be worse than
#: an admitted guess. 50 is deliberately conservative: it is the width the
#: admission invariant is proved at, and fifty serialized records is a size a
#: turn can hold without the result crowding out the reasoning.
#:
#: Raise it when something measured says to. What must not change with it is
#: the ordering -- the cap is applied before admission, so whatever this number
#: becomes, the model can still only expand what it was actually shown.
EXPANSION_LIMIT = 50


def inspected(record: dict[str, Any]) -> dict[str, Any]:
    """One node as the model is allowed to see it.

    Args:
        record: The full server-side record, as the projection holds it.

    Returns:
        Exactly the declared fields, and nothing else the record carries.
    """
    return {field: record.get(field) for field in INSPECTED_FIELDS}


def subject(record: dict[str, Any]) -> str:
    """The node an inspection is about, as the opening text the model reads.

    Through the same serializer every expansion uses (backlog 1055, from the
    Console's former _subject): a subject exempt from the rule its own children
    obey would disclose on the first turn whatever the rule was written to
    withhold. What the reader asked travels separately, as the task's input.

    Args:
        record: The trusted server-side record for the subject.

    Returns:
        The text, carrying only the declared fields.
    """
    shown = inspected(record)
    lines = [f"{field}: {shown[field]}" for field in INSPECTED_FIELDS]
    return "You are inspecting one node of a tree.\n\nThe selected node:\n" + "\n".join(lines)


def expand(ctx: Any, traversal: Any, projection: Any, node_key: Any) -> dict[str, Any]:
    """Expand one node an inspection has shown, or say why not (backlog 1055).

    The inspection rules, moved from the Console's former tree_expand_runner:
    only a key this inspection's projection holds -- one it was shown -- may be
    expanded, only a node that has children, and at most EXPANSION_LIMIT of them,
    capped before admission so the model can go on to expand only what it saw.
    What comes back is each child's inspected fields and nothing else.

    Args:
        ctx: The installation context the sources are read in.
        traversal: The TreeTraversal that owns child sourcing.
        projection: This inspection's own projection, seeded with its subject.
        node_key: The node the model asked for.

    Returns:
        ``{"children": [...], "truncated": bool}``.

    Raises:
        AppError: when no node is named, the node was never shown in this
            inspection, or it has no children -- a refusal the model is told and
            may adapt to; or as the traversal raises for a source not readable
            here.
    """
    key = str(node_key or "")
    if not key:
        raise AppError("A node_key is required.")
    record = projection.record(key)
    if record is None:
        raise AppError(
            "That node has not been shown in this inspection. "
            "Expand from a node you were given."
        )
    if not record.get("has_children"):
        raise AppError("That node has no children.")
    children, truncated = traversal.bounded_children(ctx, record, projection, EXPANSION_LIMIT)
    return {"children": [inspected(child) for child in children], "truncated": truncated}
