"""The tree one reader is looking at, for as long as they are looking at it.

A projection is temporary runtime state layered on the persisted data tree. The
canonical records are read from the database; everything a source produces
lazily is created here and lives only until its consumer lets it go.

    build       canonical records only
    expand      append what the source answered
    reload      the projection is discarded, and rebuilt canonical-only

It holds both kinds, which is what lets one lookup answer for either: a key is
looked up, and the record found says what it is. **Nothing decides what a record
is by reading its key.** A key is opaque -- no prefix is tested, no separator is
counted -- and the projection is the only thing that knows.

A synthetic id has to be unique and stable for the life of one projection, and
unique across every projection one reader holds, because one lookup answers for
all of them. So it is composed the way a persisted key is, from the parent it
hangs beneath: ``5:5`` is a root, ``5:5:1`` is made beneath it, ``5:5:1:2``
beneath that.

Composing a key is not reading one. Nothing recovers a parent, a group or a
path from a key: parentage is a field of its own, and a record that was made
from a source keeps what it was made from, which is what is read again when it
is expanded.

The mechanism only (backlog 1036, 1054). How long a projection lives is its
consumer's: the Console's TreeProjections holds one per page session and tree
group; an AI inspection run holds one for as long as it runs.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["PUBLIC_NODE_FIELDS", "TreeProjection", "public_node"]


#: What the Tree states of a node outside its own domain (#1218): its identity,
#: parentage and display facts, and the unsigned declarations its type offers it
#: in its state (``offered_actions``). Named rather than derived from a record --
#: the traversal and ordering internals a record also carries (tree_object_key,
#: tree_configuration, expansion_tvw, page_size, sort_*) stay inside the Tree,
#: and a field arriving on a record must not leave the domain because something
#: upstream started carrying it.
PUBLIC_NODE_FIELDS = (
    "node_key",
    "parent_node_key",
    "label",
    "icon",
    "tone",
    "has_children",
    "is_enabled",
    "selectable",
    "text_click",
    "buttons",
    "panel_controls",
)


def public_node(record: dict[str, Any]) -> dict[str, Any]:
    """One node's public projection: exactly :data:`PUBLIC_NODE_FIELDS`.

    The Tree's own API, not a browser representation: what a consumer composes
    from it (a profile, its affordances) is the consumer's.

    Args:
        record: The record as the projection holds it, with what its type offers it.

    Returns:
        The declared fields, and nothing else the record carries.
    """
    return {field: record.get(field) for field in PUBLIC_NODE_FIELDS}


class TreeProjection:
    """One reader's view of one tree group."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._minted = 0
        self._source_state: dict[str, Any] = {}

    def seed(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Take the canonical records this tree starts from.

        They keep the ``node_key`` the projection composed for them. Seeding
        replaces what was held: a rebuild is a new projection of the same tree,
        not an addition to the old one.
        """
        self._records = {}
        self._minted = 0
        self._source_state = {}
        held = [dict(row) for row in rows]
        for record in held:
            self._records[str(record.get("node_key") or "")] = record
        return held

    def source_state(self, key: str) -> Any:
        """What a source already read for this reader, if it has.

        A source reads; the projection is what survives between requests. A
        connection's snapshot answers every level beneath it, so reading it
        again per level would enumerate the whole of it each time.

        Held under a key the source names, and gone when the projection is
        rebuilt, with everything else.
        """
        return self._source_state.get(str(key))

    def hold_source_state(self, key: str, state: Any) -> Any:
        """Keep what a source read, and hand it back."""
        self._source_state[str(key)] = state
        return state

    def record(self, key: str) -> dict[str, Any] | None:
        """The record one key stands for, or nothing if this projection has none."""
        return self._records.get(str(key))

    def hold(self, records: Iterable[dict[str, Any]], parent_key: str) -> list[dict[str, Any]]:
        """Take canonical records, which arrive with their identity already.

        They are held for the same reason the roots are: one lookup answers for
        any key, whatever produced the record behind it.
        """
        held = [dict(record) for record in records]
        for record in held:
            self._records[str(record.get("node_key") or "")] = record
        return held

    def append(self, records: Iterable[dict[str, Any]], parent_key: str) -> list[dict[str, Any]]:
        """Take what a source produced, giving each record its identity.

        The key is composed the way the view composes a persisted one: the
        parent's own key, extended by the next number this projection has to
        give. A root carries its group, so every key beneath it does too, and no
        two tabs of one reader can compose the same string -- which is what lets
        one lookup answer for a key without being told which tree it came from.

        It is never reissued while the projection lives, so a record that was
        created keeps the key it was created with even as siblings arrive beside
        it.

        Composed here and nowhere else, and read nowhere at all: parentage is
        written as its own field below rather than recovered from the key.
        """
        held: list[dict[str, Any]] = []
        for record in records:
            self._minted += 1
            made = dict(record)
            made["node_key"] = f"{parent_key}:{self._minted}"
            made["parent_node_key"] = parent_key
            self._records[made["node_key"]] = made
            held.append(made)
        return held
