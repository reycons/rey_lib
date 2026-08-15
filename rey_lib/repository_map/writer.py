"""Deterministic JSONL serialization and record_id diffing.

Contract: rey_repository_map_generator.sgc.yaml (INC-007, REQ-001 to REQ-003,
REQ-110 to REQ-115).

The generated factual map is a JSONL fact stream: one complete JSON object per
line, every record carrying record_type and record_id, no pretty printing and
no trailing non-JSON content.

Determinism is the point. The same commit, rules and generator version produce
byte-identical output, with one exception: generated_at is wall-clock and is
excluded from both the content hash and byte-equivalence comparison.

Maps are compared by record_id, never by line position, so reordering the
stream is not a change and a moved fact is not an added one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.jsonl import render_jsonl_line, write_jsonl_file
from rey_lib.git.errors import GitError
from rey_lib.git.repo import get_head_commit, get_repo_status, run_git
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.dispatchers import inventory_dispatchers_and_switches
from rey_lib.repository_map.entry_points import extract_runtime_entry_points
from rey_lib.repository_map.extractors import (
    LANGUAGE_EXTRACTORS,
    extract_executable_references,
    extract_symbols,
)
from rey_lib.repository_map.globals_scan import extract_global_publications_and_consumers
from rey_lib.repository_map.graph import build_dependency_graph, compute_reachability
from rey_lib.repository_map.inventory import inventory_files, load_scan_rules
from rey_lib.repository_map.records import RECORD_TYPE_REPOSITORY_MAP, ScanRules
from rey_lib.repository_map.registrations import extract_registrations

__all__ = [
    "GENERATOR_VERSION",
    "MapDiff",
    "RepositoryMap",
    "POLICY_EVALUATED",
    "RULES_NOT_CONFIGURED",
    "POLICY_NOT_CONFIGURED",
    "compare_repository_maps",
    "content_hash_of",
    "generate_repository_map",
    "write_repository_map",
]

logger = get_logger(__name__)

# Bumped when the generated fact model changes, so a stale map is detectable
# even when the commit and rules are unchanged.
GENERATOR_VERSION = "1.0.0"

# Wall-clock only. Excluded from hashing and byte-equivalence so two runs of
# the same commit agree.
_NON_DETERMINISTIC_HEADER_FIELDS = frozenset({"generated_at", "content_hash"})

# Recorded as the rules hash when a repository declares no rules. There is no
# file to hash, and an empty hash would read as one that failed to compute.
RULES_NOT_CONFIGURED = "not_configured"

# Whether architectural policy was evaluated for a map. A repository that
# declares no policy is not thereby architecturally clean, and the two states
# must never read alike.
POLICY_EVALUATED = "evaluated"
POLICY_NOT_CONFIGURED = "not_configured"


@dataclass
class RepositoryMap:
    """A generated factual map: one header record plus the facts.

    Attributes:
        header: The repository_map header record.
        records: Every other record, in emission order.
    """

    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)

    def by_record_id(self) -> dict[str, dict[str, Any]]:
        """Return the fact records keyed by record_id.

        Returns:
            record_id to record. A duplicate id is a generator defect and is
            reported rather than silently overwritten.
        """
        indexed: dict[str, dict[str, Any]] = {}
        for record in self.records:
            record_id = record["record_id"]
            if record_id in indexed:
                logger.warning("Duplicate record_id in generated map: %s", record_id)
            indexed[record_id] = record
        return indexed


@dataclass(frozen=True)
class MapDiff:
    """What changed between two generated maps, by record identity.

    Attributes:
        added: record_ids present only in the new map.
        removed: record_ids present only in the old map.
        changed: record_ids whose content differs.
        unchanged_count: How many facts were identical.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged_count: int

    @property
    def is_empty(self) -> bool:
        """Return True when nothing was added, removed or changed."""
        return not (self.added or self.removed or self.changed)


def generate_repository_map(
    repo_root: Path,
    output_path: Path,
    rules_path: Path | None = None,
) -> RepositoryMap:
    """Run the complete scan and write the deterministic JSONL fact stream.

    Shared code names no repository's policy file. Where a repository keeps its
    rules — or whether it has any — is the caller's to supply, so a repository
    with a different layout needs no change here and no repository-name
    conditional exists.

    Args:
        repo_root: Repository to scan.
        output_path: Where to write the generated map.
        rules_path: The repository's scan rules. None means the repository
            declares none, which is a supported state: it still produces a file
            inventory and reports architecture_policy_status not_configured.

    Returns:
        The generated map.

    Raises:
        FileNotFoundError: If a rules path is supplied and does not exist.
            Declaring a path that is not there is an error; declaring none is
            not.
    """
    rules = load_scan_rules(rules_path) if rules_path is not None else ScanRules.unconfigured()
    report = build_repository_map(repo_root, rules, rules_path)
    write_repository_map(report, output_path)
    return report


def build_repository_map(
    repo_root: Path,
    rules: ScanRules,
    rules_path: Path | None,
) -> RepositoryMap:
    """Collect every generated fact for a repository.

    Args:
        repo_root: Repository to scan.
        rules: The repository's scan rules.
        rules_path: Path the rules were loaded from, or None when the
            repository declares none.

    Returns:
        The map, with records in emission order.
    """
    files = inventory_files(repo_root, rules)

    symbols: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    references = []
    for file_record in files:
        if file_record.language not in LANGUAGE_EXTRACTORS:
            continue
        if not rules.extracts_facts_from(file_record):
            continue
        path = repo_root / file_record.path
        inventory = extract_symbols(path, file_record.language, file_record.path)
        file_references = extract_executable_references(
            path, file_record.language, file_record.path
        )
        references.extend(file_references)
        symbols.extend(inventory.to_records())
        edges.extend(edge.to_dict() for edge in file_references)

    registrations = extract_registrations(repo_root, files, rules)
    entry_points = extract_runtime_entry_points(repo_root, files, rules)
    global_report = extract_global_publications_and_consumers(repo_root, files, rules)
    graph = build_dependency_graph(
        files,
        references,
        registrations,
        entry_points,
        list(global_report.publications),
        list(global_report.consumers),
        rules,
    )
    reachability = compute_reachability(graph, files)
    dispatchers = inventory_dispatchers_and_switches(repo_root, files, rules, references)
    # Guards run as part of generation so the map carries the verdicts its own
    # evidence supports. The authority is the same one every consumer calls;
    # this only supplies it the facts it needs.
    violations = check_architecture_boundaries(
        rules,
        references=references,
        publications=list(global_report.publications),
        files=files,
        entry_points=entry_points,
        dispatchers=dispatchers,
    )

    # Emission order groups record types; each group is sorted by record_id so
    # the stream is stable regardless of discovery order.
    records: list[dict[str, Any]] = []
    for group in (
        [record.to_dict() for record in files],
        symbols,
        edges,
        [record.to_dict() for record in registrations],
        [record.to_dict() for record in entry_points],
        [record.to_dict() for record in global_report.publications],
        [record.to_dict() for record in global_report.consumers],
        [record.to_dict() for record in reachability],
        [record.to_dict() for record in dispatchers],
        [record.to_dict() for record in violations],
    ):
        records.extend(sorted(group, key=lambda record: record["record_id"]))

    header = _header(repo_root, rules_path, records, _policy_status(rules))
    return RepositoryMap(header=header, records=records)


def _policy_status(rules: ScanRules) -> str:
    """Return whether architectural policy was evaluated for this map.

    Zero violations means one of two very different things: policy ran and
    found nothing, or no policy exists. Reporting only a count would let the
    second read as the first.

    Args:
        rules: The repository's scan rules.

    Returns:
        POLICY_EVALUATED when any rule family is declared, otherwise
        POLICY_NOT_CONFIGURED.
    """
    return POLICY_EVALUATED if rules.declares_any_policy else POLICY_NOT_CONFIGURED


def _header(
    repo_root: Path,
    rules_path: Path | None,
    records: list[dict[str, Any]],
    policy_status: str,
) -> dict[str, Any]:
    """Build the repository_map header record.

    Args:
        repo_root: Repository that was scanned.
        rules_path: Path the rules were loaded from.
        records: Every fact record, for the content hash.
        policy_status: Whether architectural policy was evaluated.

    Returns:
        The header record. Header fields are excluded from content_hash by
        construction, so adding one changes no fact and no existing baseline
        body.
    """
    branch, head_commit, working_tree_status = _git_state(repo_root)
    return {
        "record_type": RECORD_TYPE_REPOSITORY_MAP,
        "record_id": RECORD_TYPE_REPOSITORY_MAP,
        "schema_version": 1,
        "repository": repo_root.name,
        "repository_root": str(repo_root),
        "branch": branch,
        "head_commit": head_commit,
        "working_tree_status": working_tree_status,
        "generator_version": GENERATOR_VERSION,
        "rules_hash": (
            sha256_text(read_text_file(rules_path))
            if rules_path is not None
            else RULES_NOT_CONFIGURED
        ),
        "architecture_policy_status": policy_status,
        "content_hash": content_hash_of(records),
        # rey_lib's only utc_now lives in messaging, which pulls markdown_it and
        # would drag a rendering dependency into a source scanner. Every neutral
        # rey_lib module timestamps inline the same way, so this follows suit.
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _git_state(repo_root: Path) -> tuple[str, str, str]:
    """Return the repository's branch, HEAD commit and cleanliness.

    Every git call goes through rey_lib.git, which centralizes execution and
    error handling; this reads state and never mutates the repository.

    Args:
        repo_root: Repository to inspect.

    Returns:
        Branch, head commit and 'clean' or 'dirty'. A repository git cannot
        answer for reports 'unknown' rather than a guessed value, so a map
        generated outside a working tree never claims a commit it cannot
        prove.
    """
    try:
        branch = run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        commit = get_head_commit(repo_root)
        status = get_repo_status(repo_root)
    except GitError as exc:
        logger.warning("git state unavailable for %s: %s", repo_root, exc)
        return "unknown", "unknown", "unknown"
    return branch or "unknown", commit or "unknown", "clean" if status.clean else "dirty"


def content_hash_of(records: list[dict[str, Any]]) -> str:
    """Return a hash of every fact record.

    Args:
        records: The fact records, excluding the header.

    Returns:
        A hex digest that changes when any fact changes.

    Note:
        Hashed through ``render_jsonl_line``, the same encoding the file is
        written with, so the hash describes the bytes on disk rather than a
        second serialization that could drift from them.
    """
    return sha256_text("\n".join(render_jsonl_line(record) for record in records))


def write_repository_map(report: RepositoryMap, output_path: Path) -> None:
    """Write a generated map as deterministic JSONL.

    Serialization and the atomic whole-file write are owned by
    ``rey_lib.files.jsonl``; this only decides record order. A reader therefore
    never observes a partially written map.

    Args:
        report: The map to write.
        output_path: Destination file.
    """
    write_jsonl_file(output_path, [report.header, *report.records])
    logger.info("Wrote %d records to %s", len(report.records) + 1, output_path)


def compare_repository_maps(old: RepositoryMap, new: RepositoryMap) -> MapDiff:
    """Compare two generated maps by record identity.

    Record order is not content: reordering the stream produces an empty diff,
    and a fact that moved is not reported as added and removed.

    Args:
        old: The earlier map.
        new: The later map.

    Returns:
        The differences, each list sorted.
    """
    old_records = old.by_record_id()
    new_records = new.by_record_id()
    old_ids = set(old_records)
    new_ids = set(new_records)

    changed = sorted(
        record_id
        for record_id in old_ids & new_ids
        if old_records[record_id] != new_records[record_id]
    )
    return MapDiff(
        added=tuple(sorted(new_ids - old_ids)),
        removed=tuple(sorted(old_ids - new_ids)),
        changed=tuple(changed),
        unchanged_count=len(old_ids & new_ids) - len(changed),
    )
