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
from dataclasses import dataclass
from typing import Any

from rey_lib.files.data_file.base import DataFileStructureError
from rey_lib.profiling.file_profiler import infer_sql_type

__all__ = [
    "IDENTITY_EXECUTION",
    "DataTransform",
    "ExecutionForm",
    "IdentityTransform",
]

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


@dataclass(frozen=True)
class ExecutionForm:
    """An execution form EQUIVALENT to a transform's row behaviour.

    The invariant is equivalence, not speed and not nativeness. A form that
    some engine could execute but that does something different from
    ``transform`` is the failure this type exists to make impossible to
    describe by accident.

    NEUTRAL BY CONSTRUCTION. It names no engine, carries no statement, and
    holds nothing about how anything runs. What a consumer does with it is
    the consumer's; deciding whether it is worth using at all belongs to
    whatever is executing, not here.

    **NO FIELDS YET, and that is deliberate.** A column list is earned by the
    first transform whose ROW behaviour projects; a predicate by the first
    that filters. Describing shapes no implementation has would be guessing
    at semantics nothing can check -- and the guess would be consumed as
    though it had been verified.
    """


#: The one form that exists today: every row, every column, unchanged.
#:
#: A single value rather than a per-call construction, because there is one
#: identity semantic and two instances of it would invite a comparison that
#: means nothing.
IDENTITY_EXECUTION = ExecutionForm()


class DataTransform(ABC):
    """One mapping from source records to logical records."""

    @abstractmethod
    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the logical records these source records produce.

        THE GUARANTEED FORM. Every transform can do this; ``execution_form``
        is the optional alternative, and this remains the fallback whenever
        that one cannot be used.
        """

    @abstractmethod
    def logical_schema(
        self,
        records: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """Return ``(column, sql_type)`` for the records produced.

        Ordered, because the order is what an insert is built from.
        """

    def execution_form(self) -> ExecutionForm | None:
        """An equivalent non-row form for this transform, if it has one.

        ``None`` means row-by-row only, which is every transform until it
        says otherwise. Whether an existing form is usable -- or preferable
        -- is not answered here: that depends on where the records come from,
        where they are going and how many there are, none of which a
        transform can see.

        CONCRETE, NOT ABSTRACT. An implementation that has no such form
        should not have to say so, and every transform written before this
        existed keeps working unchanged.

        Returns:
            The form, or None. An implementation returning one is promising
            it is EQUIVALENT to what ``transform`` does to the same records.
        """
        return None


class IdentityTransform(DataTransform):
    """Records pass through unchanged -- the load path's transform today.

    It is not a null object: it still answers ``logical_schema``, and that
    answer is what a destination is created from.
    """

    def __init__(
        self,
        column_transforms: dict[str, dict[str, Any]] | None = None,
        columns: list[str] | None = None,
    ) -> None:
        """Hold what configuration declared about the produced records.

        Args:
            column_transforms: Output column -> its inline transform entry,
                already resolved to plain data.
            columns: The declared output columns IN ORDER, or None when
                configuration declares none. When given they are
                AUTHORITATIVE -- they are the schema, and records that do not
                match them are refused.

        Both are RESOLVED values rather than a config Namespace, so this
        object needs no knowledge of how configuration is shaped, where it
        came from, or which shapes are valid.
        """
        self.column_transforms = column_transforms or {}
        self.columns = columns

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the records unchanged.

        The load stage reads a file the transform stage already produced, so
        there is nothing left to apply.
        """
        return records

    def execution_form(self) -> ExecutionForm:
        """Identity: every row, every column, unchanged.

        **``self.columns`` IS DELIBERATELY NOT CONSULTED**, and that is the
        subtle part. Declared columns are authoritative for
        ``logical_schema`` and for what a destination is created from -- they
        are NOT the semantics of ``transform``, which returns records
        untouched, including keys configuration never mentioned.

        So describing this as "project these columns" would be describing a
        different operation. A record ``{id, name, extra}`` against declared
        ``(id, name)`` keeps ``extra`` here; a projection would drop it.

        It LOOKS safe because ``logical_schema`` refuses that mismatch -- but
        that refusal lives in another method, runs after this one, and on a
        path that never materialises records may not run at all. Which is
        exactly where a form gets used. So the only semantics provably equal
        to this transform's is identity.
        """
        return IDENTITY_EXECUTION

    def logical_schema(
        self,
        records: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """Return ``(column, sql_type)`` pairs for these records.

        A DECLARED type wins where configuration states one; otherwise the
        type is inferred from the values, and failing that the column is a
        VARCHAR wide enough for what was observed.

        **CONFIGURED COLUMNS ARE AUTHORITATIVE when declared.** They describe
        the records this transform produces -- including fields with no
        physical source at all -- so where configuration states them, they
        are the schema and its order. The records must match; see below.

        Where configuration declares none, the FIRST RECORD decides, which is
        what the loader has always done and what the direct single-file path
        relies on.

        Args:
            records: The records to be loaded.

        Returns:
            Ordered ``(column_name, sql_type)`` pairs, empty for no records.

        Raises:
            DataFileStructureError: When configuration declares columns and
                these records do not carry exactly those, in that order.
        """
        if not records:
            return []

        columns = self._columns_for(records)

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

    def _columns_for(self, records: list[dict[str, Any]]) -> list[str]:
        """Return the authoritative column list for these records.

        **Ordered comparison, not a set comparison.** Order is load-bearing
        here: a destination created in one order is later validated against a
        file header in another, and that mismatch would surface on the NEXT
        load rather than this one. The transform stage can produce a different
        order from the configured one -- a hash column is computed last, so a
        hash declared anywhere but last reorders the output -- which is
        exactly the case this catches.

        Raises:
            DataFileStructureError: When the records do not match what
                configuration declared. Raised BEFORE any DDL or insert. Left
                to the insert it would arrive as a missing-column database
                error after the table had already been created -- a database
                error for a configuration or file-drift problem.
        """
        actual = list(records[0].keys())

        if self.columns is None:
            return actual

        if actual == self.columns:
            return self.columns

        missing = [name for name in self.columns if name not in actual]
        extra = [name for name in actual if name not in self.columns]
        if missing or extra:
            detail = ", ".join(
                part for part in (
                    f"missing {missing}" if missing else "",
                    f"unexpected {extra}" if extra else "",
                ) if part
            )
        else:
            detail = (
                f"same columns in a different order -- configured "
                f"{self.columns}, found {actual}"
            )

        raise DataFileStructureError(
            f"the records do not match the configured columns: {detail}.",
            validation_name="configured_columns",
        )
