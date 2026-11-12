"""Reconciliation: what a verified lesson may change in what Rey already knows (#1344).

Pure: verified lessons and the governed read's rows in, decisions out. The rules
(user, 2026-11-12): same verified claims consolidate; contradiction invalidates; the
same key with different claims is refused; nothing unverified alters what is recorded;
claims, not keys or statements, are compared.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.experience.reconciliation import (
    CONSOLIDATE,
    INTRODUCE,
    INVALIDATE,
    LEAVE,
    REFUSE,
    VALIDATE,
    reconcile,
)
from rey_lib.ai.experience.verification import (
    CANDIDATE,
    REJECTED,
    VALIDATED,
    VERIFIED,
    ClaimCheck,
    VerifiedLesson,
)

ABSENT = {"kind": "db_member_absent", "source": "rey_apps",
          "identity": "code.development_backlog_vw.priority_rank"}
PRESENT = {"kind": "db_member_present", "source": "rey_apps",
           "identity": "code.development_backlog_vw.priority"}
PRIORITY_RANK_PRESENT = {**ABSENT, "kind": "db_member_present"}


def _lesson(key: str = "no_priority_rank_column", state: str = VALIDATED,
            claims: tuple[dict[str, Any], ...] = (ABSENT, PRESENT)) -> VerifiedLesson:
    return VerifiedLesson(experience_key=key, statement="said", knowledge_kind="observed_fact",
                          state=state, evidence=(),
                          claims=tuple(ClaimCheck(dict(one), VERIFIED, "") for one in claims))


def _row(experience_id: int, key: str = "no_priority_rank_column", *,
         state: str = VALIDATED, authoritative: bool = True, staleness: str = "current",
         claims: tuple[dict[str, Any], ...] = (ABSENT, PRESENT)) -> dict[str, Any]:
    return {"ai_experience_id": experience_id, "experience_key": key, "validation_state": state,
            "is_authoritative": authoritative, "staleness": staleness,
            "verified_claims": [dict(one) for one in claims]}


def test_a_new_validated_key_is_introduced() -> None:
    (one,) = reconcile([_lesson()], [])
    assert one.action == INTRODUCE


def test_the_same_verified_claims_consolidate_and_support_the_authoritative_one() -> None:
    (one,) = reconcile([_lesson(claims=(PRESENT, ABSENT))], [_row(7)])
    assert (one.action, one.supports) == (CONSOLIDATE, 7)


def test_the_same_key_with_different_claims_is_refused() -> None:
    (one,) = reconcile([_lesson(claims=(ABSENT,))], [_row(7)])
    assert one.action == REFUSE


def test_a_contradicting_verified_claim_invalidates_authoritative_experience() -> None:
    """Whatever the lesson's own key: the claims are compared, not the keys."""
    (one,) = reconcile([_lesson(key="priority_rank_exists", claims=(PRIORITY_RANK_PRESENT,))],
                       [_row(7)])
    assert (one.action, one.invalidates) == (INVALIDATE, (7,))


def test_a_validated_lesson_for_a_key_with_only_candidates_validates_it() -> None:
    (one,) = reconcile([_lesson()], [_row(3, state=CANDIDATE, authoritative=False, claims=())])
    assert one.action == VALIDATE


def test_unverified_lessons_never_alter_recorded_experience() -> None:
    for state in (CANDIDATE, REJECTED):
        (one,) = reconcile([_lesson(state=state, claims=(PRIORITY_RANK_PRESENT,))], [_row(7)])
        assert one.action == LEAVE and one.invalidates == ()


def test_a_rejected_proposal_of_a_new_key_changes_nothing() -> None:
    (one,) = reconcile([_lesson(key="fresh", state=REJECTED)], [])
    assert one.action == LEAVE


def test_a_candidate_of_a_new_key_is_introduced_as_a_candidate() -> None:
    (one,) = reconcile([_lesson(key="fresh", state=CANDIDATE)], [])
    assert one.action == INTRODUCE


def test_stale_experience_is_not_authoritative_here() -> None:
    """A lesson whose absent column has since appeared is not something to consolidate
    into or invalidate: the key is recorded, so a validated lesson validates it again."""
    (one,) = reconcile([_lesson()], [_row(7, staleness="stale")])
    assert one.action == VALIDATE
