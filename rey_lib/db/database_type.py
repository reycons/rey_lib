"""How a destination database represents a Rey logical type (backlog 690).

    LogicalType -> DatabaseType(provider) -> the destination's physical type

**THE ONE TRANSLATION.** Every destination type spelling Rey produces is an
entry in :data:`_PHYSICAL`, keyed by ``(provider, kind)``. Adding a provider or a
kind extends that one structure; no provider module keeps a map of its own, and
nothing else renders a type. Its only consumer is ``DatabaseTable``, which asks
for each column's type while composing CREATE and ALTER.

**NOT a database read.** ``DatabaseColumn.type`` is what an existing column is,
as its database reports it; this is what a new or changed column becomes.
"""

from __future__ import annotations

from dataclasses import dataclass

from rey_lib.data.logical_type import LogicalType
from rey_lib.errors.error_utils import ConfigError

__all__ = ["DatabaseType"]


#: ``(provider, kind) -> (bounded, unbounded)``: the physical type with the
#: logical type's bounds applied, and without them. ``{length}``, ``{precision}``
#: and ``{scale}`` are the LogicalType's own. The spellings are the ones each
#: provider received before this structure existed.
_PHYSICAL: dict[tuple[str, str], tuple[str, str]] = {
    ("postgres", "text"): ("VARCHAR({length})", "TEXT"),
    ("postgres", "integer"): ("INTEGER", "INTEGER"),
    ("postgres", "decimal"): ("DECIMAL({precision}, {scale})", "DECIMAL"),
    ("postgres", "date"): ("DATE", "DATE"),
    ("postgres", "datetime"): ("TIMESTAMP", "TIMESTAMP"),
    ("postgres", "time"): ("TIME", "TIME"),
    ("mysql", "text"): ("VARCHAR({length})", "TEXT"),
    ("mysql", "integer"): ("INT", "INT"),
    ("mysql", "decimal"): ("DECIMAL({precision}, {scale})", "DECIMAL"),
    ("mysql", "date"): ("DATE", "DATE"),
    ("mysql", "datetime"): ("DATETIME", "DATETIME"),
    ("mysql", "time"): ("TIME", "TIME"),
    ("sqlserver", "text"): ("NVARCHAR({length})", "NVARCHAR(MAX)"),
    ("sqlserver", "integer"): ("INT", "INT"),
    ("sqlserver", "decimal"): ("DECIMAL({precision}, {scale})", "DECIMAL"),
    ("sqlserver", "date"): ("DATE", "DATE"),
    ("sqlserver", "datetime"): ("DATETIME2", "DATETIME2"),
    ("sqlserver", "time"): ("TIME", "TIME"),
    # DuckDB's VARCHAR is unbounded and a declared length is not enforced, so a
    # bounded text column is VARCHAR as well.
    ("duckdb", "text"): ("VARCHAR", "VARCHAR"),
    ("duckdb", "integer"): ("INTEGER", "INTEGER"),
    ("duckdb", "decimal"): ("DECIMAL({precision}, {scale})", "DECIMAL"),
    ("duckdb", "date"): ("DATE", "DATE"),
    ("duckdb", "datetime"): ("TIMESTAMP", "TIMESTAMP"),
    ("duckdb", "time"): ("TIME", "TIME"),
}


@dataclass(frozen=True)
class DatabaseType:
    """One destination database's type semantics."""

    provider: str

    def column_type(self, logical: LogicalType) -> str:
        """The physical type a column of this logical type has on this database.

        Bounds apply where the logical type carries them: a ``text`` with a
        ``length``, a ``decimal`` with a ``precision`` and ``scale``.

        Raises:
            ConfigError: If this provider has no type for that kind.
        """
        entry = _PHYSICAL.get((self.provider, logical.kind))
        if entry is None:
            raise ConfigError(
                f"DatabaseType: provider '{self.provider}' has no type for "
                f"logical kind '{logical.kind}'."
            )
        bounded, unbounded = entry
        bounds_given = (
            logical.length is not None if logical.kind == "text"
            else logical.precision is not None and logical.scale is not None
        )
        template = bounded if bounds_given else unbounded
        return template.format(
            length=logical.length, precision=logical.precision, scale=logical.scale,
        )
