"""Which source owns a node's children, and admitting them into a projection.

Moved from the Console's NodeDataObject (backlog 1054): the non-presentation half
of reading a node's children -- route to the source that owns them, read it
(through the state the projection already holds for it), cap, and admit. What a
node can DO -- its actions, buttons and controls -- is presentation and stays
with the consumer, which hands it in as ``assemble``.

    persisted record  -> the persisted source       tree.tree_vw
    declared source   -> that source's implementation

**The record decides, never the key.** What a record is sourced from is on the
record (``tree_object_key``); a record naming nothing, or a name that is not a
declared source, is persisted. A declared source this process has no
implementation for is refused -- never read as persisted.

Not a tree model: no node meaning, no presentation, no lifetime. The projection
it admits into is the consumer's.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree.sources import DECLARED_SOURCES

__all__ = ["TreeTraversal"]

#: The consumer's presentation step: given the admitted records, give each what
#: it needs to be drawn -- updating the records it is handed. Runs after the cap
#: and after admission, as the tree's expansion path states: the source returns
#: children, the projection admits them, then the consumer assembles them.
Assemble = Callable[[Any, list[dict[str, Any]], Any], list[dict[str, Any]]]


def _capped(
    records: list[dict[str, Any]],
    limit: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Cut a list to a ceiling, and say whether cutting it lost anything.

    The count is taken from what the source produced, so nothing past the ceiling
    has to be admitted in order to discover that it exists.

    Args:
        records: What the source produced.
        limit: The ceiling, or None for no ceiling at all.

    Returns:
        The records to admit, and whether any were dropped.
    """
    if limit is None or len(records) <= limit:
        return records, False
    return records[:limit], True


class TreeTraversal:
    """The children of any canonical node record, whatever produced it.

    Args:
        persisted: What answers for a persisted record's children
            (``children(ctx, record)``).
        sources: The implementations of the declared sources this process has,
            by declared name. A declared source missing here is refused. Held,
            not copied: it is the consumer's own map, read at every expansion as
            it always was.
    """

    def __init__(self, persisted: Any, sources: Mapping[str, Any]) -> None:
        self._persisted = persisted
        self._sources = sources

    def children(
        self,
        ctx: Any,
        record: dict[str, Any],
        projection: Any = None,
        fresh: bool = False,
        assemble: Assemble | None = None,
    ) -> list[dict[str, Any]]:
        """Whatever hangs beneath one record.

        Args:
            ctx: The installation context the source is read in.
            record: The parent, as the projection holds it.
            projection: The reader's projection, which gives a created record
                its identity and holds every record for the next lookup.
            fresh: Read the source again rather than answer from held state.
            assemble: The consumer's presentation step, if it has one.

        Returns:
            The children, in the order their source gave them.
        """
        children, _truncated = self._children(ctx, record, projection, None, fresh, assemble)
        return children

    def bounded_children(
        self,
        ctx: Any,
        record: dict[str, Any],
        projection: Any,
        limit: int,
        assemble: Assemble | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """The same children, capped, and whether the cap dropped any.

        **What is returned is what was admitted.** The cap is applied before the
        projection is told anything, so a reader can only go on to expand what
        it was actually shown. Admitting the rest and returning fewer would
        leave the projection authorizing records nobody ever saw.

        Args:
            ctx: The installation context the source is read in.
            record: The parent, as the projection holds it.
            projection: The reader's projection.
            limit: The most children to admit and return.
            assemble: The consumer's presentation step, if it has one.

        Returns:
            The admitted children, and whether any were dropped to fit.
        """
        return self._children(ctx, record, projection, limit, False, assemble)

    def _children(
        self,
        ctx: Any,
        record: dict[str, Any],
        projection: Any,
        limit: int | None,
        fresh: bool,
        assemble: Assemble | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Both callers' one implementation: produce, cap, admit, then assemble.

        The cap sits before admission in both branches, which is the only place
        it can sit: ``hold`` and ``append`` are what make a record expandable,
        so anything they are given is reachable whatever is returned afterwards.

        Assembly comes last, after admission, as the expansion path states.
        What is assembled is the record as the projection holds it -- a created
        record with the identity and explicit parentage ``append`` gave it.
        Assembly updates the admitted records themselves, so what the
        projection holds is what was drawn.

        Raises:
            ConfigError: when the record names a declared source this process
                has no implementation for.
        """
        parent_key = str(record.get("node_key") or "")
        declared = str(record.get("tree_object_key") or "")
        source = self._sources.get(declared)
        if source is None and declared in DECLARED_SOURCES:
            raise ConfigError(
                f"Tree source '{declared}' cannot be read here, so node "
                f"'{parent_key}' is not expanded."
            )

        def presented(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return assemble(ctx, records, projection) if assemble is not None else records

        if source is None:
            # Persisted: these arrive with their identity already composed.
            produced = self._persisted.children(ctx, record)
            admitted, truncated = _capped(produced, limit)
            if projection is not None:
                admitted = projection.hold(admitted, parent_key)
            return presented(admitted), truncated
        produced = source.children(
            ctx, record, self._state(ctx, source, record, projection, fresh),
        )
        admitted, truncated = _capped(produced, limit)
        if projection is not None:
            # A created record is given its identity and parentage here.
            admitted = projection.append(admitted, parent_key)
        return presented(admitted), truncated

    @staticmethod
    def _state(
        ctx: Any,
        source: Any,
        record: dict[str, Any],
        projection: Any,
        fresh: bool = False,
    ) -> Any:
        """Whatever a source already read for this record's level.

        Read once and held by the projection rather than by the source: a feeder
        that kept a cache of its own would be a second thing surviving between
        requests, and one already does. A source that needs nothing held names
        no key and is handed nothing. Fresh skips the held answer, reads the
        source again and holds that read in its place.
        """
        key = source.state_key(record)
        if key is None:
            return None
        if projection is None:
            return source.state(ctx, record)
        if not fresh:
            held = projection.source_state(key)
            if held is not None:
                return held
        return projection.hold_source_state(key, source.state(ctx, record))
