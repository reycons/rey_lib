"""Where logical records go.

The sink of source -> transform -> sink. A ``DataFile`` says what a file
contains, a ``DataTransform`` says what the records made from it contain, and
this puts those records in a table.

It owns DESTINATION POLICY and nothing else: may the table be created, insert
the rows, and decide whether a failure is worth retrying. What a table IS --
whether it exists, creating it from a logical schema, widening a column -- is
``DatabaseTable``'s (backlog 690); every other database operation goes through
``DBAdapter``. No SQL is written here and no provider is named.

What it deliberately does not have
----------------------------------
**No ``ctx``.** The application context is consumed where these objects are
constructed and does not travel down with them.

**No connection.** The shared connection outlives any single load and is held
by every other consumer of the same name, so holding one would claim an
ownership this object does not have. It arrives per call.

**No destination identity.** Which object the records go to is the target
data object's, and it arrives per call for the same reason the connection
does. Holding a schema and a table beside a target that already names them
would be one endpoint represented twice.

**No configuration to interpret.** The target arrives resolved; parsing
``"database.schema.table"`` is configuration interpretation and happens at the
construction boundary.

**No module globals.** The adapter is injected, so substituting one is passing
an argument rather than patching a module attribute.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from rey_lib.data.logical_type import LogicalType
# _beside: the one naming rule for a table's short-lived companions, shared
# with DatabaseTable.replace_table so the two cannot drift.
from rey_lib.db.database_objects import DatabaseTable, _beside
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = ["DataLoader", "adapter_destination"]

_logger = get_logger(__name__)

#: "The caller did not ask" -- distinct from None, which means "the caller
#: asked and the destination is not there". Without this sentinel the two
#: would collapse and an absent destination would be re-checked needlessly,
#: or a caller's answer of None would be mistaken for not having asked.
_UNASKED: Any = object()

#: How much room a widened text column gets beyond the longest value that
#: overflowed it.
_COLUMN_EXPAND_BUFFER = 10


def adapter_destination(target: Any) -> tuple[str, str]:
    """Render one database object identity as the adapter's ``(schema, table)``.

    THE ONE TRANSLATION POINT, and it belongs here because this is the object
    that talks to the adapter. The identity keeps its parts apart --
    ``catalog`` and ``schema`` are separate fields, and it carries no rendered
    reference -- while the adapter takes a single schema argument that is
    ``database.schema`` where a backend qualifies that way.

    Args:
        target: A database object identity: ``catalog``, ``schema``, ``name``.

    Returns:
        ``(schema, table)`` exactly as the adapter has always received it, so
        what reaches the database is unchanged by the identity having parts.
    """
    catalog = str(getattr(target, "catalog", "") or "")
    schema = str(getattr(target, "schema", "") or "")
    return (f"{catalog}.{schema}" if catalog else schema, str(target.name))


class DataLoader:
    """One destination, and the policy for putting records into it."""

    def __init__(
        self,
        *,
        adapter: Any,
        create_destination: bool = False,
        replace_destination: bool = False,
        recreate_destination: bool = False,
    ) -> None:
        """Hold the policies that govern loading, and nothing about where.

        **NO DESTINATION IDENTITY.** Which object the records go to is the
        target data object's, and it arrives per call -- the same reasoning
        that has always kept the connection out of here. Holding a schema and
        a table beside a target that already names them would be the same
        endpoint twice, which is exactly what passing objects removes.

        What remains is OPERATION POLICY: what this load may do, which is not
        a property of the object it writes to. An endpoint carrying
        ``create_destination`` would be meaningless when read from.

        Args:
            adapter: The ``DBAdapter`` every database operation goes through.
                Injected rather than reached for, so this object has no
                ambient state and a test substitutes one by passing it.
            create_destination: Whether an ABSENT destination may be created
                from the records. False means the table must already exist,
                which is what the loader did before the setting existed.
            replace_destination: Whether the destination's EXISTING CONTENTS
                are removed before these records are written. False means they
                are added to, which is what a load has always done and what an
                undeclared load still means.
            recreate_destination: Whether the destination is DESTROYED AND
                BUILT AGAIN before these records are written -- dropped, then
                created from them. The one policy here that is destructive at
                schema level: what the table owns goes with it, and nothing
                rebuilds it. Distinct from ``replace_destination``, which keeps
                the table it empties.
        """
        self.adapter = adapter
        self.create_destination = create_destination
        self.replace_destination = replace_destination
        self.recreate_destination = recreate_destination

    def destination_columns(self, conn: Any, target: Any) -> list[str] | None:
        """Return the destination's columns, or None when it does not exist.

        **``None`` is not ``[]``.** An empty list would mean a table with no
        columns, which nothing matches; ``None`` means there is no table yet.
        The same distinction the file-structure contract uses, and for the
        same reason -- a caller deciding whether to CREATE must not confuse
        the two.

        Asked once and answered once: the caller uses this both to decide how
        to validate its records and to tell ``load`` what it found, so a load
        costs one existence check rather than one per decision.

        Args:
            conn: Open connection, owned by the caller and shared.
            target: The database object identity being loaded into.
        """
        if not DatabaseTable.exists(conn, target, self.adapter):
            return None
        # What the destination's columns are is the table's (backlog 722).
        return DatabaseTable.column_names(conn, target, self.adapter)

    def load(
        self,
        conn: Any,
        target: Any,
        records: list[dict[str, Any]],
        columns: list[tuple[str, LogicalType]],
        destination_columns: list[str] | None = _UNASKED,
    ) -> int:
        """Put these records in the destination, creating it if allowed.

        Args:
            conn: Open connection, owned by the caller and shared.
            target: The database object identity being loaded into. Passed,
                not held -- see ``__init__``.
            records: The logical records to insert.
            columns: ``(column, LogicalType)`` pairs describing them -- Rey's
                logical schema (backlog 690). The insert column list is DERIVED
                from this rather than passed separately, so a CREATE and its
                INSERT cannot be handed different lists.

        Returns:
            How many records were inserted.

        Raises:
            ConfigError: When the destination is absent and creation was not
                declared. A RUN-LEVEL fault: every file in the batch would
                fail the same way, so it stops the run rather than failing
                one file.
            DatabaseError: When the insert fails for any reason not repaired
                by widening an overflowing text column.
        """
        names = [name for name, _logical in columns]
        schema, table = adapter_destination(target)

        # A caller that already asked tells us what it found, so the check
        # happens ONCE per load rather than once per decision. A caller that
        # did not ask gets it asked here.
        if destination_columns is _UNASKED:
            destination_columns = self.destination_columns(conn, target)
        exists = destination_columns is not None

        if not exists and not (self.create_destination or self.recreate_destination):
            raise ConfigError(
                f"destination {schema}.{table} does not exist, and "
                f"this load does not declare that it may be created."
            )

        if exists and (self.replace_destination or self.recreate_destination):
            # WHAT IS THERE IS NOT TOUCHED UNTIL THE NEW ROWS ARE SAFE (backlog
            # 516). They are staged beside the destination first, where every
            # failure -- the truncation retry included -- costs nothing; only
            # then does the table promote them, atomically, so a failure at any
            # point leaves the destination exactly as it was.
            return self._load_through_staging(conn, target, records, columns, names)

        if not exists:
            # Only when absent. Calling this unconditionally would contradict
            # the policy governing it: IF NOT EXISTS is no defence, because a
            # table dropped between the check and the call would be recreated
            # against a load that said not to.
            DatabaseTable.create(conn, target, columns, self.adapter)

        return self._insert_widening(conn, target, records, columns, names)

    def _load_through_staging(
        self,
        conn: Any,
        target: Any,
        records: list[dict[str, Any]],
        columns: list[tuple[str, LogicalType]],
        names: list[str],
    ) -> int:
        """Stage the rows beside the destination, then let the table promote them.

        ``replace`` keeps the table and replaces its rows; ``recreate`` puts a
        table built from these records in its place -- each the table's own
        atomic behaviour (``DatabaseTable.replace_rows`` / ``replace_table``).
        The staging table is removed afterwards, and on failure.
        """
        staging = replace(target, name=_beside(target.name, "stage"))
        if DatabaseTable.exists(conn, staging, self.adapter):
            # One a failed run left behind: it is this loader's, by its name.
            DatabaseTable.drop(conn, staging, self.adapter)
        failed = True
        try:
            DatabaseTable.create(conn, staging, columns, self.adapter)
            inserted = self._insert_widening(conn, staging, records, columns, names)
            if self.recreate_destination:
                DatabaseTable.replace_table(conn, target, staging, self.adapter)
            else:
                DatabaseTable.replace_rows(conn, target, staging, self.adapter)
            failed = False
            return inserted
        finally:
            self._remove_staging(conn, staging, failed)

    def _remove_staging(self, conn: Any, staging: Any, failed: bool) -> None:
        """Remove the staging table, where it is still there.

        After a failure the original error is the one reported, so a failed
        removal is logged rather than allowed to replace it; after a success
        it is raised, because a staging table left behind is a fault.
        """
        try:
            if DatabaseTable.exists(conn, staging, self.adapter):
                DatabaseTable.drop(conn, staging, self.adapter)
                conn.commit()
        except DatabaseError as exc:
            if not failed:
                raise
            _logger.debug("Staging table %s not removed: %s", staging.name, exc)

    def _insert_widening(
        self,
        conn: Any,
        target: Any,
        records: list[dict[str, Any]],
        columns: list[tuple[str, LogicalType]],
        names: list[str],
    ) -> int:
        """Insert, widening an overflowing text column once and trying again."""
        schema, table = adapter_destination(target)
        try:
            return self._insert(conn, target, records, names)
        except DatabaseError as insert_exc:
            conn.rollback()
            if not self.adapter.is_truncation_error(insert_exc):
                raise
            widened = _widened(columns, records)
            if not widened:
                raise
            # A table this load created may have gone with the rollback, where
            # the provider's DDL is transactional: then it is created again,
            # wider. Otherwise the overflowing columns are widened in place.
            if DatabaseTable.exists(conn, target, self.adapter):
                table_object = DatabaseTable(identity=target)
                for name, logical in widened.items():
                    table_object.widen_column(conn, name, logical, self.adapter)
            else:
                DatabaseTable.create(
                    conn, target,
                    [(name, widened.get(name, logical)) for name, logical in columns],
                    self.adapter,
                )
            _logger.info(
                "Retrying insert into %s.%s after widening %s",
                schema, table, ", ".join(widened),
            )
            return self._insert(conn, target, records, names)

    def load_from_path(
        self,
        conn: Any,
        target: Any,
        source_path: Any,
        columns: list[str],
    ) -> int:
        """Put a file's rows in an EXISTING destination, without reading it.

        The non-row sibling of ``load``. Same destination, same commit
        discipline, same insert column list -- the rows are read by the
        engine over the file instead of being carried through this process.

        **EXISTING DESTINATIONS ONLY, and this does not check.** Deciding
        that the destination is there, that the source declares its
        structure, and that the transform has an equivalent non-row form is
        the caller's; those answers are what make this callable at all. There
        is no create policy here because creating requires a schema, and a
        schema is derived from records this path does not have.

        **NO TRANSACTION IS OPENED.** The provider's statement is atomic, so
        a failure leaves the destination as it was. The commit below is this
        load's, covering this file and nothing else -- the same rule
        ``_insert`` states, which is what keeps one file's failure from
        touching another's committed work.

        No truncation retry either: widening measures the records, and there
        are none. See ``DBAdapter.insert_from_path``.

        Args:
            conn: Open connection, owned by the caller and shared.
            target: The database object identity being loaded into.
            source_path: The file the provider reads.
            columns: Insert order, established from the declared structure.

        Returns:
            Rows inserted, as the engine counted them. Zero is a real answer
            -- the source held no rows -- and the caller refuses it the same
            way it refuses an empty read.

        Raises:
            DatabaseError: If the insert fails.
        """
        schema, table = adapter_destination(target)
        inserted = self.adapter.insert_from_path(
            conn, schema, table, source_path, columns
        )
        conn.commit()
        return inserted

    def _insert(
        self,
        conn: Any,
        target: Any,
        records: list[dict[str, Any]],
        columns: list[str],
    ) -> int:
        """Insert and commit, once.

        The commit is THIS load's, covering these records and nothing else --
        a batch of files is an aggregate of separate loads, and one failing
        must not undo another that already succeeded.
        """
        schema, table = adapter_destination(target)
        inserted = self.adapter.bulk_insert(
            conn, schema, table, records, columns
        )
        conn.commit()
        return inserted


def _widened(
    columns: list[tuple[str, LogicalType]], records: list[dict[str, Any]],
) -> dict[str, LogicalType]:
    """Each bounded text column a value overflowed, as the wider type it needs.

    Only ``text`` with a ``length`` can overflow; every other kind is sized by
    what it means, so nothing else is widened. The new length is the longest
    value seen plus a margin.
    """
    widened: dict[str, LogicalType] = {}
    for name, logical in columns:
        if logical.kind != "text" or logical.length is None:
            continue
        longest = max(
            (len(str(record.get(name, "") or "")) for record in records), default=0,
        )
        if longest > logical.length:
            widened[name] = LogicalType("text", length=longest + _COLUMN_EXPAND_BUFFER)
    return widened
