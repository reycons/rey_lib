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

**No configuration to interpret.** ``schema`` and ``table`` arrive resolved;
parsing ``"database.schema.table"`` is configuration interpretation and
happens at the construction boundary.

**No module globals.** The adapter is injected, so substituting one is passing
an argument rather than patching a module attribute.
"""

from __future__ import annotations

from typing import Any, Callable

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = ["DataLoader", "WidenColumns"]

_logger = get_logger(__name__)

#: "The caller did not ask" -- distinct from None, which means "the caller
#: asked and the destination is not there". Without this sentinel the two
#: would collapse and an absent destination would be re-checked needlessly,
#: or a caller's answer of None would be mistaken for not having asked.
_UNASKED: Any = object()

#: How a caller widens columns that were too narrow, and reports whether it
#: changed anything.
#:
#: ``(conn, records, column_defs) -> widened?``
#:
#: Injected because widening is CONFIGURED behaviour -- it must run a declared
#: stored procedure or SQL file rather than inline DDL, and its parameters are
#: passed through the application context. This object decides WHEN a retry is
#: warranted; it never learns HOW widening is configured.
#:
#: The connection is an argument for the same reason this object does not hold
#: one: it is externally owned and varies per call.
WidenColumns = Callable[[Any, list[dict[str, Any]], list[tuple[str, str]]], bool]


class DataLoader:
    """One destination, and the policy for putting records into it."""

    def __init__(
        self,
        *,
        schema: str,
        table: str,
        adapter: Any,
        create_destination: bool = False,
        widen_columns: WidenColumns | None = None,
    ) -> None:
        """Hold the destination and the policies that govern loading into it.

        Args:
            schema: Target schema, already resolved. May be
                ``database.schema`` where a backend qualifies that way.
            table: Target table, already resolved.
            adapter: The ``DBAdapter`` every database operation goes through.
                Injected rather than reached for, so this object has no
                ambient state and a test substitutes one by passing it.
            create_destination: Whether an ABSENT destination may be created
                from the records. False means the table must already exist,
                which is what the loader did before the setting existed.
            widen_columns: How to widen columns after a truncation. Absent
                means a truncation is simply reported.
        """
        self.schema = schema
        self.table = table
        self.adapter = adapter
        self.create_destination = create_destination
        self.widen_columns = widen_columns

    def destination_columns(self, conn: Any) -> list[str] | None:
        """Return the destination's columns, or None when it does not exist.

        **``None`` is not ``[]``.** An empty list would mean a table with no
        columns, which nothing matches; ``None`` means there is no table yet.
        The same distinction the file-structure contract uses, and for the
        same reason -- a caller deciding whether to CREATE must not confuse
        the two.

        Asked once and answered once: the caller uses this both to decide how
        to validate its records and to tell ``load`` what it found, so a load
        costs one existence check rather than one per decision.
        """
        if not self.adapter.table_exists(conn, self.schema, self.table):
            return None
        return self.adapter.get_table_columns(conn, self.schema, self.table)

    def load(
        self,
        conn: Any,
        records: list[dict[str, Any]],
        column_defs: list[tuple[str, str]],
        destination_columns: list[str] | None = _UNASKED,
    ) -> int:
        """Put these records in the destination, creating it if allowed.

        Args:
            conn: Open connection, owned by the caller and shared.
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

        # A caller that already asked tells us what it found, so the check
        # happens ONCE per load rather than once per decision. A caller that
        # did not ask gets it asked here.
        if destination_columns is _UNASKED:
            destination_columns = self.destination_columns(conn)
        exists = destination_columns is not None

        if not exists and not self.create_destination:
            raise ConfigError(
                f"destination {self.schema}.{self.table} does not exist, and "
                f"this load does not declare that it may be created."
            )

        if not exists:
            # Only when absent. Calling this unconditionally would contradict
            # the policy governing it: IF NOT EXISTS is no defence, because a
            # table dropped between the check and the call would be recreated
            # against a load that said not to.
            self.adapter.create_staging_table_if_not_exists(
                conn, self.schema, self.table, column_defs
            )

        try:
            return self._insert(conn, records, columns)
        except DatabaseError as insert_exc:
            conn.rollback()

            if not self.adapter.is_truncation_error(insert_exc):
                raise
            if self.widen_columns is None:
                raise
            if not self.widen_columns(conn, records, column_defs):
                raise

            _logger.info(
                "Retrying insert into %s.%s after column alterations",
                self.schema, self.table,
            )
            return self._insert(conn, records, columns)

    def _insert(
        self,
        conn: Any,
        records: list[dict[str, Any]],
        columns: list[str],
    ) -> int:
        """Insert and commit, once.

        The commit is THIS load's, covering these records and nothing else --
        a batch of files is an aggregate of separate loads, and one failing
        must not undo another that already succeeded.
        """
        inserted = self.adapter.bulk_insert(
            conn, self.schema, self.table, records, columns
        )
        conn.commit()
        return inserted
