"""The relational destination for a code index.

One implementation of :class:`CodeIndexWriter`. It fills the schema's staging
tables and calls the schema's promotion procedure; it holds no privilege on
storage and could not write a row there if it tried.

**The schema owns atomicity, not this.** ``code.p_publish`` validates, empties
storage, repopulates it from staging and empties staging, all inside one call,
so a failure anywhere leaves the previous index exactly as it was. Sequencing a
wipe and a repopulate from here would put a visible half-index between two
statements, and no amount of care in Python closes that window.

**It declares what it is promoting, and promotes only the scan.** The authored
architecture lives in the same schema under its own lifecycle; this writer
never stages it and never replaces it. What it does have to answer for is that
the scan it is publishing still satisfies every authored reference, and
``p_publish`` refuses the promotion when it does not -- which is why the
validation is a parameter of the call rather than a check made here.

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

__all__ = ["CodeIndexDatabaseWriter"]

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
    "end_line", "end_column", "returns_annotation", "dotted_identity",
)
_PARAMETER_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "name", "ordinal", "parameter_kind", "has_default",
    "is_optional", "annotation",
)
_ASSIGNMENT_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "source_line", "source_column", "target_kind",
    "target_chain", "attribute_name", "key", "is_augmented",
)
_ACCESS_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "source_line", "source_column", "access_kind",
    "object_chain", "attribute_name", "method_name", "argument_present",
    "argument_kind", "literal_argument",
)
_EDGE_COLUMNS = (
    "repository_key", "relative_path", "source_line", "source_column",
    "from_id", "from_symbol", "from_symbol_line", "from_symbol_column",
    "to_reference", "edge_kind", "evidence",
)


class CodeIndexDatabaseWriter:
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

        edges: list[dict[str, Any]] = [
            {"repository_key": entry.header["repository_key"], **edge}
            for entry in indexed
            for edge in entry.edges
        ]

        self._stage("repository_stage", repositories, _REPOSITORY_COLUMNS)
        self._stage("file_stage", files, _FILE_COLUMNS)
        self._stage("symbol_stage", symbols, _SYMBOL_COLUMNS)
        self._stage("edge_stage", edges, _EDGE_COLUMNS)

        parameters: list[dict[str, Any]] = [
            {"repository_key": entry.header["repository_key"], **parameter}
            for entry in indexed
            for parameter in entry.parameters
        ]
        self._stage("parameter_stage", parameters, _PARAMETER_COLUMNS)

        writes = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.writes
        ]
        accesses = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.accesses
        ]
        self._stage("assignment_stage", writes, _ASSIGNMENT_COLUMNS)
        self._stage("access_stage", accesses, _ACCESS_COLUMNS)

        logger.info(
            "Staged %d repositories, %d files, %d symbols, %d edges, "
            "%d parameters, %d writes, %d accesses",
            len(repositories),
            len(files),
            len(symbols),
            len(edges),
            len(parameters),
            len(writes),
            len(accesses),
        )
        # The scan side only. The authored architecture is not staged here and
        # is therefore not replaced -- but every surviving realization is
        # resolved against this scan before it goes live, so a rename that
        # orphans one refuses the whole promotion.
        self._call("p_publish", "true, false")
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
        for table in ("access_stage", "assignment_stage", "parameter_stage",
                      "edge_stage", "symbol_stage", "file_stage",
                      "repository_stage"):
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

    def _call(self, procedure: str, arguments: str = "") -> None:
        """Call one of the schema's write procedures.

        Args:
            procedure: The unqualified procedure name.
            arguments: The argument list, already written as SQL. These are
                literal declarations of what is being promoted, never values
                taken from a scan, so there is nothing here to parameterize.
        """
        self._adapter.execute_sql(
            self._connection,
            f"CALL {SCHEMA}.{procedure}({arguments})",
            {},
            "no_return",
        )
