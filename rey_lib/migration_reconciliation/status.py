"""
Compute what an object's migration adds up to.

The rule is the approved one, applied the same way to every object. There is no
per-object branch here and there must never be one: a generator that needed a
special case to make a particular object come out COMPLETE would be reporting
the implementer's intent rather than the record's contents.

Severity order, when an object has several reasons not to be complete:
BLOCKED, then PARTIAL, then UNPROVEN. A blocked object is reported as blocked
even when its own rows are clean, because its verdict rests on a predecessor
whose verdict is not yet earned.
"""

from __future__ import annotations

from rey_lib.migration_reconciliation.evidence import EvidenceIndex
from rey_lib.migration_reconciliation.records import (
    DISPOSITION_MISSING,
    DISPOSITION_SEMANTICS_CHANGED,
    DISPOSITION_UNPROVEN,
    EVIDENCE_REQUIRED,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    STATUS_UNPROVEN,
    ObjectRecord,
    ObjectVerdict,
)


def compute_verdicts(
    records: list[ObjectRecord],
    index: EvidenceIndex,
) -> dict[str, ObjectVerdict]:
    """Compute every object's status, resolving predecessors before dependents.

    Args:
        records: The authored records.
        index: What evidence references resolve against.

    Returns:
        Object name to verdict.
    """
    own = {record.object_name: _own_verdict(record, index) for record in records}
    by_name = {record.object_name: record for record in records}

    verdicts: dict[str, ObjectVerdict] = {}
    for name in sorted(own):
        verdicts[name] = _with_predecessors(name, by_name, own, verdicts, set())
    return verdicts


def _own_verdict(record: ObjectRecord, index: EvidenceIndex) -> ObjectVerdict:
    """The verdict from this object's own rows, before predecessors are read."""
    reasons: list[str] = []
    partial = False
    unproven = False

    for capability in record.capabilities:
        name = capability.source_capability
        if capability.disposition == DISPOSITION_MISSING:
            partial = True
            reasons.append(f"MISSING: {name}")
        elif capability.disposition == DISPOSITION_SEMANTICS_CHANGED:
            partial = True
            reasons.append(f"SEMANTICS_CHANGED: {name}")
        elif capability.disposition == DISPOSITION_UNPROVEN:
            unproven = True
            reasons.append(f"UNPROVEN: {name}")
        elif capability.disposition in EVIDENCE_REQUIRED:
            # The loader proved a reference was written. This proves it points
            # at something real -- and no further, by design.
            result = index.resolve(capability.evidence or "")
            if not result.resolved:
                unproven = True
                reasons.append(f"evidence does not resolve for {name}: {result.reason}")

    if partial:
        return ObjectVerdict(record.object_name, STATUS_PARTIAL, tuple(reasons))
    if unproven:
        return ObjectVerdict(record.object_name, STATUS_UNPROVEN, tuple(reasons))
    return ObjectVerdict(record.object_name, STATUS_COMPLETE)


def _with_predecessors(
    name: str,
    by_name: dict[str, ObjectRecord],
    own: dict[str, ObjectVerdict],
    resolved: dict[str, ObjectVerdict],
    visiting: set[str],
) -> ObjectVerdict:
    """Fold predecessor verdicts into this object's.

    A predecessor that is not COMPLETE blocks its dependent, whatever the
    dependent's own rows say. A predecessor with no authored record blocks too:
    an object cannot be complete on top of one nobody has reconciled.
    """
    if name in resolved:
        return resolved[name]
    verdict = own[name]
    record = by_name[name]

    if name in visiting:
        # A cycle is a contradictory record, not a deep graph.
        return ObjectVerdict(name, STATUS_BLOCKED, (f"predecessor cycle through {name}",))

    reasons = list(verdict.reasons)
    blocked = False
    for predecessor in record.predecessors:
        if predecessor not in by_name:
            blocked = True
            reasons.append(f"predecessor {predecessor} has no reconciliation record")
            continue
        upstream = _with_predecessors(
            predecessor, by_name, own, resolved, visiting | {name},
        )
        if upstream.status != STATUS_COMPLETE:
            blocked = True
            reasons.append(f"predecessor {predecessor} is {upstream.status}")

    if blocked:
        return ObjectVerdict(name, STATUS_BLOCKED, tuple(reasons))
    return ObjectVerdict(name, verdict.status, tuple(reasons))
