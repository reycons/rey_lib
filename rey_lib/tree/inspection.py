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

__all__ = ["EXPANSION_LIMIT", "INSPECTED_FIELDS", "inspected"]


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
