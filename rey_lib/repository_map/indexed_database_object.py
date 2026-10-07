"""A database object as the code index knows it (backlog 696).

The code index is knowledge Rey publishes ABOUT database objects -- whether an
object was indexed, which objects it relates to, and which code reaches it
through a procedure-map binding. It is not the database object's own state:
``DatabaseTable``, ``DatabaseView`` and the rest say what the database says,
and none of them depends on this. A caller that wants both combines them.

``IndexedDatabaseObject`` is the one owner of these reads. Its statements are
private to it; there is no general way to query the code index through it.
Every read is asked by the object's natural identity -- schema, name, type and
signature -- so two overloads of a routine are never answered as one.

The caller hands the connection the index was published to, as every database
object is handed its connection: where the index lives is the caller's
configuration, not this object's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["IndexedCodeReach", "IndexedDatabaseObject", "IndexedRelation"]

#: One object's related objects, in one direction. Natural identity on both
#: sides, signature included: matching on name alone would merge two overloads
#: into one answer.
_RELATED_SQL = """
    SELECT other.schema_name, other.object_name, other.object_type,
           other.signature, other.definition, r.reference_kind
    FROM   code.db_reference_vw AS r
    JOIN   code.db_object_vw AS mine  ON mine.db_node_id  = r.{mine}_node_id
    JOIN   code.db_object_vw AS other ON other.db_node_id = r.{other}_node_id
    WHERE  mine.schema_name = :schema
      AND  mine.object_name = :object
      AND  mine.object_type = :object_type
      AND  COALESCE(mine.signature, '') = :signature
    ORDER  BY other.schema_name, other.object_name, other.signature
"""

#: The statement for each direction, fixed once: what this object depends on
#: (it is the source), or what depends on it (it is the target).
_RELATED: dict[bool, str] = {
    False: _RELATED_SQL.format(mine="source", other="target"),
    True: _RELATED_SQL.format(mine="target", other="source"),
}

#: Whether the index holds this object at all, asked by the same natural
#: identity the edges are asked by.
_INDEXED_SQL = """
    SELECT 1
    FROM   code.db_object_vw AS o
    WHERE  o.schema_name = :schema
      AND  o.object_name = :object
      AND  o.object_type = :object_type
      AND  COALESCE(o.signature, '') = :signature
    LIMIT  1
"""

#: Code that can reach one database object through a procedure-map binding.
#: Either the binding resolves to THIS object -- a routine, bound directly --
#: or to a routine that references it. The second clause is why a table has any
#: code at all: nothing binds control.batch_step, it binds end_step, which
#: writes it. One hop.
_REACHED_BY_CODE_SQL = """
    SELECT cov.installation, cov.map_name, c.repository_key, c.relative_path,
           c.qualified_name, c.binding_name
    FROM   code.db_code_reference_vw  AS c
    JOIN   code.db_bridge_coverage_vw AS cov ON cov.seam = c.seam
    JOIN   code.db_binding_vw         AS b
           ON  b.installation = cov.installation
           AND b.map_name     = cov.map_name
           AND b.binding_name = c.binding_name
           AND b.resolves_now
    JOIN   code.db_object_vw AS bound ON bound.db_node_id = b.db_node_id
    WHERE  EXISTS (
               SELECT 1 FROM code.db_object_vw AS mine
               WHERE  mine.schema_name = :schema
                 AND  mine.object_name = :object
                 AND  mine.object_type = :object_type
                 AND  COALESCE(mine.signature, '') = :signature
                 AND  (mine.db_node_id = bound.db_node_id
                       OR EXISTS (SELECT 1 FROM code.db_reference_vw AS r
                                  WHERE r.source_node_id = bound.db_node_id
                                    AND r.target_node_id = mine.db_node_id))
           )
    ORDER  BY cov.installation, cov.map_name, c.qualified_name
"""


@dataclass(frozen=True)
class IndexedRelation:
    """One object related to an indexed object, and how, as the index states it."""

    schema: str
    name: str
    object_type: str
    signature: str
    definition: str
    reference_kind: str


@dataclass(frozen=True)
class IndexedCodeReach:
    """One piece of code reaching an indexed object through a procedure-map binding."""

    installation: str
    map_name: str
    repository_key: str
    relative_path: str
    qualified_name: str
    binding_name: str


@dataclass(frozen=True)
class IndexedDatabaseObject:
    """A database object as the code index knows it, by its natural identity.

    ``object_type`` is the index's own word for the kind -- ``table``, ``view``,
    ``procedure``, ``function`` -- and ``signature`` is part of the identity:
    empty for an object that has none, never ignored for one that does.
    """

    schema: str
    name: str
    object_type: str
    signature: str = ""

    def is_indexed(self, conn: Any, adapter: Any = None) -> bool:
        """Whether the index has ever seen this object.

        Not the same answer as "nothing relates to it": an object absent from
        the index says nobody looked, and a caller must be able to tell.
        """
        return bool(self._read(conn, _INDEXED_SQL, adapter))

    def related(
        self, conn: Any, *, reverse: bool, adapter: Any = None,
    ) -> tuple[IndexedRelation, ...]:
        """One direction of this object's edges, as the index states them.

        ``reverse`` False: what this object depends on. True: what depends on
        it. An object the index has never seen relates to nothing here --
        ``is_indexed`` is what tells that apart from having no relations.
        """
        return tuple(
            IndexedRelation(
                schema=_text(row, "schema_name"),
                name=_text(row, "object_name"),
                object_type=_text(row, "object_type"),
                signature=_text(row, "signature"),
                definition=_text(row, "definition"),
                reference_kind=_text(row, "reference_kind"),
            )
            for row in self._read(conn, _RELATED[reverse], adapter)
        )

    def reached_by_code(
        self, conn: Any, adapter: Any = None,
    ) -> tuple[IndexedCodeReach, ...]:
        """Code that reaches this object through a procedure-map binding, one hop."""
        return tuple(
            IndexedCodeReach(
                installation=_text(row, "installation"),
                map_name=_text(row, "map_name"),
                repository_key=_text(row, "repository_key"),
                relative_path=_text(row, "relative_path"),
                qualified_name=_text(row, "qualified_name"),
                binding_name=_text(row, "binding_name"),
            )
            for row in self._read(conn, _REACHED_BY_CODE_SQL, adapter)
        )

    def _read(self, conn: Any, statement: str, adapter: Any) -> list[dict[str, Any]]:
        """Run one of this object's statements, bound to its whole identity."""
        if adapter is None:
            from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415 -- imports widely

            adapter = DBAdapter()
        rows = adapter.execute_sql(conn, statement, {
            "schema": self.schema,
            "object": self.name,
            "object_type": self.object_type,
            "signature": self.signature or "",
        }, "dataset_result")
        return [dict(row) for row in (rows or [])]


def _text(row: dict[str, Any], column: str) -> str:
    """A column as text; an absent or null value as empty."""
    value = row.get(column)
    return "" if value is None else str(value)
