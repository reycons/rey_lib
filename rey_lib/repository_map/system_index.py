"""The system repository-map index.

Contract: rey_system_repository_map_correction.sgc.yaml (COR-004).

A system baseline is a set of repository baselines, not a merged map. This
index binds them: it names each repository's artifact, commit and content hash,
and records the relationships that only exist between repositories.

It copies no fact. A relationship carries the repositories it runs between and
the record_ids of the evidence that proved it, so a reader resolves a claim
back into the repository map that owns it. Cross-repository references are
always qualified by repository, because record_id is unique within a repository
and not across the estate.

Every map is read through rey_lib.files.jsonl. Nothing here parses JSONL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.jsonl import read_jsonl_file, render_jsonl_line, write_jsonl_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    EDGE_KIND_IMPORT,
    EDGE_KIND_RE_EXPORT,
    RECORD_TYPE_CROSS_REPOSITORY_EDGE,
    RECORD_TYPE_DEPENDENCY_EDGE,
    RECORD_TYPE_REPOSITORY_BASELINE,
    RECORD_TYPE_SYSTEM_MAP,
)
from rey_lib.repository_map.writer import GENERATOR_VERSION, RepositoryMap

__all__ = [
    "SystemIndex",
    "build_system_index",
    "load_repository_baselines",
    "validate_system_index",
    "write_system_index",
]

logger = get_logger(__name__)

# Recorded when a repository has no review artifact yet. Stated rather than
# omitted: a missing review is a fact about the baseline's completeness.
REVIEW_NOT_PRESENT = "not_present"

# Recorded when a baseline predates architecture_policy_status. It is neither
# evaluated nor not_configured, and flattening it into either would be a claim
# the artifact does not make.
POLICY_UNRECORDED = "unrecorded"


@dataclass
class SystemIndex:
    """The system baseline: a header, its repository baselines and relationships.

    Attributes:
        header: The system_repository_map header record.
        records: Baseline and cross-repository relationship records.
    """

    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)

    def baselines(self) -> list[dict[str, Any]]:
        """Return the repository baseline records."""
        return [r for r in self.records if r["record_type"] == RECORD_TYPE_REPOSITORY_BASELINE]


def load_repository_baselines(
    context_root: Path,
    repositories: list[str],
) -> dict[str, RepositoryMap]:
    """Read every repository map through the canonical JSONL reader.

    Args:
        context_root: Directory holding the canonical artifacts.
        repositories: The system membership set.

    Returns:
        Repository name to its parsed map.

    Raises:
        FileNotFoundError: If a member's canonical artifact is absent. A system
            baseline cannot be assembled from an incomplete set.
    """
    maps: dict[str, RepositoryMap] = {}
    for repository in repositories:
        path = context_root / f"03_repository_map.{repository}.generated.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"Canonical baseline missing for {repository}: {path}")
        rows = [dict(row.record) for row in read_jsonl_file(path)]
        maps[repository] = RepositoryMap(header=rows[0], records=rows[1:])
    return maps


def build_system_index(
    context_root: Path,
    maps: dict[str, RepositoryMap],
) -> SystemIndex:
    """Bind repository baselines into one system baseline.

    Args:
        context_root: Directory holding the canonical artifacts.
        maps: Repository name to its parsed map.

    Returns:
        The index, deterministically ordered.
    """
    records: list[dict[str, Any]] = []
    for repository in sorted(maps):
        records.append(_baseline_record(context_root, repository, maps[repository]))
    records.extend(_cross_repository_records(maps))

    header = {
        "record_type": RECORD_TYPE_SYSTEM_MAP,
        "record_id": RECORD_TYPE_SYSTEM_MAP,
        "schema_version": 1,
        "member_count": len(maps),
        "generator_version": GENERATOR_VERSION,
        "content_hash": _content_hash(records),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return SystemIndex(header=header, records=records)


def _baseline_record(
    context_root: Path,
    repository: str,
    report: RepositoryMap,
) -> dict[str, Any]:
    """Build one repository_baseline record.

    Args:
        context_root: Directory holding the canonical artifacts.
        repository: Repository name.
        report: That repository's parsed map.

    Returns:
        The record. It names the artifact and its provenance; it holds no
        facts from inside the map.
    """
    review_path = context_root / f"03_repository_map.{repository}.review.yaml"
    review_present = review_path.is_file()
    return {
        "record_type": RECORD_TYPE_REPOSITORY_BASELINE,
        "record_id": f"{RECORD_TYPE_REPOSITORY_BASELINE}:{repository}",
        "repository": repository,
        "artifact_path": f"03_repository_map.{repository}.generated.jsonl",
        "branch": report.header.get("branch", ""),
        "head_commit": report.header.get("head_commit", ""),
        "working_tree_status": report.header.get("working_tree_status", ""),
        "rules_hash": report.header.get("rules_hash", ""),
        "content_hash": report.header.get("content_hash", ""),
        "record_count": len(report.records) + 1,
        "architecture_policy_status": report.header.get(
            "architecture_policy_status", POLICY_UNRECORDED
        ),
        "review_artifact_path": (
            f"03_repository_map.{repository}.review.yaml" if review_present else REVIEW_NOT_PRESENT
        ),
        "review_artifact_hash": (
            sha256_text(read_text_file(review_path)) if review_present else REVIEW_NOT_PRESENT
        ),
    }


def _cross_repository_records(maps: dict[str, RepositoryMap]) -> list[dict[str, Any]]:
    """Derive relationships that run between member repositories.

    Only existing repository-map facts are used. A dependency edge whose target
    names a member repository is a cross-repository relationship; a target that
    names nothing in the membership set is a third-party package and stays
    external, because membership is the only thing that makes a name internal.

    Args:
        maps: Repository name to its parsed map.

    Returns:
        One record per ordered repository pair, evidence retained.
    """
    members = set(maps)
    pairs: dict[tuple[str, str], list[str]] = {}

    for repository, report in maps.items():
        for record in report.records:
            if record["record_type"] != RECORD_TYPE_DEPENDENCY_EDGE:
                continue
            if record["edge_kind"] not in {EDGE_KIND_IMPORT, EDGE_KIND_RE_EXPORT}:
                continue
            target = _target_repository(record["to"], members, repository)
            if target is None:
                continue
            pairs.setdefault((repository, target), []).append(record["record_id"])

    records = []
    for (source, target) in sorted(pairs):
        evidence = sorted(pairs[(source, target)])
        records.append(
            {
                "record_type": RECORD_TYPE_CROSS_REPOSITORY_EDGE,
                "record_id": f"{RECORD_TYPE_CROSS_REPOSITORY_EDGE}:{source}:{target}",
                "from_repository": source,
                "to_repository": target,
                "edge_count": len(evidence),
                "evidence_record_ids": evidence,
            }
        )
    return records


def _target_repository(target: str, members: set[str], source: str) -> str | None:
    """Return the member repository an import target names, if any.

    Args:
        target: The written import target.
        members: The system membership set.
        source: The importing repository, excluded so a repository's own
            imports are not reported as crossing to itself.

    Returns:
        The member repository name, or None when the target is external.
    """
    root = target.lstrip(".").split(".", 1)[0].split("/", 1)[0]
    if root in members and root != source:
        return root
    return None


def _content_hash(records: list[dict[str, Any]]) -> str:
    """Return a hash over the index records.

    Args:
        records: Every index record, excluding the header.

    Returns:
        A hex digest, taken through the same encoding the file is written with.
    """
    return sha256_text("\n".join(render_jsonl_line(record) for record in records))


def write_system_index(index: SystemIndex, output_path: Path) -> None:
    """Write the system index as deterministic JSONL.

    Args:
        index: The index to write.
        output_path: Destination file.
    """
    write_jsonl_file(output_path, [index.header, *index.records])
    logger.info(
        "Wrote system index with %d members to %s",
        index.header["member_count"],
        output_path,
    )


def validate_system_index(
    index: SystemIndex,
    maps: dict[str, RepositoryMap],
) -> list[str]:
    """Return why a system index no longer describes its repository baselines.

    A system baseline is only as current as its least current member: if a
    repository map is regenerated, the index that referenced its old hash is
    stale and must not be treated as describing the new one.

    Args:
        index: The index to validate.
        maps: The repository maps currently on disk.

    Returns:
        Problems found, empty when the index is current and complete.
    """
    problems: list[str] = []
    recorded = {r["repository"]: r for r in index.baselines()}

    for repository, report in maps.items():
        entry = recorded.get(repository)
        if entry is None:
            problems.append(f"{repository} has a baseline but is absent from the system index.")
            continue
        if entry["content_hash"] != report.header.get("content_hash", ""):
            problems.append(
                f"{repository} baseline has changed since the index was built "
                f"({entry['content_hash'][:12]} indexed, "
                f"{report.header.get('content_hash', '')[:12]} on disk). "
                f"Regenerate the system index."
            )

    for repository in sorted(set(recorded) - set(maps)):
        problems.append(f"The index references {repository}, which has no baseline on disk.")

    if index.header.get("content_hash", "") != _content_hash(index.records):
        problems.append("The system index does not match its own content_hash.")

    known = {name: {x["record_id"] for x in report.records} for name, report in maps.items()}
    for record in index.records:
        if record["record_type"] != RECORD_TYPE_CROSS_REPOSITORY_EDGE:
            continue
        source = record["from_repository"]
        missing = [rid for rid in record["evidence_record_ids"] if rid not in known.get(source, ())]
        if missing:
            problems.append(
                f"{record['record_id']} cites {len(missing)} evidence record_id(s) absent from "
                f"the {source} map, starting with {missing[0]}."
            )
    return problems
