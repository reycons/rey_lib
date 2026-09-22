"""How source records become the records that are loaded.

The middle of source -> transform -> sink. A ``DataFile`` says what a file
physically contains; a ``DataTransform`` says what the records produced from
it contain; a ``DataLoader`` puts those somewhere.

Why this is a separate object from the file
-------------------------------------------
Because the two answers genuinely differ. One configured feed in this estate
declares **56 output columns over a 47-field file** -- three constants, five
context-derived values and a hash have no physical source at all. A file
cannot describe them, so ``logical_schema`` is not a question a file can
answer.

What is here today, and what is not
-----------------------------------
**The load path applies no transformation.** ``transformer.transform_row`` --
the mapper that applies column mapping, constants, context values and hashes
-- is called only by the TRANSFORM STAGE, which runs earlier and writes its
output to a file. By the time a load runs, that has already happened.

So the load path's transform is IDENTITY, and ``IdentityTransform`` is the
whole of what this module implements. The mapping transform belongs to the
transform stage and is its own work; putting a second implementation here
would be inventing one where the estate already has one.

An explicit identity rather than letting a loader cope with the absence of a
transform: the pipeline then has one shape whether or not anything is
configured, and the direct CLI path -- file, table, connection, no transform
at all -- is the same three objects as a configured feed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from rey_lib.profiling.file_profiler import infer_sql_type

__all__ = ["DataTransform", "IdentityTransform"]

#: Declared transform type -> the neutral SQL type it produces.
#:
#: The neutral vocabulary every backend understands through DBAdapter --
#: VARCHAR(n), INTEGER, DECIMAL(p,s), DATE. Backend-specific mapping happens
#: inside each provider, not here.
_TRANSFORM_TYPE_MAP: dict[str, str] = {
    "date":       "DATE",
    "datetime":   "DATETIME2",
    "time":       "TIME",
    "regex_date": "DATE",
    "numeric":    "DECIMAL(18, 6)",
}


class DataTransform(ABC):
    """One mapping from source records to logical records."""

    @abstractmethod
    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the logical records these source records produce."""

    @abstractmethod
    def logical_schema(
        self,
        records: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """Return ``(column, sql_type)`` for the records produced.

        Ordered, because the order is what an insert is built from.
        """


class IdentityTransform(DataTransform):
    """Records pass through unchanged -- the load path's transform today.

    It is not a null object: it still answers ``logical_schema``, and that
    answer is what a destination is created from.
    """

    def __init__(
        self,
        column_transforms: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Hold the declared per-column transforms, where config declares any.

        Args:
            column_transforms: Output column -> its inline transform entry,
                already resolved to plain data. The RESOLVED map rather than a
                config Namespace, so this object needs no knowledge of how
                configuration is shaped or where it came from.
        """
        self.column_transforms = column_transforms or {}

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the records unchanged.

        The load stage reads a file the transform stage already produced, so
        there is nothing left to apply.
        """
        return records

    def logical_schema(
        self,
        records: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """Return ``(column, sql_type)`` pairs for these records.

        A DECLARED type wins where configuration states one; otherwise the
        type is inferred from the values, and failing that the column is a
        VARCHAR wide enough for what was observed.

        **Columns come from the FIRST record.** That is what the loader has
        always done, and nothing here changes it -- which schema is
        authoritative is a separate decision, deliberately not taken while
        this is only being moved.

        Args:
            records: The records to be loaded.

        Returns:
            Ordered ``(column_name, sql_type)`` pairs, empty for no records.
        """
        if not records:
            return []

        columns = list(records[0].keys())

        # Widest observed value per column, for varchar sizing.
        max_lengths: dict[str, int] = {}
        for record in records:
            for column, value in record.items():
                length = len(str(value)) if value is not None else 0
                if column not in max_lengths or length > max_lengths[column]:
                    max_lengths[column] = length

        column_defs: list[tuple[str, str]] = []
        for column in columns:
            declared = self.column_transforms.get(column, {})
            declared_type = declared.get("type", "") if declared else ""
            cast_to = declared.get("cast_to", "") if declared else ""

            if declared_type in _TRANSFORM_TYPE_MAP:
                sql_type = _TRANSFORM_TYPE_MAP[declared_type]
            elif declared_type == "regex_extract" and cast_to in ("float", "double"):
                sql_type = "DECIMAL(18, 6)"
            elif declared_type == "regex_extract" and cast_to in ("int", "integer"):
                sql_type = "INTEGER"
            else:
                values = [
                    str(record.get(column, "") or "").strip()
                    for record in records
                    if str(record.get(column, "") or "").strip()
                ]
                inferred = infer_sql_type(values) if values else None
                if inferred:
                    sql_type = inferred
                else:
                    observed = max_lengths.get(column, 0)
                    sql_type = f"VARCHAR({max(observed + 10, 20)})"

            column_defs.append((column, sql_type))

        return column_defs
