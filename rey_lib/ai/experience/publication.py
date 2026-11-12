"""Publish one generation of AI experience (#1344 slice 2).

**It concludes nothing.** What an experience says, and what supports it, is
decided before this is called; this stages it and asks the database to admit
it. Whether the evidence supports the conclusion -- validation evidence for a
validated or rejected experience, an outcome for a validated episode, citations
without a cycle, code evidence that resolves, provenance the originating run's
LLM_PACKAGE agrees with -- is ``ai.p_ai_experience_publish``'s to refuse, once,
at admission.

The boundaries are the inferred-architecture publisher's, for the same reasons:

**Governed operations go through the procedure map; data movement does not.**
Staging is ``bulk_insert`` into ``ai_stage``; clearing and admitting are the
``control`` map's ``clear_experience_stage`` and ``publish_experience``, bound
to the fully qualified ``ai`` routines in the publishing installation's map.

**The host owns installation context.** As with the AI's construction
boundary, the hosting application resolves the connection and the ``control``
map entry and hands them in; nothing here reads or keeps installation context.

**The schema owns atomicity.** Connections are autocommit by contract, so
there is no Python transaction. Validation, the durable writes and the stage
drain happen inside one procedure call: a refusal leaves published experience
exactly as it was.

**Staging is scoped to one publication.** Every row carries a
``publication_key``, fresh per attempt, and both governed calls are scoped to
it, so two passes can never clear or promote each other's rows.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.procedure_map import ProcedureMap
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger

__all__ = [
    "ExperienceIdentity", "ExperiencePublisher", "PublicationError",
    "StagedCodeEvidence", "StagedDependency", "StagedEvidence", "StagedExperience",
    "StagedGeneration",
]

_logger = AppLogger(__name__)

#: The map these routines are bound in, and the bindings. Named once.
_MAP = "control"
_CLEAR = "clear_experience_stage"
_PUBLISH = "publish_experience"

_SCHEMA = "ai_stage"
_GENERATION_TABLE = "ai_experience_generation_stage"
_EXPERIENCE_TABLE = "ai_experience_stage"
_EVIDENCE_TABLE = "ai_experience_evidence_stage"
_CODE_EVIDENCE_TABLE = "ai_experience_code_evidence_stage"
_DEPENDENCY_TABLE = "ai_experience_dependency_stage"

_IDENTITY_COLUMNS = ("installation_id", "application_id", "subject_identity", "experience_key")
_GENERATION_COLUMNS = (
    "publication_key", "generated_ts", "generator", "recording_act", "recording_run_manifest_id",
)
_EXPERIENCE_COLUMNS = (
    "publication_key", *_IDENTITY_COLUMNS, "experience_level", "statement", "knowledge_kind",
    "validation_state", "originating_run_manifest_id", "ai_task_id", "ai_contract_id",
)
_EVIDENCE_COLUMNS = (
    "publication_key", *_IDENTITY_COLUMNS, "evidence_role", "run_manifest_id", "run_log_id",
    "batch_step_id", "file_mutation_id", "message_id", "supporting_ai_experience_id",
    "supporting_installation_id", "supporting_application_id", "supporting_subject_identity",
    "supporting_experience_key",
)
_CODE_EVIDENCE_COLUMNS = (
    "publication_key", *_IDENTITY_COLUMNS, "evidence_role", "repository_key", "identity",
    "member_kind",
)
_DEPENDENCY_COLUMNS = (
    "publication_key", *_IDENTITY_COLUMNS, "ai_contract_id", "development_rule_id",
    "file_mutation_id",
)


class PublicationError(AppError):
    """A publication attempted outside a run."""


@dataclass(frozen=True)
class ExperienceIdentity:
    """What an experience is about and its key: the identity a generation supersedes."""

    installation_id: int
    application_id: int
    subject_identity: dict[str, Any]
    experience_key: str

    def staged(self, prefix: str = "") -> dict[str, Any]:
        """The identity as stage columns; ``subject_identity`` as JSON text for the jsonb column.

        Measured on the real bulk_insert path (psycopg 3, rolled back, 2026-11-12):
        JSON text is stored as the jsonb object; a dict is refused by the driver.
        """
        return {
            f"{prefix}installation_id": self.installation_id,
            f"{prefix}application_id": self.application_id,
            f"{prefix}subject_identity": json.dumps(self.subject_identity),
            f"{prefix}experience_key": self.experience_key,
        }


@dataclass(frozen=True)
class StagedEvidence:
    """One record supporting an experience: exactly one of the record ids, or a citation.

    A citation names a published experience by ``supporting_ai_experience_id``,
    or one staged in the same publication by ``cites``.
    """

    evidence_role: str
    run_manifest_id: int | None = None
    run_log_id: int | None = None
    batch_step_id: int | None = None
    file_mutation_id: int | None = None
    message_id: int | None = None
    supporting_ai_experience_id: int | None = None
    cites: ExperienceIdentity | None = None


@dataclass(frozen=True)
class StagedCodeEvidence:
    """One indexed identity supporting an experience."""

    evidence_role: str
    repository_key: str
    identity: str
    member_kind: str


@dataclass(frozen=True)
class StagedDependency:
    """One thing an experience depends on: exactly one of the three."""

    ai_contract_id: int | None = None
    development_rule_id: int | None = None
    file_mutation_id: int | None = None


@dataclass(frozen=True)
class StagedExperience:
    """One experience, with what supports it and what it depends on."""

    identity: ExperienceIdentity
    experience_level: str
    statement: str
    knowledge_kind: str
    validation_state: str
    originating_run_manifest_id: int | None = None
    ai_task_id: int | None = None
    ai_contract_id: int | None = None
    evidence: tuple[StagedEvidence, ...] = field(default_factory=tuple)
    code_evidence: tuple[StagedCodeEvidence, ...] = field(default_factory=tuple)
    dependencies: tuple[StagedDependency, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StagedGeneration:
    """One recording act -- extraction, correction or validation -- ready to be admitted."""

    generated_ts: Any
    generator: str
    recording_act: str
    recording_run_manifest_id: int | None = None
    experiences: tuple[StagedExperience, ...] = field(default_factory=tuple)


class ExperiencePublisher:
    """Stages one generation and asks the database to admit it."""

    def __init__(self, adapter: Any, connection: Any, procedure_map: Any) -> None:
        """Hold what the host already resolved.

        Args:
            adapter: The provider-dispatching adapter, for staging.
            connection: An open connection for the publishing role.
            procedure_map: The installation's ``control`` map entry, as the
                host resolved it; its bindings are resolved per call through
                ``ProcedureMap.resolve``.
        """
        self._adapter = adapter
        self._connection = connection
        self._procedure_map = procedure_map

    def publish(
        self,
        run_log: Any,
        generation: StagedGeneration,
        parent_batch_step_id: Any,
    ) -> str:
        """Stage the generation and promote it, or raise.

        Args:
            run_log: The run's evidence recorder.
            generation: The recording act and its experiences.
            parent_batch_step_id: The step this publication hangs beneath.
                **Required.** Both routines register a batch step, and
                ``control.f_batch_step_begin`` refuses when given neither a
                batch nor a parent. The routines accept a NULL parent at the
                signature, so this is where it is refused, by name.

        Returns:
            The publication key the rows were staged under.

        Raises:
            PublicationError: No parent step was supplied.
            Any refusal the procedure raises. Nothing is caught here: a
            refused generation must not look like a published one.
        """
        if parent_batch_step_id is None:
            raise PublicationError(
                "publishing AI experience needs the batch step it hangs beneath: both "
                "routines register their own step, and a governed routine never creates "
                "a batch. Run it as a workflow step, or pass the step a run already opened."
            )

        # Opaque and per ATTEMPT, so two attempts can never share staged rows.
        publication_key = uuid.uuid4().hex

        # Scoped: clears only an earlier attempt under this same key.
        self._call(run_log, _CLEAR, publication_key, parent_batch_step_id)

        self._stage(publication_key, generation)
        _logger.info(
            "Staged %d experiences, %d evidence, %d code evidence and %d dependencies "
            "for publication %s",
            len(generation.experiences),
            sum(len(one.evidence) for one in generation.experiences),
            sum(len(one.code_evidence) for one in generation.experiences),
            sum(len(one.dependencies) for one in generation.experiences),
            publication_key,
        )

        self._call(run_log, _PUBLISH, publication_key, parent_batch_step_id)
        _logger.info("Admitted AI experience generation %s", publication_key)
        return publication_key

    def _stage(self, publication_key: str, generation: StagedGeneration) -> None:
        """Fill the five stage tables, every row carrying the key."""
        key = {"publication_key": publication_key}
        self._insert(_GENERATION_TABLE, _GENERATION_COLUMNS, [{
            **key,
            "generated_ts": generation.generated_ts,
            "generator": generation.generator,
            "recording_act": generation.recording_act,
            "recording_run_manifest_id": generation.recording_run_manifest_id,
        }])
        self._insert(_EXPERIENCE_TABLE, _EXPERIENCE_COLUMNS, [
            {
                **key,
                **one.identity.staged(),
                "experience_level": one.experience_level,
                "statement": one.statement,
                "knowledge_kind": one.knowledge_kind,
                "validation_state": one.validation_state,
                "originating_run_manifest_id": one.originating_run_manifest_id,
                "ai_task_id": one.ai_task_id,
                "ai_contract_id": one.ai_contract_id,
            }
            for one in generation.experiences
        ])
        self._insert(_EVIDENCE_TABLE, _EVIDENCE_COLUMNS, [
            {
                **key,
                **one.identity.staged(),
                "evidence_role": item.evidence_role,
                "run_manifest_id": item.run_manifest_id,
                "run_log_id": item.run_log_id,
                "batch_step_id": item.batch_step_id,
                "file_mutation_id": item.file_mutation_id,
                "message_id": item.message_id,
                "supporting_ai_experience_id": item.supporting_ai_experience_id,
                **_cited(item.cites),
            }
            for one in generation.experiences
            for item in one.evidence
        ])
        self._insert(_CODE_EVIDENCE_TABLE, _CODE_EVIDENCE_COLUMNS, [
            {
                **key,
                **one.identity.staged(),
                "evidence_role": item.evidence_role,
                "repository_key": item.repository_key,
                "identity": item.identity,
                "member_kind": item.member_kind,
            }
            for one in generation.experiences
            for item in one.code_evidence
        ])
        self._insert(_DEPENDENCY_TABLE, _DEPENDENCY_COLUMNS, [
            {
                **key,
                **one.identity.staged(),
                "ai_contract_id": item.ai_contract_id,
                "development_rule_id": item.development_rule_id,
                "file_mutation_id": item.file_mutation_id,
            }
            for one in generation.experiences
            for item in one.dependencies
        ])

    def _insert(self, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
        """Stage rows through the table's bulk insert."""
        if not rows:
            return
        DatabaseTable.bulk_insert(
            self._connection,
            DatabaseObjectIdentity(connection="", catalog="", schema=_SCHEMA, name=table),
            rows,
            list(columns),
            self._adapter,
        )

    def _call(
        self,
        run_log: Any,
        binding: str,
        publication_key: str,
        parent_batch_step_id: Any,
    ) -> None:
        """Invoke one governed operation through the map, never an assembled statement."""
        procedure = ProcedureMap.resolve(self._connection, self._procedure_map, _MAP, binding)
        procedure.call(
            self._connection,
            {"publication_key": publication_key, "batch_step_id": parent_batch_step_id},
            run_log,
        )


def _cited(cites: ExperienceIdentity | None) -> dict[str, Any]:
    """A staged citation's identity columns; all NULL when the evidence cites nothing staged."""
    if cites is None:
        return {f"supporting_{name}": None for name in _IDENTITY_COLUMNS}
    return cites.staged("supporting_")
