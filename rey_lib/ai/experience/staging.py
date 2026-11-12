"""What reconciliation decided, as the recording acts ExperiencePublisher admits (#1344).

Each decision becomes staged rows exactly as the publication gates expect them --
the shape the real rehearsal proved (2026-11-12):

- a verified claim is a **validation**-role code evidence row: ``db_member`` for a
  column present, ``db_member_absent`` for one absent -- what a validated experience
  establishes, and what recall reads;
- a resolved index evidence item is a **recommendation**-role code evidence row;
- a resolved execution is an **outcome**-role record (its run_log_id), a resolved
  conversation message a **recommendation**-role record (its message_id);
- a consolidation also cites the authoritative experience it supports
  (**validation** role).

Recording acts, as the publication gate reads them: ``introduce`` is an
**extraction** (it may create an identity); ``consolidate``, ``validate`` and the
rejections ``invalidate`` records are **validation** acts on identities that exist.
``refuse`` and ``leave`` stage nothing. Evidence that did not resolve is never staged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping

from rey_lib.ai.experience.publication import (
    ExperienceIdentity,
    StagedCodeEvidence,
    StagedEvidence,
    StagedExperience,
    StagedGeneration,
)
from rey_lib.ai.experience.reconciliation import (
    CONSOLIDATE,
    INTRODUCE,
    INVALIDATE,
    VALIDATE,
    Reconciled,
)
from rey_lib.ai.experience.verification import REJECTED, VerificationSubject, VerifiedLesson

__all__ = ["ExperienceProvenance", "generations_for"]

_EXTRACTION: Final = "extraction"
_VALIDATION_ACT: Final = "validation"
_KNOWLEDGE: Final = "knowledge"
_CLAIM_KINDS: Final = {"db_member_present": "db_member", "db_member_absent": "db_member_absent"}


@dataclass(frozen=True)
class ExperienceProvenance:
    """Where the lessons came from: the extraction's task run, task and contract."""

    task_run_id: int
    ai_task_id: int
    ai_contract_id: int
    generator: str
    generated_ts: Any


def generations_for(subject: VerificationSubject, decisions: Iterable[Reconciled],
                    existing: Iterable[Mapping[str, Any]],
                    provenance: ExperienceProvenance) -> tuple[StagedGeneration, ...]:
    """The recording acts the decisions call for: an extraction, a validation, or both.

    Args:
        subject: The subject the lessons are about.
        decisions: Reconciliation's decisions for its verified lessons.
        existing: The subject's experience, as ``Control.ai_experiences`` answers it --
            the rows an invalidation rejects.
        provenance: The extraction's task run, task and contract.
    """
    by_id = {int(row["ai_experience_id"]): dict(row) for row in existing}
    introduced: dict[str, StagedExperience] = {}
    validated: dict[str, StagedExperience] = {}
    for decision in decisions:
        lesson = decision.lesson
        if decision.action == INTRODUCE:
            staged = _experience(subject, lesson, provenance)
            if staged is not None:
                introduced[lesson.experience_key] = staged
        elif decision.action in (CONSOLIDATE, VALIDATE):
            staged = _experience(subject, lesson, provenance, supports=decision.supports)
            if staged is not None:
                validated[lesson.experience_key] = staged
        elif decision.action == INVALIDATE:
            for target in decision.invalidates:
                row = by_id.get(int(target))
                if row is not None:
                    key = str(row.get("experience_key") or "")
                    validated[key] = _rejection(subject, row, lesson, provenance)
    acts = ((_EXTRACTION, introduced), (_VALIDATION_ACT, validated))
    return tuple(
        StagedGeneration(generated_ts=provenance.generated_ts, generator=provenance.generator,
                         recording_act=act, recording_run_manifest_id=provenance.task_run_id,
                         experiences=tuple(staged.values()))
        for act, staged in acts if staged
    )


def _experience(subject: VerificationSubject, lesson: VerifiedLesson,
                provenance: ExperienceProvenance,
                supports: int | None = None) -> StagedExperience | None:
    """One verified lesson as a staged experience; None when nothing of it resolved."""
    evidence: list[StagedEvidence] = []
    code: list[StagedCodeEvidence] = []
    for check in lesson.evidence:
        if not check.resolved:
            continue
        kind = str(check.item.get("kind") or "")
        if kind == "execution":
            evidence.append(StagedEvidence("outcome", run_log_id=check.record_id))
        elif kind == "message":
            evidence.append(StagedEvidence("recommendation", message_id=check.record_id))
        elif kind in ("db_object", "db_member"):
            code.append(StagedCodeEvidence("recommendation", str(check.item.get("source") or ""),
                                           str(check.item.get("identity") or ""), kind))
    code.extend(_claims(lesson.verified_claims))
    if supports is not None:
        evidence.append(StagedEvidence("validation", supporting_ai_experience_id=supports))
    if not evidence and not code:
        return None
    return StagedExperience(
        identity=_identity(subject, lesson.experience_key),
        experience_level=_KNOWLEDGE, statement=lesson.statement,
        knowledge_kind=lesson.knowledge_kind, validation_state=lesson.state,
        originating_run_manifest_id=provenance.task_run_id,
        ai_task_id=provenance.ai_task_id, ai_contract_id=provenance.ai_contract_id,
        evidence=tuple(evidence), code_evidence=tuple(code),
    )


def _rejection(subject: VerificationSubject, row: Mapping[str, Any], lesson: VerifiedLesson,
               provenance: ExperienceProvenance) -> StagedExperience:
    """A rejected generation of the identity the lesson's verified claims contradict."""
    return StagedExperience(
        identity=_identity(subject, str(row.get("experience_key") or "")),
        experience_level=str(row.get("experience_level") or _KNOWLEDGE),
        statement=str(row.get("statement") or ""),
        knowledge_kind=str(row.get("knowledge_kind") or ""),
        validation_state=REJECTED,
        originating_run_manifest_id=provenance.task_run_id,
        ai_task_id=provenance.ai_task_id, ai_contract_id=provenance.ai_contract_id,
        code_evidence=tuple(_claims(lesson.verified_claims)),
    )


def _claims(claims: Iterable[Mapping[str, Any]]) -> list[StagedCodeEvidence]:
    """Verified claims as validation-role code evidence."""
    return [StagedCodeEvidence("validation", str(one.get("source") or ""),
                               str(one.get("identity") or ""), _CLAIM_KINDS[str(one.get("kind"))])
            for one in claims if str(one.get("kind")) in _CLAIM_KINDS]


def _identity(subject: VerificationSubject, key: str) -> ExperienceIdentity:
    return ExperienceIdentity(subject.installation_id, subject.application_id,
                              dict(subject.subject_identity), key)
