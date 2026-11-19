"""The relational destination for a code index.

One implementation of :class:`CodeIndexWriter`. It fills the schema's staging
tables and calls the schema's promotion procedure; it holds no privilege on
storage and could not write a row there if it tried.

**The schema owns atomicity, not this.** ``code.p_publish`` validates, empties
storage, repopulates it from staging and empties staging, all inside one call,
so a failure anywhere leaves the previous index exactly as it was. Sequencing a
wipe and a repopulate from here would put a visible half-index between two
statements, and no amount of care in Python closes that window.

**It publishes facts only (backlog 639).** ``replace_index`` promotes the scan
and nothing else. No authored declaration is staged, resolved or checked: the
facts of the current code publish whatever any architecture document says.

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

from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.procedure_map import ProcedureMap, get_procedure_map
from rey_lib.errors.error_utils import StateError
from rey_lib.logs.app_logger import AppLogger
from rey_lib.repository_map.code_index import IndexedRepository

__all__ = ["CodeIndexDatabaseWriter", "require_parent_batch_step"]

logger = AppLogger(__name__)

#: The schema owning the code index. One place knows the word.
SCHEMA = "code"

#: The procedure map the index's write routines are bound in, and the bindings (#541).
_MAP = "code"
_PUBLISH = "publish_code_index"
_CLEAR = "clear_code_index"

#: Where the scan is staged before code.p_publish admits it (backlog 855).
STAGE_SCHEMA = "code_stage"

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
    "is_generator", "name_line", "name_column",
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
_CLASS_ATTRIBUTE_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "name", "ordinal", "declaration_form", "is_annotated",
    "has_default", "is_optional", "annotation", "modifiers",
)
_RETURN_SITE_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "source_line", "source_column", "ordinal", "has_value",
    "value_kind", "value_chain",
)
_RAISE_SITE_COLUMNS = (
    "repository_key", "relative_path", "owner_qualified_name", "owner_line",
    "owner_column", "source_line", "source_column", "ordinal", "is_bare",
    "value_kind", "value_chain", "callee_chain", "has_cause",
)
_CALL_ARGUMENT_COLUMNS = (
    "repository_key", "relative_path", "source_line", "source_column",
    "callee", "edge_kind", "ordinal", "argument_form", "keyword",
    "argument_kind", "literal_argument", "expression",
)
_SEMANTIC_COVERAGE_COLUMNS = (
    "repository_key", "producer", "producer_version", "status", "reason",
    "position_encoding", "documents", "occurrences", "refused_occurrences",
)
_DEFINITION_EVIDENCE_COLUMNS = (
    "repository_key", "relative_path", "qualified_name", "name_line",
    "name_column", "symbol", "producer",
)
_REFERENCE_EVIDENCE_COLUMNS = (
    "repository_key", "relative_path", "end_line", "end_column", "symbol",
    "occurrence_kind", "package", "is_standard_library", "producer",
)
_EDGE_COLUMNS = (
    "repository_key", "relative_path", "source_line", "source_column",
    "from_id", "from_symbol", "from_symbol_line", "from_symbol_column",
    "to_reference", "edge_kind", "evidence", "end_line", "end_column",
)


_FLOW_OWNER_COLUMNS = ("repository_key", "relative_path", "owner_qualified_name", "owner_line", "owner_column")
_FLOW_COLUMNS = {
    "node": _FLOW_OWNER_COLUMNS + ("node_key", "kind", "source_line", "source_column",
                                   "end_line", "end_column", "text", "callee", "call_edge_kind", "commands"),
    "edge": _FLOW_OWNER_COLUMNS + ("from_key", "to_key", "kind"),
    "coverage": _FLOW_OWNER_COLUMNS + ("status", "reason", "producer_version"),
}


class CodeIndexDatabaseWriter:
    """Writes one snapshot through the code schema's own write API."""

    def __init__(self, adapter: DBAdapter, connection: Any, ctx: Any,
                 run_log: Any = None) -> None:
        """Hold what was already resolved.

        Neither is opened here. The application bootstraps, resolves the
        connection its configuration names, and hands it over; a writer that
        found its own would be a second answer to which database this is.

        Args:
            adapter: The provider-dispatching adapter.
            connection: An open connection for the indexer's role.
            ctx: The run's context, which holds the ``code`` procedure map the
                promotions are bound in (#541).
            run_log: The run's evidence recorder, which each promotion is logged to.
        """
        self._adapter = adapter
        self._connection = connection
        self._ctx = ctx
        self._run_log = run_log

    def replace_index(self, indexed: list[IndexedRepository],
                      parent_batch_step_id: Any) -> None:
        """Stage every repository, then promote them as one replacement.

        Args:
            indexed: Every repository the scan observed.
            parent_batch_step_id: The run's batch step. **Required** (#270):
                ``code.p_publish`` records its own step beneath it, and a
                governed routine never creates a batch.

        Raises:
            StateError: No parent step was supplied; nothing is staged.
        """
        require_parent_batch_step(parent_batch_step_id, "publishing the code index")
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
        class_attributes = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.class_attributes
        ]
        self._stage("assignment_stage", writes, _ASSIGNMENT_COLUMNS)
        self._stage("access_stage", accesses, _ACCESS_COLUMNS)
        self._stage("class_attribute_stage", class_attributes,
                    _CLASS_ATTRIBUTE_COLUMNS)
        return_sites = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.return_sites
        ]
        self._stage("return_site_stage", return_sites, _RETURN_SITE_COLUMNS)
        raise_sites = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.raise_sites
        ]
        self._stage("raise_site_stage", raise_sites, _RAISE_SITE_COLUMNS)
        call_arguments = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.call_arguments
        ]
        self._stage("call_argument_stage", call_arguments,
                    _CALL_ARGUMENT_COLUMNS)
        for kind, attribute in (("node", "control_flow_nodes"), ("edge", "control_flow_edges"),
                                ("coverage", "control_flow_coverage")):
            rows = [{"repository_key": entry.header["repository_key"], **row}
                    for entry in indexed for row in getattr(entry, attribute)]
            self._stage(f"control_flow_{kind}_stage", rows, _FLOW_COLUMNS[kind])
        # Semantic evidence (backlog 872): staged per repository, correlated
        # across all of them by the promotion, which alone sees every one.
        semantic_coverage = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.semantic_coverage
        ]
        definition_evidence = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.definition_evidence
        ]
        reference_evidence = [
            {"repository_key": entry.header["repository_key"], **row}
            for entry in indexed for row in entry.reference_evidence
        ]
        self._stage("semantic_coverage_stage", semantic_coverage,
                    _SEMANTIC_COVERAGE_COLUMNS)
        self._stage("definition_evidence_stage", definition_evidence,
                    _DEFINITION_EVIDENCE_COLUMNS)
        self._stage("reference_evidence_stage", reference_evidence,
                    _REFERENCE_EVIDENCE_COLUMNS)

        logger.info(
            "Staged %d repositories, %d files, %d symbols, %d edges, "
            "%d parameters, %d writes, %d accesses, %d class attributes, "
            "%d return sites, %d raise sites, %d call arguments, "
            "%d semantic coverage, %d definition evidence, %d reference evidence",
            len(repositories),
            len(files),
            len(symbols),
            len(edges),
            len(parameters),
            len(writes),
            len(accesses),
            len(class_attributes),
            len(return_sites),
            len(raise_sites),
            len(call_arguments),
            len(semantic_coverage),
            len(definition_evidence),
            len(reference_evidence),
        )
        # The scan only. Nothing authored is staged or resolved here: a
        # declaration cannot refuse the publication of current facts.
        self._call(_PUBLISH, {"scan": True, "batch_step_id": parent_batch_step_id})
        logger.info("Promoted the staged scan to the live index")

    def clear(self, parent_batch_step_id: Any) -> None:
        """Empty the index and its staging.

        Administrative, not part of indexing: the normal path replaces, so
        nothing has to run between a wipe and a repopulate.

        Args:
            parent_batch_step_id: The run's batch step. **Required** (#270), as
                for :meth:`replace_index`: ``code.p_index_clear`` records its own
                step beneath it.

        Raises:
            StateError: No parent step was supplied.
        """
        require_parent_batch_step(parent_batch_step_id, "clearing the code index")
        self._call(_CLEAR, {"batch_step_id": parent_batch_step_id})

    def _clear_staging(self) -> None:
        """Empty staging before filling it.

        A promotion empties staging itself, so this only matters after a run
        that staged and then failed to promote. Leaving those rows would let
        the next scan promote a mixture of two.
        """
        for table in ("control_flow_edge_stage", "control_flow_node_stage",
                      "control_flow_coverage_stage", "reference_evidence_stage", "definition_evidence_stage",
                      "semantic_coverage_stage", "call_argument_stage",
                      "raise_site_stage", "return_site_stage",
                      "class_attribute_stage",
                      "access_stage",
                      "assignment_stage", "parameter_stage",
                      "edge_stage", "symbol_stage", "file_stage",
                      "repository_stage"):
            DatabaseTable.delete_all_rows(
                self._connection,
                DatabaseObjectIdentity(connection="", catalog="", schema=STAGE_SCHEMA, name=table),
                self._adapter,
            )

    def _stage(
        self,
        table: str,
        rows: list[dict[str, Any]],
        columns: tuple[str, ...],
    ) -> None:
        """Insert staged rows through the table's bulk insert (backlog 753).

        Args:
            table: The staging table, unqualified.
            rows: What to insert. An empty list is a legitimate scan result and
                is not an error.
            columns: The column order the rows are written in.
        """
        if not rows:
            return
        DatabaseTable.bulk_insert(
            self._connection,
            DatabaseObjectIdentity(connection="", catalog="", schema=STAGE_SCHEMA, name=table),
            rows,
            list(columns),
            self._adapter,
        )

    def _call(self, binding: str, values: dict[str, Any]) -> None:
        """Invoke one of the index's governed write routines through the ``code`` map (#541).

        Not a built ``CALL`` string: the map is what makes this a governed operation,
        logs it in the run, and makes the call visible to the code index.

        Args:
            binding: The binding's name in the ``code`` map.
            values: The values its mapped parameters take.
        """
        ProcedureMap.resolve(
            self._connection, get_procedure_map(self._ctx, _MAP), _MAP, binding,
        ).call(self._connection, values, self._run_log)


def require_parent_batch_step(parent_batch_step_id: Any, publication: str) -> None:
    """Refuse a publication with no parent step, before anything is staged (#270).

    Each promotion routine records its own batch step beneath the parent, and
    ``control.f_batch_step_begin`` refuses when given neither a batch nor a
    parent -- "a governed routine never creates a batch". Saying so here beats a
    database error naming a control function the caller never invoked.

    Args:
        parent_batch_step_id: What the caller supplied.
        publication: What was being published, for the message.

    Raises:
        StateError: When no parent was supplied.
    """
    if parent_batch_step_id is None:
        raise StateError(
            f"{publication} needs the batch step it hangs beneath: the routine "
            "records its own step, and a governed routine never creates a batch. "
            "Run it as a workflow step, or pass the step the run already opened."
        )
