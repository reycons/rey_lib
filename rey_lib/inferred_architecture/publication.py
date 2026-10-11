"""Publish an inferred-architecture generation.

**It infers nothing.** An AI reads the index, the derived evidence views and
the canonical claims, decides what architecture the evidence supports, and
hands that over as one JSON document (AI contract ``inferred_architecture``).
This checks the document has the contract's shape, stages it, and asks the
database to admit it. Whether the areas are right is not this module's
question; keeping it out is what leaves architectural interpretation with one
author.

The boundaries are the capability publisher's, for the same reasons:

**Governed operations go through the procedure map; data movement does not.**
Staging is ``bulk_insert``; clearing and admitting are mapped routines, which
is also what makes this code-to-database seam visible to the code index.

**The schema owns atomicity.** Connections are autocommit by contract, so
there is no Python transaction. Validation, the durable writes and the stage
drain happen inside one procedure call: a refusal leaves the previous
architecture exactly as it was.

**Staging is scoped to one publication.** Every row carries a
``publication_key``, fresh per attempt, and both governed calls are scoped to
it, so two passes can never clear or promote each other's rows.

What the payload must look like is checked here, before anything is staged:
missing fields, empty lists, unknown kinds and digits in a statement are
refused by name. What it must MEAN against the index -- a parent that exists,
no cycle, memberships that name indexed code, non-zero evidence, the declared
task and contract -- is the procedure's to refuse, once, at admission.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.procedure_map import ProcedureMap, get_procedure_map
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger

__all__ = [
    "ArchitecturePublisher", "PublicationError", "StagedArea", "StagedEvidence",
    "StagedGeneration", "StagedMembership", "generation_from_payload",
]

_logger = AppLogger(__name__)

#: The map these routines are bound in, and the bindings. Named once.
_MAP = "code"
_CLEAR = "clear_inferred_architecture_stage"
_PUBLISH = "publish_inferred_architecture"

_SCHEMA = "code"
_GENERATION_TABLE = "inferred_architecture_generation_stage"
_AREA_TABLE = "inferred_architecture_area_stage"
_MEMBERSHIP_TABLE = "inferred_architecture_membership_stage"
_EVIDENCE_TABLE = "inferred_architecture_evidence_stage"

_GENERATION_COLUMNS = (
    "publication_key", "generated_ts", "index_indexed_ts",
    "index_repository_count", "generator", "ai_contract_id", "ai_task_id",
)
_AREA_COLUMNS = (
    "publication_key", "area_key", "parent_area_key", "label", "statement",
    "sort_order",
)
_MEMBERSHIP_COLUMNS = (
    "publication_key", "area_key", "repository_key", "identity", "member_kind",
    "sort_order",
)
_EVIDENCE_COLUMNS = (
    "publication_key", "area_key", "population_kind", "reference",
    "population_size", "matched_count", "sort_order",
)

#: What the contract allows, as the stage CHECKs declare it.
MEMBER_KINDS = frozenset({"package", "module", "symbol"})
EVIDENCE_POPULATIONS = frozenset({
    "symbol", "edge", "repository", "db_object", "db_member", "code_relation",
    "package", "canonical_object",
})


class PublicationError(AppError):
    """A payload that does not describe a generation, or a publication outside a run."""


@dataclass(frozen=True)
class StagedEvidence:
    """One measurement supporting one area, as M of N."""

    population_kind: str
    reference: str
    population_size: int
    matched_count: int
    sort_order: int = 0


@dataclass(frozen=True)
class StagedMembership:
    """One indexed identity an area contains."""

    repository_key: str
    identity: str
    member_kind: str
    sort_order: int = 0


@dataclass(frozen=True)
class StagedArea:
    """One inferred area, its place in the tree, what it contains and what supports it."""

    area_key: str
    parent_area_key: str | None
    label: str
    statement: str
    memberships: tuple[StagedMembership, ...]
    evidence: tuple[StagedEvidence, ...]
    sort_order: int = 0


@dataclass(frozen=True)
class StagedGeneration:
    """One inference pass, ready to be admitted.

    Carries its provenance -- the index it read, the model, and the CONTRACT
    and TASK it followed -- as the capability generation does: a later
    architecture is uninterpretable unless you can tell which of those moved.
    """

    generated_ts: Any
    index_indexed_ts: Any
    index_repository_count: int
    generator: str
    ai_contract_id: int
    ai_task_id: int
    areas: tuple[StagedArea, ...] = field(default_factory=tuple)


def _text(value: Any, name: str) -> str:
    """A required, non-blank string."""
    if not isinstance(value, str) or not value.strip():
        raise PublicationError(f"the payload's {name} is missing or blank")
    return value


def _number(value: Any, name: str) -> int:
    """A required integer; a bool is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PublicationError(f"the payload's {name} is not a number")
    try:
        return int(value)
    except ValueError as exc:
        raise PublicationError(f"the payload's {name} is not a number") from exc


def _evidence(item: Any, area_key: str) -> StagedEvidence:
    """One evidence entry of one area."""
    if not isinstance(item, dict):
        raise PublicationError(f"area {area_key}: an evidence entry is not an object")
    kind = _text(item.get("population_kind"), f"area {area_key} evidence population_kind")
    if kind not in EVIDENCE_POPULATIONS:
        raise PublicationError(
            f"area {area_key}: evidence population {kind!r} is not an evidence population "
            f"({', '.join(sorted(EVIDENCE_POPULATIONS))}); the previous generation is never evidence"
        )
    size = _number(item.get("population_size"), f"area {area_key} evidence population_size")
    matched = _number(item.get("matched_count"), f"area {area_key} evidence matched_count")
    if size <= 0 or not 0 <= matched <= size:
        raise PublicationError(
            f"area {area_key}: evidence must be M of N with N > 0 and 0 <= M <= N, got {matched} of {size}"
        )
    return StagedEvidence(
        population_kind=kind,
        reference=_text(item.get("reference"), f"area {area_key} evidence reference"),
        population_size=size,
        matched_count=matched,
        sort_order=_number(item.get("sort_order", 0), f"area {area_key} evidence sort_order"),
    )


def _membership(item: Any, area_key: str) -> StagedMembership:
    """One membership of one area."""
    if not isinstance(item, dict):
        raise PublicationError(f"area {area_key}: a membership is not an object")
    kind = _text(item.get("member_kind"), f"area {area_key} membership member_kind")
    if kind not in MEMBER_KINDS:
        raise PublicationError(
            f"area {area_key}: member_kind {kind!r} is not one of {', '.join(sorted(MEMBER_KINDS))}"
        )
    return StagedMembership(
        repository_key=_text(item.get("repository_key"), f"area {area_key} membership repository_key"),
        identity=_text(item.get("identity"), f"area {area_key} membership identity"),
        member_kind=kind,
        sort_order=_number(item.get("sort_order", 0), f"area {area_key} membership sort_order"),
    )


def _area(item: Any) -> StagedArea:
    """One area, with its memberships and evidence."""
    if not isinstance(item, dict):
        raise PublicationError("an area is not an object")
    area_key = _text(item.get("area_key"), "area_key")
    if "parent_area_key" not in item:
        raise PublicationError(f"area {area_key}: parent_area_key is absent (null for a root)")
    parent = item["parent_area_key"]
    if parent is not None:
        parent = _text(parent, f"area {area_key} parent_area_key")
    statement = _text(item.get("statement"), f"area {area_key} statement")
    # The stage CHECK is the authority; refusing here means nothing is staged first.
    if re.search(r"[0-9]", statement):
        raise PublicationError(f"area {area_key}: the statement carries a digit; figures belong in evidence")
    memberships = item.get("memberships")
    evidence = item.get("evidence")
    if not isinstance(memberships, list) or not memberships:
        raise PublicationError(f"area {area_key}: no memberships -- an inferred area must contain indexed code")
    if not isinstance(evidence, list) or not evidence:
        raise PublicationError(f"area {area_key}: no evidence -- an area the evidence does not support is not written")
    return StagedArea(
        area_key=area_key,
        parent_area_key=parent,
        label=_text(item.get("label"), f"area {area_key} label"),
        statement=statement,
        memberships=tuple(_membership(one, area_key) for one in memberships),
        evidence=tuple(_evidence(one, area_key) for one in evidence),
        sort_order=_number(item.get("sort_order", 0), f"area {area_key} sort_order"),
    )


def generation_from_payload(payload: Any) -> StagedGeneration:
    """Build a generation from the document an inference pass produced.

    Refuses rather than filling in: a payload missing a field is a pass that
    did not finish, and defaulting one here would admit an architecture nobody
    inferred. Only ``sort_order`` defaults, as in the capability payload.

    Raises:
        PublicationError: The document does not have the contract's shape.
    """
    if not isinstance(payload, dict):
        raise PublicationError("the payload is not a JSON object")
    for name in ("generated_ts", "index_indexed_ts", "index_repository_count",
                 "generator", "ai_contract_id", "ai_task_id", "areas"):
        if name not in payload:
            raise PublicationError(f"the payload does not describe a generation: {name} is absent")
    areas = payload["areas"]
    if not isinstance(areas, list) or not areas:
        raise PublicationError(
            "the payload inferred no areas. A pass that found nothing is a finding about the pass, not an architecture."
        )
    staged = tuple(_area(one) for one in areas)
    keys = [one.area_key for one in staged]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise PublicationError(f"the payload repeats area_key: {', '.join(duplicated)}")
    count = _number(payload["index_repository_count"], "index_repository_count")
    if count <= 0:
        raise PublicationError("the payload's index_repository_count must be positive")
    return StagedGeneration(
        generated_ts=_text(payload["generated_ts"], "generated_ts"),
        index_indexed_ts=payload["index_indexed_ts"],
        index_repository_count=count,
        generator=_text(payload["generator"], "generator"),
        ai_contract_id=_number(payload["ai_contract_id"], "ai_contract_id"),
        ai_task_id=_number(payload["ai_task_id"], "ai_task_id"),
        areas=staged,
    )


class ArchitecturePublisher:
    """Stages one generation and asks the database to admit it."""

    def __init__(self, adapter: Any, connection: Any) -> None:
        """Hold what was already resolved.

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
            generation: What the inference pass concluded.
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
            rejected generation must not look like a published one.
        """
        if parent_batch_step_id is None:
            raise PublicationError(
                "publishing an inferred-architecture generation needs the batch step it "
                "hangs beneath: both routines register their own step, and a governed "
                "routine never creates a batch. Run it as a workflow step, or pass the "
                "step a run already opened."
            )

        # Opaque and per ATTEMPT, so two attempts can never share staged rows.
        publication_key = uuid.uuid4().hex

        # Scoped: clears only an earlier attempt under this same key.
        self._call(ctx, run_log, _CLEAR, publication_key, parent_batch_step_id)

        self._stage(publication_key, generation)
        _logger.info(
            "Staged %d areas, %d memberships and %d evidence items for publication %s",
            len(generation.areas),
            sum(len(one.memberships) for one in generation.areas),
            sum(len(one.evidence) for one in generation.areas),
            publication_key,
        )

        self._call(ctx, run_log, _PUBLISH, publication_key, parent_batch_step_id)
        _logger.info("Admitted inferred-architecture generation %s", publication_key)
        return publication_key

    def _stage(self, publication_key: str, generation: StagedGeneration) -> None:
        """Fill the four stage tables, every row carrying the key."""
        self._insert(_GENERATION_TABLE, _GENERATION_COLUMNS, [{
            "publication_key": publication_key,
            "generated_ts": generation.generated_ts,
            "index_indexed_ts": generation.index_indexed_ts,
            "index_repository_count": generation.index_repository_count,
            "generator": generation.generator,
            "ai_contract_id": generation.ai_contract_id,
            "ai_task_id": generation.ai_task_id,
        }])
        self._insert(_AREA_TABLE, _AREA_COLUMNS, [
            {
                "publication_key": publication_key,
                "area_key": one.area_key,
                "parent_area_key": one.parent_area_key,
                "label": one.label,
                "statement": one.statement,
                "sort_order": one.sort_order,
            }
            for one in generation.areas
        ])
        self._insert(_MEMBERSHIP_TABLE, _MEMBERSHIP_COLUMNS, [
            {
                "publication_key": publication_key,
                "area_key": one.area_key,
                "repository_key": item.repository_key,
                "identity": item.identity,
                "member_kind": item.member_kind,
                "sort_order": item.sort_order,
            }
            for one in generation.areas
            for item in one.memberships
        ])
        self._insert(_EVIDENCE_TABLE, _EVIDENCE_COLUMNS, [
            {
                "publication_key": publication_key,
                "area_key": one.area_key,
                "population_kind": item.population_kind,
                "reference": item.reference,
                "population_size": item.population_size,
                "matched_count": item.matched_count,
                "sort_order": item.sort_order,
            }
            for one in generation.areas
            for item in one.evidence
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
        ctx: Any,
        run_log: Any,
        binding: str,
        publication_key: str,
        parent_batch_step_id: Any,
    ) -> None:
        """Invoke one governed operation through the map, never an assembled statement."""
        procedure = ProcedureMap.resolve(
            self._connection, get_procedure_map(ctx, _MAP), _MAP, binding,
        )
        procedure.call(
            self._connection,
            {"publication_key": publication_key, "batch_step_id": parent_batch_step_id},
            run_log,
        )
