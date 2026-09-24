"""A query on a connection, as a data object a load can read.

    QuerySource -> DataTransform / IdentityTransform -> DatabaseObjectIdentity

The database family's READ side. Its siblings here are the identity a load
writes to and the mechanics that write it; until now the family could be a
target and never a source, so a load could only ever begin at a file.

**Its primitive is a connection and a statement**, the way a ``DataFile``'s is
a path. It is not a file, does not pretend to be one, and answers nothing
file-shaped: no format token, no record shape, no path. What it answers is
what a load asks of the object it is reading.

**The caller owns the connection.** It is handed one that is already open --
the same contract ``DBDataSource`` states in ``rey_lib.analysis.datasource``
-- so nothing here resolves a connection config, opens a handle, or closes
one. A load that writes to a different connection opens that one separately;
neither end reaches for the other's.
"""

from __future__ import annotations

from typing import Any

from rey_lib.data.errors import DataStructureError
from rey_lib.logs import get_logger

__all__ = ["QuerySource"]

_logger = get_logger(__name__)

#: What is read to learn the columns. One row, because a result set states its
#: columns whether or not it has any -- but a database will not describe a
#: statement it has not run, so this is what running it costs.
_STRUCTURE_ROWS = 1


class QuerySource:
    """One statement on one connection, and the records it returns."""

    def __init__(
        self,
        conn: Any,
        statement: str,
        *,
        adapter: Any,
    ) -> None:
        """Hold the open connection, the statement, and the adapter to run it.

        Args:
            conn: An open connection, owned by the caller.
            statement: The query. Held as written -- nothing here parses it,
                rewrites it, or decides what it means.
            adapter: The ``DBAdapter`` every database operation goes through.
                Injected rather than reached for, so this object has no
                ambient state and a test substitutes one by passing it -- the
                same reason ``DataLoader`` takes one.
        """
        self.conn = conn
        self.statement = str(statement)
        self.adapter = adapter

    def source_structure(self) -> list[str]:
        """The columns this query returns, in the order it returns them.

        ORDERED, because that is what the comparison against a destination
        relies on and what an insert is built from.

        Costs one row. A database describes a result by running the statement,
        so there is no reading of the structure that does not execute it; what
        is avoided is materialising the whole result to find out its shape.

        Returns:
            The column names, or an empty list for a statement that returns
            no result at all.

        Raises:
            DatabaseError: If the statement cannot be executed. Not converted
                into a structural failure -- a query that will not run is not
                a query whose shape is wrong.
        """
        columns, _rows = self.adapter.query_rows(
            self.conn, self.statement, limit=_STRUCTURE_ROWS,
        )
        return list(columns)

    def read(self) -> list[dict[str, Any]]:
        """Every row this query returns, knowing nothing of any destination.

        ``limit=None``: a load must have all of its source, and naming a large
        bound instead would silently truncate the day the result outgrew it.
        """
        _columns, rows = self.adapter.query_rows(
            self.conn, self.statement, limit=None,
        )
        return list(rows)

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Refuse a query whose structure is not what it must be.

        **TWO DIFFERENT QUESTIONS, and the argument selects which** -- the same
        split every source answers.

        ``expected_columns`` given -- DESTINATION COMPATIBILITY, compared in
            ORDER. A result set's columns are ordered and the insert is built
            from that order, so a set comparison would pass two results that
            write different values into different columns.

        ``expected_columns`` is None -- INTERNAL CONSISTENCY. A result set has
            nothing to contradict itself with, so the only thing to establish
            is that it has columns at all.

        **``None`` is not ``[]``.** An empty list is a destination that has no
        columns, which nothing matches.

        Raises:
            DataStructureError: When the query does not satisfy whichever
                question was asked.
        """
        actual = self.source_structure()

        if expected_columns is None:
            if not actual:
                raise DataStructureError(
                    f"{self!r} returns no columns, so its structure "
                    "cannot be established.",
                    validation_name="load_structure",
                )
            return

        if actual == expected_columns:
            return

        _logger.error(
            "Load structure validation failed for %r\n"
            "Expected destination columns:\n%s\n\n"
            "Actual query columns:\n%s",
            self,
            ",".join(expected_columns),
            ",".join(actual),
        )
        # SHORT, because this is what the run log records. The diagnosis above
        # goes to the logger; putting it here instead would change recorded
        # evidence.
        raise DataStructureError(
            "Structure mismatch", validation_name="load_structure",
        )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate, THEN read -- the cheap order for this source.

        A query states its columns from one row, so a result whose shape is
        wrong is refused before the whole of it is fetched. The opposite order
        is a keyed source's, which cannot know its keys until it has read.

        The statement is executed twice: once bounded to learn the shape, once
        to read. That is the price of refusing early, and it is the same trade
        a delimited file makes when it reads its header before its rows.
        """
        self.validate(expected_columns)
        return self.read()

    def __repr__(self) -> str:
        """Name the source and its statement, for a failure that has to be read.

        The same shape ``DataFile.__repr__`` uses -- the class, and enough of
        what it is reading to recognise it.
        """
        return f"{type(self).__name__}({_glimpse(self.statement)!r})"


def _glimpse(statement: str) -> str:
    """One line of a statement, short enough to sit inside a message."""
    flattened = " ".join(str(statement).split())
    return flattened if len(flattened) <= 60 else f"{flattened[:57]}..."
