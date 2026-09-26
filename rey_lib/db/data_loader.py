"""Where logical records go.

The sink of source -> transform -> sink. A ``DataFile`` says what a file
contains, a ``DataTransform`` says what the records made from it contain, and
this puts those records in a table.

It owns DESTINATION MECHANICS and nothing else: does the table exist, may it
be created, insert the rows, and decide whether a failure is worth retrying.
Every database operation goes through ``DBAdapter``; no SQL is written here
and no provider is named.

What it deliberately does not have
----------------------------------
**No ``ctx``.** The application context is consumed where these objects are
constructed and does not travel down with them. The one indirect exception is
``widen_columns`` -- see below -- and it is documented rather than hidden.

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

from typing import Any, Callable

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = ["DataLoader", "WidenColumns", "adapter_destination"]

_logger = get_logger(__name__)

#: "The caller did not ask" -- distinct from None, which means "the caller
#: asked and the destination is not there". Without this sentinel the two
#: would collapse and an absent destination would be re-checked needlessly,
#: or a caller's answer of None would be mistaken for not having asked.
_UNASKED: Any = object()

#: How a caller widens columns that were too narrow, and reports whether it
#: changed anything.
#:
#: ``(conn, target, records, column_defs) -> widened?``
#:
#: Injected because widening is CONFIGURED behaviour -- it must run a declared
#: stored procedure or SQL file rather than inline DDL, and its parameters are
#: passed through the application context. This object decides WHEN a retry is
#: warranted; it never learns HOW widening is configured.
#:
#: The connection and the target are arguments for the same reason this object
#: holds neither: one is externally owned and the other is the load's, and both
#: vary per call.
WidenColumns = Callable[
    [Any, Any, list[dict[str, Any]], list[tuple[str, str]]], bool
]


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
        widen_columns: WidenColumns | None = None,
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
            widen_columns: How to widen columns after a truncation. Absent
                means a truncation is simply reported.
        """
        self.adapter = adapter
        self.create_destination = create_destination
        self.replace_destination = replace_destination
        self.widen_columns = widen_columns

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
        schema, table = adapter_destination(target)
        if not self.adapter.table_exists(conn, schema, table):
            return None
        return self.adapter.get_table_columns(conn, schema, table)

    def load(
        self,
        conn: Any,
        target: Any,
        records: list[dict[str, Any]],
        column_defs: list[tuple[str, str]],
        destination_columns: list[str] | None = _UNASKED,
    ) -> int:
        """Put these records in the destination, creating it if allowed.

        Args:
            conn: Open connection, owned by the caller and shared.
            target: The database object identity being loaded into. Passed,
                not held -- see ``__init__``.
            records: The logical records to insert.
            column_defs: ``(column, sql_type)`` pairs describing them. The
                insert column list is DERIVED from this rather than passed
                separately, so a CREATE and its INSERT cannot be handed
                different lists.

        Returns:
            How many records were inserted.

        Raises:
            ConfigError: When the destination is absent and creation was not
                declared. A RUN-LEVEL fault: every file in the batch would
                fail the same way, so it stops the run rather than failing
                one file.
            DatabaseError: When the insert fails for any reason not repaired
                by widening.
        """
        columns = [name for name, _sql_type in column_defs]
        schema, table = adapter_destination(target)

        # A caller that already asked tells us what it found, so the check
        # happens ONCE per load rather than once per decision. A caller that
        # did not ask gets it asked here.
        if destination_columns is _UNASKED:
            destination_columns = self.destination_columns(conn, target)
        exists = destination_columns is not None

        if not exists and not self.create_destination:
            raise ConfigError(
                f"destination {schema}.{table} does not exist, and "
                f"this load does not declare that it may be created."
            )

        if not exists:
            # Only when absent. Calling this unconditionally would contradict
            # the policy governing it: IF NOT EXISTS is no defence, because a
            # table dropped between the check and the call would be recreated
            # against a load that said not to.
            self.adapter.create_staging_table_if_not_exists(
                conn, schema, table, column_defs
            )
        elif self.replace_destination:
            # THE CONTENTS ARE REPLACED, so what was there goes first.
            #
            # Only where the destination EXISTS: a table this load just created
            # has nothing to remove, and emptying it would be a second answer
            # to a question `create` already settled.
            #
            # BEFORE the insert and inside the same transaction as it, so a
            # failed write leaves the destination as it was rather than empty
            # -- the worst outcome available, because it destroys what was
            # there and puts nothing in its place.
            #
            # The connector is asked; which provider is on the other end, and
            # how it spells this, is not this object's business.
            self.adapter.delete_all_rows(conn, schema, table)

        try:
            return self._insert(conn, target, records, columns)
        except DatabaseError as insert_exc:
            conn.rollback()

            if not self.adapter.is_truncation_error(insert_exc):
                raise
            if self.widen_columns is None:
                raise
            if not self.widen_columns(conn, target, records, column_defs):
                raise

            _logger.info(
                "Retrying insert into %s.%s after column alterations",
                schema, table,
            )
            return self._insert(conn, target, records, columns)

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
