"""
What a migration record is made of.

One authored file per migrated object, and one generated stream over all of
them. The split is the whole point: an implementer says which source
capabilities exist and where each one landed, and a generator says whether that
adds up to a complete migration. Nobody authors the verdict.

The dispositions are the audit's own vocabulary, kept exactly, because a record
that renamed them would stop answering the question the audit asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A capability that is carried into the target, in the target's own shape or
#: unchanged. Both require evidence, because both claim the behaviour survived.
DISPOSITION_PRESERVED = "PRESERVED"
DISPOSITION_PRESERVED_DIFFERENT_SHAPE = "PRESERVED_DIFFERENT_TARGET_SHAPE"
#: Deliberately not carried. Requires a named architecture decision, so that a
#: capability cannot be retired because it was inconvenient to migrate.
DISPOSITION_RETIRED = "INTENTIONALLY_RETIRED"
#: Not carried, and not decided. Each blocks completion in its own way.
DISPOSITION_MISSING = "MISSING"
DISPOSITION_SEMANTICS_CHANGED = "SEMANTICS_CHANGED"
DISPOSITION_UNPROVEN = "UNPROVEN"

DISPOSITIONS: frozenset[str] = frozenset({
    DISPOSITION_PRESERVED,
    DISPOSITION_PRESERVED_DIFFERENT_SHAPE,
    DISPOSITION_RETIRED,
    DISPOSITION_MISSING,
    DISPOSITION_SEMANTICS_CHANGED,
    DISPOSITION_UNPROVEN,
})

#: Dispositions whose claim is "this behaviour survived", and which therefore
#: cannot be believed without a resolvable evidence reference.
EVIDENCE_REQUIRED: frozenset[str] = frozenset({
    DISPOSITION_PRESERVED,
    DISPOSITION_PRESERVED_DIFFERENT_SHAPE,
})

#: The computed verdicts. Ordered by severity so a reader knows which wins when
#: an object has several reasons not to be complete.
STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNPROVEN = "UNPROVEN"
STATUS_BLOCKED = "BLOCKED"

RECORD_TYPE_HEADER = "migration_status"
RECORD_TYPE_OBJECT = "migration_object"
RECORD_TYPE_CAPABILITY = "migration_capability"

GENERATOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class CapabilityRow:
    """One source capability, and what became of it.

    Attributes:
        source_capability: The capability as the source object had it.
        disposition: One of ``DISPOSITIONS``.
        target_owner: The console_next object answering for it, or None.
        evidence: A reference that must resolve, for the preserved dispositions.
        decision: The named architecture decision, for a retirement.
        shape_change: Why the target shape differs, where it does.
    """

    source_capability: str
    disposition: str
    target_owner: str | None = None
    evidence: str | None = None
    decision: str | None = None
    shape_change: str | None = None


@dataclass(frozen=True)
class ObjectRecord:
    """One authored capability file, parsed.

    Attributes:
        object_name: The target object this record is about.
        path: Where the authored file was read from.
        increment: The contract increment that produced it.
        capabilities: Every source capability, in authored order.
        predecessors: Canonical objects this one depends on.
        source_objects: Declared provenance. Carried for later gates; this
            module does not enforce provenance cardinality.
    """

    object_name: str
    path: Path
    increment: str | None
    capabilities: tuple[CapabilityRow, ...]
    predecessors: tuple[str, ...] = ()
    source_objects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObjectVerdict:
    """What the generator computed for one object.

    Attributes:
        object_name: The object.
        status: One of the ``STATUS_*`` constants.
        reasons: Why it is not COMPLETE, empty when it is.
    """

    object_name: str
    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationReport:
    """The generated stream: a header, then one record per object and capability."""

    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)

    def verdict_of(self, object_name: str) -> str | None:
        """Return the computed status for one object, or None when absent."""
        for record in self.records:
            if (record.get("record_type") == RECORD_TYPE_OBJECT
                    and record.get("object") == object_name):
                return str(record.get("computed_status"))
        return None
