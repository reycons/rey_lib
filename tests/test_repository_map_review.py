"""Focused tests for the review artifact and generated-map immutability.

Contract: rey_repository_map_generator.sgc.yaml (INC-008).

No reviewer edits evidence to make the architecture pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.review import (
    load_review,
    validate_review,
    verify_generated_map_unedited,
)
from rey_lib.repository_map.writer import RepositoryMap, content_hash_of

DISPATCHER_ID = "dispatcher:frontend/src/tree_client/TreeClient.ts:action:810:2"
VIOLATION_ID = "violation:presentation_routing_via_coordinator:static/js/object_window.js:62:6"


def _map() -> RepositoryMap:
    """Return a small generated map with a valid content hash."""
    records = [
        {"record_type": "dispatcher", "record_id": DISPATCHER_ID, "classification": "unreviewed"},
        {"record_type": "architecture_violation", "record_id": VIOLATION_ID, "source_line": 62},
    ]
    return RepositoryMap(
        header={
            "record_type": "repository_map",
            "record_id": "repository_map",
            "content_hash": content_hash_of(records),
        },
        records=records,
    )


def _write(path: Path, body: str) -> Path:
    """Write a review artifact and return its path."""
    review_path = path / "03_repository_map.review.yaml"
    review_path.write_text(body, encoding="utf-8")
    return review_path


def test_a_review_references_findings_by_record_id(tmp_path: Path) -> None:
    """Decisions point at generated facts rather than restating them."""
    path = _write(
        tmp_path,
        f'review:\n  reviewer: someone\ndecisions:\n  - record_id: "{DISPATCHER_ID}"\n'
        "    decision: legitimate\n    rationale: The tree's own dispatcher.\n",
    )

    review = load_review(path)

    assert [decision.record_id for decision in review.decisions] == [DISPATCHER_ID]
    assert review.decisions[0].decision == "legitimate"
    assert review.reviewer == "someone"


def test_a_decision_restating_a_generated_field_is_refused(tmp_path: Path) -> None:
    """A copied fact is a second version of the truth and is rejected."""
    path = _write(
        tmp_path,
        f'decisions:\n  - record_id: "{DISPATCHER_ID}"\n    decision: legitimate\n'
        "    classification: legitimate\n    branch_count: 11\n",
    )

    with pytest.raises(ValueError, match="restates generated fields"):
        load_review(path)


def test_an_unknown_decision_field_is_refused(tmp_path: Path) -> None:
    """The review vocabulary is closed, so a typo is not silently kept."""
    path = _write(
        tmp_path,
        f'decisions:\n  - record_id: "{DISPATCHER_ID}"\n    decision: legitimate\n'
        "    reasoning: typo for rationale\n",
    )

    with pytest.raises(ValueError, match="Unknown review decision fields"):
        load_review(path)


def test_a_decision_needs_a_target_and_a_conclusion(tmp_path: Path) -> None:
    """A decision without a record_id decides nothing."""
    path = _write(tmp_path, "decisions:\n  - decision: legitimate\n")

    with pytest.raises(ValueError, match="needs record_id and decision"):
        load_review(path)


def test_an_empty_review_is_valid(tmp_path: Path) -> None:
    """Nothing reviewed yet is a legitimate state, not an error."""
    path = _write(tmp_path, 'review:\n  reviewer: ""\ndecisions: []\n')

    review = load_review(path)

    assert review.decisions == ()
    assert validate_review(review, _map()) == []


def test_a_missing_review_file_is_refused(tmp_path: Path) -> None:
    """A declared review path that does not exist is an error."""
    with pytest.raises(FileNotFoundError):
        load_review(tmp_path / "absent.yaml")


def test_a_dangling_decision_is_reported(tmp_path: Path) -> None:
    """A decision about a finding that no longer exists is surfaced."""
    path = _write(
        tmp_path,
        'decisions:\n  - record_id: "dispatcher:gone.ts:handler:1:0"\n    decision: legitimate\n',
    )

    problems = validate_review(load_review(path), _map())

    assert len(problems) == 1
    assert "not in the generated map" in problems[0]


def test_a_review_of_a_superseded_map_is_reported(tmp_path: Path) -> None:
    """Decisions recorded against a different map may no longer apply."""
    path = _write(
        tmp_path,
        f'review:\n  reviewed_content_hash: "0123456789abcdef"\ndecisions:\n'
        f'  - record_id: "{DISPATCHER_ID}"\n    decision: legitimate\n',
    )

    problems = validate_review(load_review(path), _map())

    assert any("different generated map" in problem for problem in problems)


def test_deciding_one_finding_twice_is_reported(tmp_path: Path) -> None:
    """Two decisions about one finding leave which one applies undefined."""
    path = _write(
        tmp_path,
        f'decisions:\n  - record_id: "{DISPATCHER_ID}"\n    decision: legitimate\n'
        f'  - record_id: "{DISPATCHER_ID}"\n    decision: questionable\n',
    )

    problems = validate_review(load_review(path), _map())

    assert any("more than once" in problem for problem in problems)


def test_an_intact_generated_map_verifies() -> None:
    """A map that matches its own fingerprint is intact."""
    assert verify_generated_map_unedited(_map()) == []


def test_a_hand_edited_fact_is_detected() -> None:
    """Editing evidence to make a boundary pass breaks the fingerprint.

    This is the mechanism behind generated-map immutability: the map is not
    merely declared machine-owned, an edit to it is detectable.
    """
    report = _map()
    report.records[1]["source_line"] = 999

    problems = verify_generated_map_unedited(report)

    assert len(problems) == 1
    assert "edited in place" in problems[0]


def test_a_removed_fact_is_detected() -> None:
    """Deleting an inconvenient violation is an edit like any other."""
    report = _map()
    del report.records[1]

    assert verify_generated_map_unedited(report) != []


def test_a_map_without_a_content_hash_is_reported() -> None:
    """A map that cannot prove its own integrity is not trusted."""
    report = _map()
    del report.header["content_hash"]

    problems = verify_generated_map_unedited(report)

    assert problems == ["Generated map header carries no content_hash."]
