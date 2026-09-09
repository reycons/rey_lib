"""The relational destination for a code index.

One implementation of :class:`CodeIndexWriter`. It fills the schema's staging
tables and calls the schema's promotion procedure; it holds no privilege on
storage and could not write a row there if it tried.

**The schema owns atomicity, not this.** ``code.p_index_replace`` empties
storage, repopulates it from staging and empties staging, all inside one call,
so a failure anywhere leaves the previous index exactly as it was. Sequencing a
wipe and a repopulate from here would put a visible half-index between two
statements, and no amount of care in Python closes that window.

Staging is filled as ordinary inserts and deliberately needs no transaction:
nothing reads it, and a half-filled staging table means nothing until a call
promotes it. That is what lets a scan of roughly 91,000 records arrive without
one enormous parameter.

This module is where the database lives. ``code_index`` -- the shape, the
grouping, the protocol -- imports nothing from ``rey_lib.db``, so a second
destination costs one class and no change there.
"""

from __future__ import annotations

from typing import Any

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.code_index import IndexedRepository

__all__ = ["DatabaseCodeIndexWriter"]

logger = get_logger(__name__)

#: The schema owning the code index. One place knows the word.
SCHEMA = "code"

#: What each staging table is filled with, in the order its columns are named.
#: Kept beside the SQL rather than derived from a row, so a column added to one
#: side without the other is a failure at the insert rather than a silent skip.
_REPOSITORY_COLUMNS = (
    "repository_key", "revision", "branch", "working_tree_status",
    "content_hash", "generator_version", "rules_hash",
)
_FILE_COLUMNS = (
    "repository_key", "relative_path", "language", "content_hash",
    "is_generated", "is_vendor", "is_test",
)
_SYMBOL_COLUMNS = (
    "repository_key", "relative_path", "symbol_kind", "name",
    "qualified_name", "owner", "is_public", "start_line", "start_column",
)


class DatabaseCodeIndexWriter:
    """Writes one snapshot through the code schema's own write API."""

    def __init__(self, adapter: DBAdapter, connection: Any) -> None:
        """Hold what was already resolved.

        Neither is opened here. The application bootstraps, resolves the
        connection its configuration names, and hands it over; a writer that
        found its own would be a second answer to which database this is.

        Args:
            adapter: The provider-dispatching adapter.
            connection: An open connection for the indexer's role.
        """
        self._adapter = adapter
        self._connection = connection

    def replace_index(self, indexed: list[IndexedRepository]) -> None:
        """Stage every repository, then promote them as one replacement.

        Args:
            indexed: Every repository the scan observed.
        """
        self._clear_staging()

        repositories = [dict(entry.header) for entry in indexed]
        files: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        for entry in indexed:
            for declared in entry.files:
                files.append(
                    {
                        "repository_key": entry.header["repository_key"],
                        **{
                            column: declared[column]
                            for column in _FILE_COLUMNS
                            if column != "repository_key"
                        },
                    }
                )
                for symbol in declared["symbols"]:
                    symbols.append(
                        {
                            "repository_key": entry.header["repository_key"],
                            "relative_path": declared["relative_path"],
                            **symbol,
                        }
                    )

        self._stage("repository_stage", repositories, _REPOSITORY_COLUMNS)
        self._stage("file_stage", files, _FILE_COLUMNS)
        self._stage("symbol_stage", symbols, _SYMBOL_COLUMNS)

        logger.info(
            "Staged %d repositories, %d files, %d symbols",
            len(repositories),
            len(files),
            len(symbols),
        )
        self._call("p_index_replace")
        logger.info("Promoted the staged scan to the live index")

    def clear(self) -> None:
        """Empty the index and its staging.

        Administrative, not part of indexing: the normal path replaces, so
        nothing has to run between a wipe and a repopulate.
        """
        self._call("p_index_clear")

    def _clear_staging(self) -> None:
        """Empty staging before filling it.

        A promotion empties staging itself, so this only matters after a run
        that staged and then failed to promote. Leaving those rows would let
        the next scan promote a mixture of two.
        """
        for table in ("symbol_stage", "file_stage", "repository_stage"):
            self._adapter.execute_sql(
                self._connection,
                f"DELETE FROM {SCHEMA}.{table}",
                {},
                "no_return",
            )

    def _stage(
        self,
        table: str,
        rows: list[dict[str, Any]],
        columns: tuple[str, ...],
    ) -> None:
        """Insert staged rows through the adapter's bulk mechanism.

        Args:
            table: The staging table, unqualified.
            rows: What to insert. An empty list is a legitimate scan result and
                is not an error.
            columns: The column order the rows are written in.
        """
        if not rows:
            return
        self._adapter.bulk_insert(
            self._connection, SCHEMA, table, rows, list(columns)
        )

    def _call(self, procedure: str) -> None:
        """Call one of the schema's write procedures.

        Args:
            procedure: The unqualified procedure name.
        """
        self._adapter.execute_sql(
            self._connection, f"CALL {SCHEMA}.{procedure}()", {}, "no_return"
        )
