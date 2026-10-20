"""Running a caller's SQL, and the shape of what comes back (backlogs 730, 731, 757).

``DatabaseQuery`` owns the execution mechanics beneath a caller that owns its
SQL, and nothing else. It holds no SQL of its own and knows nothing of what an
answer means; the caller wrote the text and decides that. DBAdapter is the
primitive beneath it.

Four ways to run, one per need. ``rows`` binds the caller's parameters
unchanged, runs in dataset mode and hands back plain dictionaries. ``run``
executes the text exactly as supplied -- possibly a batch, a definition or a
call -- for every result it produces; ``page`` reads it a page at a time.
``read`` runs one read and keeps its columns whether or not any row came back,
bounded or not, on every provider that reads. The text is never read,
classified, bounded or rewritten, and a failure or a declination reaches the
caller as raised. Parameters are valid only where they can be bound: ``run``,
``page`` and ``read`` refuse them rather than drop them.

``StatementResult`` and ``PageResult`` are the one shape of those answers. The
providers build them; every caller reads them from here. Their values are the
providers' own: making one JSON-safe is ``rey_lib.files.json.json_value``'s,
invoked by whichever boundary sends it on.
"""

from __future__ import annotations

import re
import threading

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from rey_lib.errors.error_utils import DatabaseError

if TYPE_CHECKING:
    from rey_lib.db.connection import Connection

__all__ = ["DatabaseQuery", "PageResult", "StatementResult"]

#: The most rows one result of a run holds when the query is executed without paging.
RESULT_ROW_LIMIT = 1_000

#: The live properties a reader may change through ``DatabaseQuery.update``.
_LIVE_PROPERTIES = frozenset({
    "statement", "connection", "pageable", "rows_per_page", "current_page", "order_by",
    "filters",
})


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


@dataclass
class DatabaseQuery:
    """A caller's SQL, run in one of the four ways a caller needs -- and, held
    live by the application hosting a reader's query, the query itself.

    ``rows`` binds ``params`` by name, never interpolated, and passes them
    through as the caller gave them. ``run``, ``page`` and ``read`` execute the
    text exactly as supplied, which binds nothing: they refuse a query carrying
    parameters rather than run it without them.

    THE LIVE QUERY. Bound to a shared ``connection`` -- a reference the host
    supplies; this object never creates, duplicates or closes it -- the query
    owns its reading state (``pageable``, ``rows_per_page``, ``current_page``,
    ``order_by``, ``filters``), changes it through ``update``, shows it through
    ``projection`` and acts on it through ``execute``. Paging and running stay
    the provider's, beneath ``page`` and ``run``.
    """

    statement: str
    params: Mapping[str, Any] = field(default_factory=dict)
    #: The shared Connection this query runs on: a reference, never owned here.
    connection: Optional["Connection"] = field(default=None, compare=False, repr=False)
    #: Whether ``execute`` reads one page of the result, or runs the query.
    pageable: bool = True
    #: How many rows one page holds.
    rows_per_page: int = 50
    #: Which page ``execute`` reads, from 0.
    current_page: int = 0
    #: The ordering a page applies, as column/direction mappings.
    order_by: list[dict[str, Any]] = field(default_factory=list)
    #: The filters a page applies, as column/operator/value mappings. A filter
    #: may also carry ``path``, a list of keys naming a value nested inside the
    #: column (#1143) -- logical only; the provider renders it.
    filters: list[dict[str, Any]] = field(default_factory=list)
    # One act at a time: an update, or an execution and the answer it gives.
    _acting: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False,
    )

    # -- the live query -------------------------------------------------------

    def act(self) -> Any:
        """The lock one act is taken under: an update, or an execution."""
        return self._acting

    def text(self) -> str:
        """The statement with its parameters written in, as a reader sees it.

        Each ``:name`` the parameters name is replaced by the SQL literal that
        stands for its value, so no value's characters compose SQL. Text that
        names no parameter is returned as written.
        """
        written = self.statement
        for name, value in self.params.items():
            literal = _sql_literal(value)
            written = re.sub(
                rf"(?<![:\w]):{re.escape(str(name))}\b", lambda _match: literal, written,
            )
        return written

    def update(self, **changes: Any) -> None:
        """Change the query's live properties, and only those.

        Replacing the statement replaces the text as written, so its parameters
        go with it. Nothing else changes implicitly: a new statement does not
        move the page, and neither does a new ``connection`` -- the shared
        Connection its host supplies, still a reference this query never
        creates or closes. A caller that means the page to move says so.

        Raises:
            DatabaseError: If a change names anything but a live property.
        """
        unknown = sorted(set(changes) - _LIVE_PROPERTIES)
        if unknown:
            raise DatabaseError(f"DatabaseQuery has no live property {unknown}.")
        with self._acting:
            for name, value in changes.items():
                setattr(self, name, value)
            if "statement" in changes:
                self.params = {}

    def projection(self) -> dict[str, Any]:
        """What this query shows: its text and its reading state. Runs nothing."""
        with self._acting:
            return {
                "statement": self.text(),
                "pageable": self.pageable,
                "rows_per_page": self.rows_per_page,
                "current_page": self.current_page,
                "order_by": list(self.order_by),
                "filters": list(self.filters),
                "connection": self.connection.name if self.connection is not None else None,
            }

    def execute(self) -> dict[str, Any]:
        """Execute this query as its own state says, on its connection.

        ``pageable`` reads page ``current_page`` of ``rows_per_page`` rows; a
        provider that cannot page this text says so by raising, and that answer
        reaches the caller. Otherwise the query is run, each result holding at
        most ``RESULT_ROW_LIMIT`` rows.

        Raises:
            DatabaseError: If the query is bound to no connection, has no
                statement, or execution fails.
            UnsupportedDatabaseCapabilityError: If paging was asked for and the
                provider cannot page this text.
        """
        with self._acting:
            if self.connection is None:
                raise DatabaseError("DatabaseQuery is bound to no connection.")
            written = self.text()
            if not written.strip():
                raise DatabaseError("DatabaseQuery has no statement to execute.")
            handle = self.connection.handle()
            as_written = DatabaseQuery(written)
            if self.pageable:
                page = as_written.page(
                    handle,
                    offset=int(self.current_page) * int(self.rows_per_page),
                    limit=int(self.rows_per_page),
                    order_by=list(self.order_by) or None,
                    filters=list(self.filters) or None,
                )
                return {
                    "pageable": True,
                    "columns": _columns(page.columns, page.column_kinds),
                    "rows": list(page.rows),
                    "total_row_count": page.total_row_count,
                    "offset": page.offset,
                    "limit": page.limit,
                    "next_offset": page.next_offset,
                    **({"max_page_size": page.max_page_size}
                       if page.max_page_size is not None else {}),
                }
            results = as_written.run(handle, limit=RESULT_ROW_LIMIT)
            return {
                "pageable": False,
                "results": [
                    {
                        "columns": _columns(result.columns, result.column_kinds),
                        "rows": list(result.rows),
                        "row_count": result.row_count,
                        "at_row_limit": len(result.rows) >= RESULT_ROW_LIMIT,
                    }
                    for result in results
                ],
                "row_limit": RESULT_ROW_LIMIT,
            }

    # -- the four ways to run -------------------------------------------------

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

    def read(
        self, conn: Any, *, limit: int | None = None, adapter: Any = None,
    ) -> StatementResult:
        """Run one read and return its columns and its rows, in the order returned.

        What ``rows`` cannot answer (backlog 757): the COLUMNS are kept even
        when no row comes back, because a result states them whether or not it
        has any; and the read can be BOUNDED, so learning a result's shape
        costs one row and a sample costs its size.

        ``limit=None`` is every row -- a different act from a large number,
        which would silently truncate the day the result outgrew it.
        ``limit=n`` stops after ``n``. Every provider that reads answers it.

        Returns:
            The one result, as the canonical ``StatementResult``. ``row_count``
            is None: a read affects nothing, so there is no count to report.

        Raises:
            DatabaseError: If the query carries parameters, or execution fails.
        """
        self._unbound("read")
        columns, rows = _adapter(adapter).query_rows(conn, self.statement, limit=limit)
        return StatementResult(columns=list(columns), rows=list(rows))

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


def _columns(names: list[str], kinds: Mapping[str, str]) -> list[dict[str, Any]]:
    """A result's columns, each by name with the kind its provider stated."""
    return [{"name": name, **({"kind": kinds[name]} if name in kinds else {})}
            for name in names]


def _sql_literal(value: Optional[Any]) -> str:
    """Any value as the SQL literal that stands for it.

    A number as itself, absence as NULL, a boolean as TRUE/FALSE, and
    everything else as quoted text with its quotes doubled. A value that looks
    like SQL is text like any other -- rendered, never spliced, so it can close
    no literal and open no statement of its own.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
