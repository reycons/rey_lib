"""
Whether a migration adds up.

repository_map owns structural facts and is one input here, never the owner of
this question: what exists is a fact about a repository, and whether a source
object's capabilities all landed somewhere is a fact about a migration. Keeping
them apart is the same authority split the context set already draws between
structure and semantics.

What this module answers, and only this: every authored capability accounted
for, every claimed preservation pointing at evidence that resolves, every
retirement naming a decision, and no object complete on top of a predecessor
that is not. The verdict is generated. Nothing here reads an authored status.
"""

from __future__ import annotations

from rey_lib.migration_reconciliation.evidence import EvidenceIndex, EvidenceResult
from rey_lib.migration_reconciliation.loader import (
    MigrationRecordError,
    load_all,
    load_object_record,
)
from rey_lib.migration_reconciliation.records import (
    DISPOSITIONS,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    STATUS_UNPROVEN,
    CapabilityRow,
    ObjectRecord,
    ObjectVerdict,
    ReconciliationReport,
)
from rey_lib.migration_reconciliation.status import compute_verdicts
from rey_lib.migration_reconciliation.writer import (
    generate_migration_status,
    verify_migration_status_unedited,
)

__all__ = [
    "DISPOSITIONS",
    "STATUS_BLOCKED",
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_UNPROVEN",
    "CapabilityRow",
    "EvidenceIndex",
    "EvidenceResult",
    "MigrationRecordError",
    "ObjectRecord",
    "ObjectVerdict",
    "ReconciliationReport",
    "compute_verdicts",
    "generate_migration_status",
    "load_all",
    "load_object_record",
    "verify_migration_status_unedited",
]
