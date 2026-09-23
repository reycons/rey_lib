"""Publish a concluded capability generation.

**It concludes nothing.** An AI reads the index, decides what the estate
measurably is, and hands the conclusions over as a payload. This stages that
payload and asks the database to admit it. Where the capabilities came from and
whether they are right are not this module's questions, and keeping them out of
it is what leaves capability interpretation with one author.

Two boundaries decide the shape:

**Governed operations go through the procedure map; data movement does not.**
Staging rows is data movement and uses ``bulk_insert``, the same call the code
index stages with. Promotion is a governed operation and goes through a mapped
routine, which is also what makes the code-to-database seam visible to the
index -- a call assembled as a string is invisible to ``code.edge_vw``.

**The schema owns atomicity.** There is no Python transaction here, and
``DBAdapter`` offers none: PostgreSQL connections in this estate are autocommit
by contract. Validation, the durable writes and the stage drain happen inside
one procedure call, so a refusal leaves the previous description exactly as it
was.

**Staging is scoped to one publication.** Every row carries a
``publication_key``, and both governed calls are scoped to it. Without that,
two passes racing would let one delete the other's staged rows and then promote
them as its own -- and the "exactly one staged generation" check would not
notice, because after the race there IS exactly one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from rey_lib.db.procedure_map import execute_mapped_routine
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import get_logger

__all__ = [
    "CapabilityPublisher", "PublicationError", "StagedCapability",
    "StagedEvidence", "StagedGeneration", "generation_from_payload",
]

_logger = get_logger(__name__)

#: The map these routines are bound in, and the bindings. Named once.
_MAP = "code"
_CLEAR = "clear_capability_stage"
_PUBLISH = "publish_system_capability"

_SCHEMA = "code"
_GENERATION_TABLE = "system_capability_generation_stage"
_CAPABILITY_TABLE = "system_capability_stage"
_EVIDENCE_TABLE = "system_capability_evidence_stage"

_GENERATION_COLUMNS = (
    "publication_key", "generated_ts", "index_indexed_ts",
    "index_repository_count", "generator", "ai_contract_id", "ai_task_id",
)
_CAPABILITY_COLUMNS = (
    "publication_key", "capability_key", "label", "statement", "maturity",
    "sort_order",
)
_EVIDENCE_COLUMNS = (
    "publication_key", "capability_key", "population_kind", "reference",
    "population_size", "matched_count", "sort_order",
)


class PublicationError(AppError):
    """A payload that does not describe a generation."""


@dataclass(frozen=True)
class StagedEvidence:
    """One measurement supporting one capability, as M of N."""

    population_kind: str
    reference: str
    population_size: int
    matched_count: int
    sort_order: int = 0


@dataclass(frozen=True)
class StagedCapability:
    """One concluded capability and what supports it."""

    capability_key: str
    label: str
    statement: str
    maturity: str
    evidence: tuple[StagedEvidence, ...]
    sort_order: int = 0


@dataclass(frozen=True)
class StagedGeneration:
    """One analysis pass, ready to be admitted.

    Carries its own provenance -- the estate it read, the model that read it,
    and the CONTRACT and TASK it followed -- because a later description is
    uninterpretable unless you can tell which of those moved.

    The contract pins the exact method version, which is enough on its own only
    until a second task reuses the same contract. The task is recorded beside
    it for that reason. The binding is not recorded: it is mutable, so its id
    would pin a row that changes underneath the generation.
    """

    generated_ts: Any
    index_indexed_ts: Any
    index_repository_count: int
    generator: str
    ai_contract_id: int
    ai_task_id: int
    capabilities: tuple[StagedCapability, ...] = field(default_factory=tuple)


def generation_from_payload(payload: dict[str, Any]) -> StagedGeneration:
    """Build a generation from the document an analysis pass produced.

    Refuses rather than filling in: a payload missing a field is a pass that
    did not finish, and defaulting one here would admit a description nobody
    concluded.

    Args:
        payload: The concluded document.

    Returns:
        The generation, ready to stage.

    Raises:
        PublicationError: A field is absent, or the payload holds no
            capabilities.
    """
    try:
        capabilities = tuple(
            StagedCapability(
                capability_key=one["capability_key"],
                label=one["label"],
                statement=one["statement"],
                maturity=one["maturity"],
                sort_order=int(one.get("sort_order", 0)),
                evidence=tuple(
                    StagedEvidence(
                        population_kind=item["population_kind"],
                        reference=item["reference"],
                        population_size=int(item["population_size"]),
                        matched_count=int(item["matched_count"]),
                        sort_order=int(item.get("sort_order", 0)),
                    )
                    for item in one["evidence"]
                ),
            )
            for one in payload["capabilities"]
        )
        generation = StagedGeneration(
            generated_ts=payload["generated_ts"],
            index_indexed_ts=payload["index_indexed_ts"],
            index_repository_count=int(payload["index_repository_count"]),
            generator=payload["generator"],
            ai_contract_id=int(payload["ai_contract_id"]),
            ai_task_id=int(payload["ai_task_id"]),
            capabilities=capabilities,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationError(
            f"the payload does not describe a generation: {exc}"
        ) from exc

    if not generation.capabilities:
        # The procedure refuses this too. Saying so here names the payload
        # rather than the stage, which is what the author can act on.
        raise PublicationError(
            "the payload concluded no capabilities. A pass that found nothing "
            "is a finding about the pass, not a description."
        )
    return generation


class CapabilityPublisher:
    """Stages one generation and asks the database to admit it."""

    def __init__(self, adapter: Any, connection: Any) -> None:
        """Hold what was already resolved.

        Neither is opened here. The application bootstraps, resolves the
        connection its configuration names, and hands it over; a publisher that
        found its own would be a second answer to which database this is.

        Args:
            adapter: The provider-dispatching adapter, for staging.
            connection: An open connection for the publishing role.
        """
        self._adapter = adapter
        self._connection = connection

    def publish(
        self,
        ctx: Any,
        run_log: Any,
        generation: StagedGeneration,
        parent_batch_step_id: Any,
    ) -> str:
        """Stage the generation and promote it, or raise.

        Args:
            ctx: Application context, for resolving the map and logging.
            run_log: The run's evidence recorder.
            generation: What the analysis pass concluded.
            parent_batch_step_id: The step this publication hangs beneath.
                **Required.** Both routines register a batch step, and
                ``control.f_batch_step_begin`` refuses when given neither a
                batch nor a parent -- "a governed routine never creates a
                batch". So a publication outside a run is not possible, and
                saying so here beats a database error naming a control
                function the caller never invoked.

        Returns:
            The publication key the rows were staged under.

        Raises:
            PublicationError: No parent step was supplied.
            Any refusal the procedure raises. Nothing is caught here: a
            rejected generation must not look like a published one.
        """
        if parent_batch_step_id is None:
            raise PublicationError(
                "publishing a capability generation needs the batch step it "
                "hangs beneath: both routines register their own step, and a "
                "governed routine never creates a batch. Run it as a workflow "
                "step, or pass the step a run already opened."
            )

        # Opaque, and per ATTEMPT rather than per run: two attempts within one
        # run would share a run id or a batch step, and then could not be told
        # apart in staging.
        publication_key = uuid.uuid4().hex

        # Scoped, so this clears only a previous attempt under this same key.
        # It recovers nothing from a pass that failed under a different one --
        # those rows are invisible to every later publication and are collected
        # by nothing.
        self._call(ctx, run_log, _CLEAR, publication_key, parent_batch_step_id)

        self._stage(publication_key, generation)

        _logger.info(
            "Staged %d capabilities and %d evidence items for publication %s",
            len(generation.capabilities),
            sum(len(one.evidence) for one in generation.capabilities),
            publication_key,
        )

        self._call(ctx, run_log, _PUBLISH, publication_key, parent_batch_step_id)
        _logger.info("Admitted capability generation %s", publication_key)
        return publication_key

    def _stage(self, publication_key: str, generation: StagedGeneration) -> None:
        """Fill the three stage tables, every row carrying the key.

        Ordinary inserts, deliberately without a transaction: nothing reads
        staging, and rows under a key nothing promotes mean nothing.
        """
        self._insert(_GENERATION_TABLE, _GENERATION_COLUMNS, [{
            "publication_key": publication_key,
            "generated_ts": generation.generated_ts,
            "index_indexed_ts": generation.index_indexed_ts,
            "index_repository_count": generation.index_repository_count,
            "generator": generation.generator,
            "ai_contract_id": generation.ai_contract_id,
            "ai_task_id": generation.ai_task_id,
        }])

        self._insert(_CAPABILITY_TABLE, _CAPABILITY_COLUMNS, [
            {
                "publication_key": publication_key,
                "capability_key": one.capability_key,
                "label": one.label,
                "statement": one.statement,
                "maturity": one.maturity,
                "sort_order": one.sort_order,
            }
            for one in generation.capabilities
        ])

        self._insert(_EVIDENCE_TABLE, _EVIDENCE_COLUMNS, [
            {
                "publication_key": publication_key,
                "capability_key": one.capability_key,
                "population_kind": item.population_kind,
                "reference": item.reference,
                "population_size": item.population_size,
                "matched_count": item.matched_count,
                "sort_order": item.sort_order,
            }
            for one in generation.capabilities
            for item in one.evidence
        ])

    def _insert(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: list[dict[str, Any]],
    ) -> None:
        """Stage rows through the adapter's bulk mechanism."""
        if not rows:
            return
        self._adapter.bulk_insert(
            self._connection, _SCHEMA, table, rows, list(columns)
        )

    def _call(
        self,
        ctx: Any,
        run_log: Any,
        binding: str,
        publication_key: str,
        parent_batch_step_id: Any,
    ) -> None:
        """Invoke one governed operation through the map.

        Not a built ``CALL`` string. The map is what makes this a governed
        operation, what lets the routine record its own batch step beneath the
        supplied parent, and what makes the call visible to the code index --
        a procedure name passed as a string literal appears in no edge.
        """
        execute_mapped_routine(
            ctx, run_log, self._connection, _MAP, binding,
            {
                "publication_key": publication_key,
                "batch_step_id": parent_batch_step_id,
            },
        )
