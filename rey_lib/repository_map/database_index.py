"""The relational destination for a database index.

Sibling of :mod:`code_index_database`, and the same contract: fill the schema's
staging tables, call the schema's promotion procedure, hold no privilege on
storage. ``code.p_publish_database`` validates, empties storage, repopulates it
from staging and empties staging inside one call, so a failure anywhere leaves
the previous index exactly as it was.

**One source row for the database, never one per schema.** The source is the
database the connection reaches; the schema is a column on the object. An index
holding one schema is an incomplete publication of one source, not several
sources.

**Metadata only.** What arrives here came from the catalog and from routine
definitions. No row of any indexed table is read, and nothing here would know
what to do with one.
"""

from __future__ import annotations

from typing import Any

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.logs.logging_setup import get_logger

__all__ = ["DatabaseIndexWriter"]

logger = get_logger(__name__)

SCHEMA = "code"

_SOURCE_COLUMNS = ("source_key", "provider", "catalog_name", "generator_version")
_OBJECT_COLUMNS = (
    "source_key", "schema_name", "object_name", "object_type", "signature",
    "provider_object_id", "definition_hash", "reference_analysis_status",
    # The text, not only its hash. A reader opening the object needs what it
    # says; the hash only says whether it changed.
    "definition",
)
_MEMBER_COLUMNS = (
    "source_key", "schema_name", "object_name", "object_type", "signature",
    "member_kind", "member_name", "ordinal", "data_type", "member_mode",
    "provider_member_id",
)
_OBSERVATION_COLUMNS = (
    "from_source_key", "from_schema_name", "from_object_name",
    "from_object_type", "from_signature", "from_member_kind",
    "from_member_name", "from_ordinal",
    "to_source_key", "to_schema_name", "to_object_name", "to_object_type",
    "to_signature", "to_member_kind", "to_member_name", "to_ordinal",
    "reference_kind", "observed_target", "resolution",
    "observed_definition_hash", "statement_ordinal", "source_line",
    "source_column", "fact_source", "evidence",
)

#: Emptied before filling, so a previous run that staged and failed to promote
#: cannot be published as part of the next one.
_STAGING = (
    "db_observation_stage",
    "db_member_stage",
    "db_object_stage",
    "db_source_stage",
)


class DatabaseIndexWriter:
    """Stages one database's catalog facts and promotes them in one call."""

    def __init__(self, adapter: DBAdapter, connection: Any) -> None:
        """Hold the adapter and the connection the index is published through.

        Args:
            adapter: The database adapter.
            connection: An open connection handle.
        """
        self._adapter = adapter
        self._connection = connection

    def replace_index(
        self,
        *,
        source_key: str,
        provider: str,
        catalog_name: str,
        generator_version: str,
        objects: list[dict[str, Any]],
        members: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> None:
        """Replace the published database index with this inspection.

        Args:
            source_key: Identity of the database, not of a schema.
            provider: The provider the facts came from.
            catalog_name: The catalog the connection reaches.
            generator_version: What produced this inspection.
            objects: Normalized objects, without their source key.
            members: Normalized members, without their source key.
            observations: Reference observations, without their source keys.

        Raises:
            DatabaseError: If staging or promotion fails. The published index is
                untouched in that case, which is the schema's guarantee and not
                this writer's.
        """
        self._clear_staging()

        self._stage("db_source_stage", [{
            "source_key": source_key,
            "provider": provider,
            "catalog_name": catalog_name,
            "generator_version": generator_version,
        }], _SOURCE_COLUMNS)

        self._stage(
            "db_object_stage",
            [{**row, "source_key": source_key} for row in objects],
            _OBJECT_COLUMNS,
        )
        self._stage(
            "db_member_stage",
            [{**row, "source_key": source_key} for row in members],
            _MEMBER_COLUMNS,
        )
        # The origin always carries the source key. The TARGET carries it only
        # where the observation resolved: the schema's own contract is
        # (resolution = 'exact') <> (to_source_key <> ''), so a target source
        # on an unresolved observation claims a binding that was never made,
        # and the publication refuses it -- correctly.
        self._stage(
            "db_observation_stage",
            [
                {
                    **row,
                    "from_source_key": source_key,
                    "to_source_key": (
                        source_key if str(row.get("resolution")) == "exact" else ""
                    ),
                }
                for row in observations
            ],
            _OBSERVATION_COLUMNS,
        )

        logger.debug(
            "Staged %d objects, %d members, %d observations for %s",
            len(objects), len(members), len(observations), source_key,
        )
        self._call("p_publish_database")

    def _clear_staging(self) -> None:
        """Empty every staging table, deepest dependency first."""
        for table in _STAGING:
            self._adapter.execute_sql(
                self._connection, f"DELETE FROM {SCHEMA}.{table}", {}, "no_return",
            )

    def _stage(
        self, table: str, rows: list[dict[str, Any]], columns: tuple[str, ...],
    ) -> None:
        """Insert staged rows through the adapter's bulk mechanism.

        Args:
            table: The staging table, unqualified.
            rows: What to insert. Empty is a legitimate result, not an error.
            columns: The column order the rows are written in.
        """
        if not rows:
            return
        self._adapter.bulk_insert(
            self._connection, SCHEMA, table, rows, list(columns)
        )

    def _call(self, procedure: str) -> None:
        """Call the schema's promotion procedure.

        Args:
            procedure: The unqualified procedure name.
        """
        self._adapter.execute_sql(
            self._connection, f"CALL {SCHEMA}.{procedure}()", {}, "no_return",
        )
