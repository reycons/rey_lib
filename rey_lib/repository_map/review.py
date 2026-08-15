"""The review artifact and the immutability of generated facts.

Contract: rey_repository_map_generator.sgc.yaml (INC-008, REQ-120 to REQ-122).

Ownership runs one way: source, then generated facts and verdicts, then review
decisions. A reviewer records what should happen about a finding; a reviewer
never edits the evidence that produced it.

Two things enforce that split rather than merely asking for it.

A review references generated facts by record_id and may not restate them. A
decision carrying a generated field is rejected, because a copied fact is a
second version of the truth that will eventually disagree with the first.

A generated map is machine-owned and whole. Its header carries a content hash
over every fact record, so a hand-edited fact is detectable: the map no longer
hashes to what it claims. Regeneration replaces the file entirely; nothing
edits a fact in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import read_text_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.writer import RepositoryMap, content_hash_of

__all__ = [
    "GENERATED_ONLY_FIELDS",
    "ReviewDecision",
    "ReviewDocument",
    "load_review",
    "verify_generated_map_unedited",
    "validate_review",
]

logger = get_logger(__name__)

# Fields that belong to generated records. A review that carries any of them is
# restating a fact instead of pointing at one.
GENERATED_ONLY_FIELDS = frozenset(
    {
        "record_type",
        "source_path",
        "source_line",
        "source_column",
        "callee",
        "caller",
        "edge_kind",
        "branch_count",
        "branch_values",
        "vocabulary",
        "symbol",
        "registry",
        "registered_id",
        "target",
        "status",
        "classification",
    }
)

# Keys a decision entry may carry.
_DECISION_FIELDS = frozenset({"record_id", "decision", "rationale", "owner"})


@dataclass(frozen=True)
class ReviewDecision:
    """One human decision about one generated finding.

    Attributes:
        record_id: The generated fact this decision is about.
        decision: What review concluded, as free vocabulary owned by review.
        rationale: Why. Recorded so a later reader need not re-derive it.
        owner: Who is accountable for acting on it, empty when unassigned.
    """

    record_id: str
    decision: str
    rationale: str = ""
    owner: str = ""


@dataclass(frozen=True)
class ReviewDocument:
    """Human decisions about one generated map.

    Attributes:
        reviewed_content_hash: The content hash of the map that was reviewed,
            so a review of a superseded map is detectable rather than assumed
            to still apply.
        reviewed_at: When the review was recorded.
        reviewer: Who recorded it.
        decisions: The decisions, in declaration order.
    """

    reviewed_content_hash: str = ""
    reviewed_at: str = ""
    reviewer: str = ""
    decisions: tuple[ReviewDecision, ...] = field(default_factory=tuple)


def load_review(path: Path) -> ReviewDocument:
    """Load a review document from its YAML artifact.

    Args:
        path: Path to a repository's review artifact, named
            03_repository_map.<repository>.review.yaml.

    Returns:
        The parsed review.

    Raises:
        FileNotFoundError: If the review file does not exist.
        ValueError: If the file is not valid review content, or a decision
            restates a generated fact.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Review file not found: {path}")

    try:
        parsed = parse_yaml(read_text_file(path))
    except Exception as exc:  # Name the offending file, not a bare parse error.
        raise ValueError(f"Review file is not valid YAML: {path}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Review file must be a mapping: {path}")

    metadata = parsed.get("review") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"Review section must be a mapping: {path}")

    return ReviewDocument(
        reviewed_content_hash=metadata.get("reviewed_content_hash", ""),
        reviewed_at=metadata.get("reviewed_at", ""),
        reviewer=metadata.get("reviewer", ""),
        decisions=tuple(_decisions(parsed.get("decisions"), path)),
    )


def _decisions(entries: Any, path: Path) -> list[ReviewDecision]:
    """Build decisions from the parsed entries.

    Args:
        entries: The decisions section.
        path: Review path, for error messages.

    Returns:
        The decisions.

    Raises:
        ValueError: If an entry is malformed or restates a generated fact.
    """
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"Review 'decisions' must be a list: {path}")

    decisions = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Each review decision must be a mapping: {path}")
        restated = sorted(set(entry) & GENERATED_ONLY_FIELDS)
        if restated:
            raise ValueError(
                f"Review decision for {entry.get('record_id', '<unknown>')} restates generated "
                f"fields {restated} in {path}. A review references facts by record_id and "
                f"never copies them."
            )
        unknown = sorted(set(entry) - _DECISION_FIELDS)
        if unknown:
            raise ValueError(f"Unknown review decision fields {unknown} in {path}.")
        if "record_id" not in entry or "decision" not in entry:
            raise ValueError(f"A review decision needs record_id and decision: {path}")
        decisions.append(
            ReviewDecision(
                record_id=entry["record_id"],
                decision=entry["decision"],
                rationale=entry.get("rationale", ""),
                owner=entry.get("owner", ""),
            )
        )
    return decisions


def verify_generated_map_unedited(report: RepositoryMap) -> list[str]:
    """Return why a generated map is not what it claims to be.

    The map is machine-owned and whole. Its header hashes every fact record, so
    editing a fact by hand — to make a boundary pass, for instance — leaves the
    map disagreeing with its own fingerprint.

    Args:
        report: The map to verify.

    Returns:
        Problems found, empty when the map is intact.
    """
    problems: list[str] = []
    declared = report.header.get("content_hash", "")
    actual = content_hash_of(report.records)
    if not declared:
        problems.append("Generated map header carries no content_hash.")
    elif declared != actual:
        problems.append(
            "Generated map does not match its own content_hash: a fact was edited in place. "
            "Regenerate the map; review decisions belong in the review artifact."
        )
    return problems


def validate_review(review: ReviewDocument, report: RepositoryMap) -> list[str]:
    """Return why a review does not apply cleanly to a generated map.

    Args:
        review: The review decisions.
        report: The generated map they are about.

    Returns:
        Problems found, empty when the review applies cleanly.
    """
    problems: list[str] = []
    known = set(report.by_record_id())

    for decision in review.decisions:
        if decision.record_id not in known:
            problems.append(
                f"Review decision references {decision.record_id}, which is not in the "
                f"generated map. The finding may have been resolved or the map regenerated."
            )

    declared = report.header.get("content_hash", "")
    if review.reviewed_content_hash and declared and review.reviewed_content_hash != declared:
        problems.append(
            "Review was recorded against a different generated map "
            f"({review.reviewed_content_hash[:12]} vs {declared[:12]}). Its decisions may no "
            f"longer apply."
        )

    seen: set[str] = set()
    for decision in review.decisions:
        if decision.record_id in seen:
            problems.append(f"Review decides {decision.record_id} more than once.")
        seen.add(decision.record_id)

    return problems
