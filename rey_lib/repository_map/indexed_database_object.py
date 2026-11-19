"""A database object as the code index knows it (backlog 696).

The code index is knowledge Rey publishes ABOUT database objects -- whether an
object was indexed, which objects it relates to, and which code reaches it
through a procedure-map binding. It is not the database object's own state:
``DatabaseTable``, ``DatabaseView`` and the rest say what the database says,
and none of them depends on this. A caller that wants both combines them.

``IndexedDatabaseObject`` is the one owner of these reads; there is no general
way to query the code index through it. Every read is asked by the object's
natural identity -- schema, name, type and signature -- so two overloads of a
routine are never answered as one.

The reads are governed functions (#541) -- code.f_indexed_object_exists,
f_indexed_object_related, f_indexed_object_reached_by_code -- bound in the
control procedure map and asked through the ``Control`` the caller hands over,
as every governed read is: no statement is written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["IndexedCodeReach", "IndexedDatabaseObject", "IndexedRelation"]


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

    @property
    def label(self) -> str:
        """Its qualified display name, independent of its overload signature."""
        return f"{self.schema}.{self.name}"

    def is_indexed(self, control: Any) -> bool:
        """Whether the index has ever seen this object.

        Not the same answer as "nothing relates to it": an object absent from
        the index says nobody looked, and a caller must be able to tell.
        """
        return control.indexed_object_exists(*self._identity())

    def related(self, control: Any, *, reverse: bool) -> tuple[IndexedRelation, ...]:
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
            for row in control.indexed_object_related(*self._identity(), reverse)
        )

    def reached_by_code(self, control: Any) -> tuple[IndexedCodeReach, ...]:
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
            for row in control.indexed_object_reached_by_code(*self._identity())
        )

    def _identity(self) -> tuple[str, str, str, str]:
        """Its whole natural identity, as every read is asked by it: the signature too."""
        return self.schema, self.name, self.object_type, self.signature or ""


def _text(row: dict[str, Any], column: str) -> str:
    """A column as text; an absent or null value as empty."""
    value = row.get(column)
    return "" if value is None else str(value)
