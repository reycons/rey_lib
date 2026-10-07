"""Running a caller's SQL, and the shape of what comes back (backlogs 730, 731).

``DatabaseQuery`` owns the execution mechanics beneath a caller that owns its
SQL, and nothing else. It holds no SQL of its own and knows nothing of what an
answer means; the caller wrote the text and decides that. DBAdapter is the
primitive beneath it.

Three ways to run, one per need. ``rows`` binds the caller's parameters
unchanged, runs in dataset mode and hands back plain dictionaries. ``run``
executes the text exactly as supplied -- possibly a batch, a definition or a
call -- for every result it produces; ``page`` reads it a page at a time. The
text is never read, classified, bounded or rewritten, and a failure or a
declination reaches the caller as raised. Parameters are valid only where they
can be bound: ``run`` and ``page`` refuse them rather than drop them.

``StatementResult`` and ``PageResult`` are the one shape of those answers. The
providers build them; every caller reads them from here. ``json_value`` is the
one way a value in them is made JSON-safe, for every caller that sends one on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from rey_lib.errors.error_utils import DatabaseError

__all__ = ["DatabaseQuery", "PageResult", "StatementResult", "json_value"]


@dataclass(frozen=True)
class PageResult:
    """One bounded page of a result, and the size of the whole result.

    A success type only. There is no "could not page" state to read here:
    a provider that cannot page says so by raising, because an empty page with
    a zero total would publish "this query has no rows" as though it were the
    answer.

    Shaped on the page models this estate already uses -- ``FileHierarchyPage``
    in ``rey_lib.logs.file_hierarchy`` carries the same offset/limit/total/
    next_offset -- so a reader who knows one knows this.

    Attributes:
        columns: The column names, in the order they were returned.
        rows: This page's rows, each a column-to-value mapping. At most
            ``limit`` of them.
        total_row_count: How many rows the whole query returns, **not** how many
            are on this page -- or **None where the provider does not count**.

            None means *unknown*, and never zero. A provider counts only where
            counting is an operation it can afford: over a table it is cheap,
            and over a file it is a full parse of that file, which is not a
            price to pay on every page. A reader of this field must say
            "unknown" rather than draw "no rows" over rows that plainly exist.
        offset: Where this page starts in the whole result.
        limit: The most rows this page may hold.
        next_offset: Where the following page starts, or None when this page
            reaches the end.

            **This is the continuation signal, not a convenience.** Where
            ``total_row_count`` is None, nothing else says whether another page
            exists -- there is no total to do arithmetic against -- so this must
            survive every crossing between here and whatever draws the page.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    total_row_count: int | None
    offset: int
    limit: int
    next_offset: int | None = None
    #: A neutral kind per column, where the provider stated one -- e.g.
    #: ``"document"`` for a structured value. Absent means not stated. The
    #: vocabulary is the providers'; this type assigns no meaning to any kind.
    column_kinds: dict[str, str] = field(default_factory=dict)
    #: The most rows the provider will return per page FOR THIS RESULT, or None
    #: where it sets no ceiling. Why it set one is the provider's; ``limit``
    #: already reports the limit actually applied.
    max_page_size: int | None = None


@dataclass(frozen=True)
class StatementResult:
    """One result a database handed back, normalized across providers.

    A result, not a statement: what a driver exposes and what a person typed are
    not the same count. A statement that returns nothing still produces one of
    these -- it ran, and saying so is the answer.

    Attributes:
        columns: The column names, in the order they were returned. Empty for a
            statement that returned no rows at all, which is ordinary for DDL
            and for a call.
        rows: The rows, each a column-to-value mapping, bounded by the caller's
            limit.
        row_count: How many rows the statement affected, or **None where the
            backend gave no meaningful count**. DBAPI reports -1 for "unknown"
            and means different things by it for queries and for DML, so that
            is normalized here rather than escaping as a number that looks real.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int | None = None
    #: A neutral kind per column, where the provider stated one. See
    #: ``PageResult.column_kinds``.
    column_kinds: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DatabaseQuery:
    """A caller's SQL, run in one of the three ways a caller needs.

    ``rows`` binds ``params`` by name, never interpolated, and passes them
    through as the caller gave them. ``run`` and ``page`` execute the text
    exactly as supplied, which binds nothing: they refuse a query carrying
    parameters rather than run it without them.
    """

    statement: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def rows(self, conn: Any, adapter: Any = None) -> tuple[dict[str, Any], ...]:
        """Run the statement on ``conn`` and return its rows, in the order returned.

        Each row is a plain dictionary of column to value; no rows -- including
        a driver answering nothing at all -- is an empty tuple.
        """
        read = _adapter(adapter).execute_sql(
            conn, self.statement, dict(self.params), "dataset_result",
        )
        return tuple(dict(row) for row in (read or []))

    def run(
        self, conn: Any, *, limit: int, adapter: Any = None,
    ) -> tuple[StatementResult, ...]:
        """Run the text as written and return every result it produced, in order.

        ``limit`` is the most rows read from any one result. A statement that
        returns no rows is still a result.

        Raises:
            DatabaseError: If the query carries parameters, or execution fails.
        """
        self._unbound("run")
        return tuple(_adapter(adapter).execute_statements(conn, self.statement, limit=limit))

    def page(
        self,
        conn: Any,
        *,
        offset: int,
        limit: int,
        order_by: list[dict[str, Any]] | None = None,
        filters: list[dict[str, Any]] | None = None,
        adapter: Any = None,
    ) -> PageResult:
        """One page of the text's result, and the size of the whole.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot page this
                text, raised before it has run. What that means is the caller's.
            DatabaseError: If the query carries parameters, or execution fails.
        """
        self._unbound("page")
        return _adapter(adapter).execute_page(
            conn, self.statement, offset=offset, limit=limit,
            order_by=order_by, filters=filters,
        )

    def _unbound(self, mode: str) -> None:
        """Refuse parameters a mode cannot bind, rather than run without them."""
        if self.params:
            raise DatabaseError(
                f"DatabaseQuery.{mode} executes the text as written and binds no "
                f"parameters; this query carries {sorted(self.params)}."
            )


def _adapter(adapter: Any) -> Any:
    """The adapter handed in, or the shared primitive beneath."""
    if adapter is None:
        from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415 -- imports widely

        adapter = DBAdapter()
    return adapter


def json_value(value: Any) -> Any:
    """One result value as JSON can carry it.

    Scalars as they are, a date or a timestamp in ISO form, a mapping or a
    sequence value by value, and anything else as its text.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return str(value)
