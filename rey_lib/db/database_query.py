"""A caller's row query, run and answered as plain rows (backlog 730).

``DatabaseQuery`` owns the execution mechanics of a row query and nothing
else. The caller writes the statement and knows what its rows mean; this binds
the caller's parameters unchanged, runs the statement in dataset mode, and
hands back each row as a plain dictionary, so no adapter or provider row type
reaches the caller.

It holds no SQL of its own and interprets no row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["DatabaseQuery"]


@dataclass(frozen=True)
class DatabaseQuery:
    """One parameter-bound statement whose answer is rows.

    ``params`` are bound by name, never interpolated, and passed through as
    the caller gave them.
    """

    statement: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def rows(self, conn: Any, adapter: Any = None) -> tuple[dict[str, Any], ...]:
        """Run the statement on ``conn`` and return its rows, in the order returned.

        Each row is a plain dictionary of column to value; no rows -- including
        a driver answering nothing at all -- is an empty tuple.
        """
        if adapter is None:
            from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415 -- imports widely

            adapter = DBAdapter()
        read = adapter.execute_sql(conn, self.statement, dict(self.params), "dataset_result")
        return tuple(dict(row) for row in (read or []))
