"""
Emit the generated migration status, and prove it was not edited afterwards.

The same discipline the repository map already established: a machine-owned
JSONL stream carrying a content hash over its fact records, written whole, and
verifiable against its own contents. A verdict a human retyped is exactly the
failure this artifact exists to remove, so a hand edit is detectable rather than
merely discouraged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.files.jsonl import read_jsonl_file, write_jsonl_file
from rey_lib.repository_map.writer import content_hash_of

from rey_lib.migration_reconciliation.evidence import EvidenceIndex
from rey_lib.migration_reconciliation.loader import load_all
from rey_lib.migration_reconciliation.records import (
    GENERATOR_VERSION,
    RECORD_TYPE_CAPABILITY,
    RECORD_TYPE_HEADER,
    RECORD_TYPE_OBJECT,
    ObjectRecord,
    ObjectVerdict,
    ReconciliationReport,
)
from rey_lib.migration_reconciliation.status import compute_verdicts


def generate_migration_status(
    records_dir: Path,
    evidence_roots: tuple[Path, ...],
    output_path: Path | None = None,
) -> ReconciliationReport:
    """Read every authored record, compute each verdict, and emit the stream.

    Args:
        records_dir: Where the authored ``*.capabilities.yaml`` files live.
        evidence_roots: Trees an evidence reference may resolve against.
        output_path: Where to write. None computes without writing.

    Returns:
        The generated report.

    Raises:
        MigrationRecordError: If any authored record cannot be believed.
    """
    authored = load_all(records_dir)
    index = EvidenceIndex(evidence_roots)
    verdicts = compute_verdicts(authored, index)

    records: list[dict[str, Any]] = []
    for record in authored:
        records.extend(_object_records(record, verdicts[record.object_name], index))

    header = {
        "record_type": RECORD_TYPE_HEADER,
        "record_id": RECORD_TYPE_HEADER,
        "schema_version": 1,
        "object_count": len(authored),
        "generator_version": GENERATOR_VERSION,
        "content_hash": content_hash_of(records),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    report = ReconciliationReport(header=header, records=records)
    if output_path is not None:
        write_jsonl_file(output_path, [header, *records])
    return report


def _object_records(
    record: ObjectRecord,
    verdict: ObjectVerdict,
    index: EvidenceIndex,
) -> list[dict[str, Any]]:
    """One object record, then one record per capability."""
    rows: list[dict[str, Any]] = [{
        "record_type": RECORD_TYPE_OBJECT,
        "record_id": f"migration_object:{record.object_name}",
        "object": record.object_name,
        "increment": record.increment,
        "source_objects": list(record.source_objects),
        "predecessors": list(record.predecessors),
        "capability_count": len(record.capabilities),
        # Generated. An authored computed_status is never read.
        "computed_status": verdict.status,
        "reasons": list(verdict.reasons),
    }]
    for position, capability in enumerate(record.capabilities):
        resolved = (
            index.resolve(capability.evidence).located_in
            if capability.evidence else ""
        )
        rows.append({
            "record_type": RECORD_TYPE_CAPABILITY,
            "record_id": f"migration_capability:{record.object_name}:{position}",
            "object": record.object_name,
            "source_capability": capability.source_capability,
            "disposition": capability.disposition,
            "target_owner": capability.target_owner,
            "evidence": capability.evidence,
            "evidence_resolves_to": resolved,
            "decision": capability.decision,
        })
    return rows


def verify_migration_status_unedited(path: Path) -> list[str]:
    """Return why a generated status file is not the one that was generated.

    Args:
        path: The generated artifact.

    Returns:
        Problems, empty when the file's records hash to its own header.
    """
    # read_jsonl_file yields the parsed row beside the physical line it came
    # from; only the row is hashed, because the line number is not a fact.
    rows = [dict(row.record) for row in read_jsonl_file(path)]
    if not rows:
        return [f"{path.name}: is empty."]
    header, records = rows[0], rows[1:]
    if header.get("record_type") != RECORD_TYPE_HEADER:
        return [f"{path.name}: does not begin with a header."]

    recomputed = content_hash_of(records)
    if recomputed != header.get("content_hash"):
        return [
            f"{path.name}: content hash does not match its records. "
            "The file was edited after it was generated.",
        ]
    return []
