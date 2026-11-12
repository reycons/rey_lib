"""Reconciled lessons become the recording acts the publication gates admit (#1344).

The shape is the one the real rehearsal proved: verified claims as validation-role
db_member / db_member_absent code evidence, resolved index evidence as
recommendation-role code evidence, a resolved execution as outcome, a message as
recommendation, a consolidation citing what it supports; introduce is an extraction,
consolidate / validate / invalidate are validation acts; nothing unresolved is staged.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.experience.reconciliation import (
    CONSOLIDATE,
    INTRODUCE,
    INVALIDATE,
    LEAVE,
    REFUSE,
    Reconciled,
)
from rey_lib.ai.experience.staging import ExperienceProvenance, generations_for
from rey_lib.ai.experience.verification import (
    CANDIDATE,
    VALIDATED,
    VERIFIED,
    ClaimCheck,
    EvidenceCheck,
    VerificationSubject,
    VerifiedLesson,
)

SUBJECT = VerificationSubject(1, 6, {"kind": "views", "name": "development_backlog_vw", "schema": "code",
                                     "catalog": "", "connection": "rey_apps"})
PROVENANCE = ExperienceProvenance(task_run_id=6369, ai_task_id=192, ai_contract_id=219,
                                  generator="ai_app experience_publication",
                                  generated_ts="2026-11-12T13:00:00Z")
ABSENT = {"kind": "db_member_absent", "source": "rey_apps",
          "identity": "code.development_backlog_vw.development_backlog_id"}
PRESENT = {"kind": "db_member_present", "source": "rey_apps",
           "identity": "code.development_backlog_vw.backlog_key"}


def _lesson(key: str = "backlog_key_is_sole_identifier", state: str = VALIDATED,
            claims: tuple[dict[str, Any], ...] = (ABSENT, PRESENT),
            evidence: tuple[EvidenceCheck, ...] | None = None) -> VerifiedLesson:
    return VerifiedLesson(
        experience_key=key, statement="said", knowledge_kind="observed_fact", state=state,
        evidence=evidence if evidence is not None else (
            EvidenceCheck({"kind": "db_object", "source": "rey_apps",
                           "identity": "code.development_backlog_vw"}, True, ""),
            EvidenceCheck({"kind": "execution", "run_log_id": 3290534}, True, "", 3290534),
            EvidenceCheck({"kind": "message", "conversation_id": 12, "sequence": 2}, True, "", 902),
            EvidenceCheck({"kind": "execution", "run_log_id": 1}, False, "not this object's"),
        ),
        claims=tuple(ClaimCheck(dict(one), VERIFIED, "") for one in claims))


def test_an_introduced_lesson_is_an_extraction_staged_as_the_rehearsal_proved() -> None:
    (generation,) = generations_for(SUBJECT, [Reconciled(_lesson(), INTRODUCE, "")], [], PROVENANCE)

    assert generation.recording_act == "extraction"
    assert generation.recording_run_manifest_id == 6369
    (experience,) = generation.experiences
    assert (experience.validation_state, experience.originating_run_manifest_id,
            experience.ai_task_id, experience.ai_contract_id) == (VALIDATED, 6369, 192, 219)
    assert [(c.evidence_role, c.member_kind, c.identity) for c in experience.code_evidence] == [
        ("recommendation", "db_object", "code.development_backlog_vw"),
        ("validation", "db_member_absent", "code.development_backlog_vw.development_backlog_id"),
        ("validation", "db_member", "code.development_backlog_vw.backlog_key"),
    ]
    # Resolved records only: the execution as outcome, the message as recommendation.
    assert [(e.evidence_role, e.run_log_id, e.message_id) for e in experience.evidence] == [
        ("outcome", 3290534, None), ("recommendation", None, 902)]


def test_a_consolidation_is_a_validation_act_citing_what_it_supports() -> None:
    (generation,) = generations_for(SUBJECT, [Reconciled(_lesson(), CONSOLIDATE, "", supports=32)],
                                    [], PROVENANCE)

    assert generation.recording_act == "validation"
    (experience,) = generation.experiences
    assert ("validation", 32) in [(e.evidence_role, e.supporting_ai_experience_id)
                                  for e in experience.evidence]


def test_an_invalidation_rejects_the_contradicted_identity_on_the_verified_claims() -> None:
    existing = [{"ai_experience_id": 7, "experience_key": "priority_rank_exists",
                 "experience_level": "knowledge", "statement": "priority_rank exists",
                 "knowledge_kind": "observed_fact"}]
    lesson = _lesson(key="no_priority_rank_column", claims=(
        {"kind": "db_member_absent", "source": "rey_apps",
         "identity": "code.development_backlog_vw.priority_rank"},))

    (generation,) = generations_for(SUBJECT, [Reconciled(lesson, INVALIDATE, "", invalidates=(7,))],
                                    existing, PROVENANCE)

    assert generation.recording_act == "validation"
    (rejected,) = generation.experiences
    assert (rejected.identity.experience_key, rejected.validation_state) == ("priority_rank_exists", "rejected")
    assert [(c.evidence_role, c.member_kind) for c in rejected.code_evidence] == [
        ("validation", "db_member_absent")]


def test_both_acts_when_both_are_called_for() -> None:
    generations = generations_for(SUBJECT, [
        Reconciled(_lesson(key="fresh"), INTRODUCE, ""),
        Reconciled(_lesson(), CONSOLIDATE, "", supports=32),
    ], [], PROVENANCE)

    assert [g.recording_act for g in generations] == ["extraction", "validation"]


def test_refused_and_left_lessons_stage_nothing() -> None:
    assert generations_for(SUBJECT, [Reconciled(_lesson(), REFUSE, ""),
                                     Reconciled(_lesson(state=CANDIDATE), LEAVE, "")],
                           [], PROVENANCE) == ()


def test_a_candidate_stages_its_resolved_evidence_and_no_claims() -> None:
    (generation,) = generations_for(SUBJECT, [Reconciled(_lesson(state=CANDIDATE, claims=()),
                                                          INTRODUCE, "")], [], PROVENANCE)

    (experience,) = generation.experiences
    assert experience.validation_state == CANDIDATE
    assert all(c.evidence_role == "recommendation" for c in experience.code_evidence)


def test_a_lesson_none_of_whose_evidence_resolved_is_not_staged() -> None:
    lesson = _lesson(state=CANDIDATE, claims=(),
                     evidence=(EvidenceCheck({"kind": "execution", "run_log_id": 1}, False, ""),))

    assert generations_for(SUBJECT, [Reconciled(lesson, INTRODUCE, "")], [], PROVENANCE) == ()
