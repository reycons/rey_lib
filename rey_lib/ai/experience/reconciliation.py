"""Reconcile verified lessons with the subject's existing experience (#1344 step 3).

Verification says what each proposed lesson's evidence establishes; this says what
that may change in what Rey already knows about the subject -- and refuses the rest.
The existing experience is the governed read's (``Control.ai_experiences``): every
generation of the subject, with its state, authority, staleness and verified claims.

The rules (user, 2026-11-12):

- **introduce** -- a key the subject has never recorded, proposed as a validated or
  candidate lesson.
- **consolidate** -- a validated lesson whose verified claims are exactly the
  authoritative experience's: a new generation of that identity, citing it. More
  evidence never makes a generation more authoritative than the one it supports.
- **validate** -- a validated lesson for a key with no authoritative experience yet.
- **invalidate** -- an authoritative experience a verified claim contradicts (the same
  column, present against absent): a rejected generation of that identity.
- **refuse** -- a validated lesson whose verified claims differ from the authoritative
  experience of the same key: never merged automatically.
- **leave** -- anything not validated, for a key already recorded: unverified claims
  never alter what is recorded. A rejected proposal of a new key changes nothing.

Claims are compared as what they assert -- kind, source and identity -- never by key
or statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping

from rey_lib.ai.experience.verification import REJECTED, VALIDATED, VerifiedLesson

__all__ = [
    "CONSOLIDATE", "INTRODUCE", "INVALIDATE", "LEAVE", "REFUSE", "VALIDATE",
    "Reconciled", "reconcile",
]

INTRODUCE: Final = "introduce"
CONSOLIDATE: Final = "consolidate"
VALIDATE: Final = "validate"
INVALIDATE: Final = "invalidate"
REFUSE: Final = "refuse"
LEAVE: Final = "leave"

_OPPOSITE: Final = {"db_member_present": "db_member_absent", "db_member_absent": "db_member_present"}

#: One claim as what it asserts.
_Claim = tuple[str, str, str]


@dataclass(frozen=True)
class Reconciled:
    """What one verified lesson may change, and why."""

    lesson: VerifiedLesson
    action: str
    reason: str
    #: The authoritative experience consolidated, or the one a validation follows.
    supports: int | None = None
    #: The authoritative experiences this lesson's verified claims contradict.
    invalidates: tuple[int, ...] = field(default_factory=tuple)


def reconcile(lessons: Iterable[VerifiedLesson],
              existing: Iterable[Mapping[str, Any]]) -> tuple[Reconciled, ...]:
    """Each verified lesson's change to the subject's experience, by the rules above.

    Args:
        lessons: The verifier's lessons for one subject.
        existing: The subject's experience as ``Control.ai_experiences`` answers it.
    """
    rows = [dict(one) for one in existing]
    recorded = {str(row.get("experience_key") or "") for row in rows}
    authoritative = {
        str(row.get("experience_key") or ""): row for row in rows
        if row.get("is_authoritative") and row.get("validation_state") == VALIDATED
        and row.get("staleness") == "current"
    }
    return tuple(_one(lesson, recorded, authoritative) for lesson in lessons)


def _one(lesson: VerifiedLesson, recorded: set[str],
         authoritative: dict[str, dict[str, Any]]) -> Reconciled:
    claims = {_asserted(one) for one in lesson.verified_claims}
    # What this lesson's verified claims contradict, whatever its own key.
    contradicted = tuple(
        int(row["ai_experience_id"]) for row in authoritative.values()
        if any((_OPPOSITE.get(kind, ""), source, identity) in claims
               for kind, source, identity in map(_asserted, row.get("verified_claims") or ()))
    )
    key = lesson.experience_key
    if lesson.state != VALIDATED:
        if key in recorded:
            return Reconciled(lesson, LEAVE, "not validated: unverified claims never alter "
                                             "what is recorded")
        if lesson.state == REJECTED:
            return Reconciled(lesson, LEAVE, "a rejected proposal of a new key changes nothing")
        return Reconciled(lesson, INTRODUCE, "a new key, recorded as a candidate")
    if contradicted:
        return Reconciled(lesson, INVALIDATE, "its verified claims contradict authoritative "
                                              "experience", invalidates=contradicted)
    current = authoritative.get(key)
    if current is None:
        if key in recorded:
            return Reconciled(lesson, VALIDATE, "the key has no authoritative experience yet")
        return Reconciled(lesson, INTRODUCE, "a new key, validated by its claims")
    if claims == {_asserted(one) for one in current.get("verified_claims") or ()}:
        return Reconciled(lesson, CONSOLIDATE, "the same verified claims as the authoritative "
                                               "experience", supports=int(current["ai_experience_id"]))
    return Reconciled(lesson, REFUSE, "the same key with materially different verified claims "
                                      "is never merged automatically")


def _asserted(claim: Mapping[str, Any]) -> _Claim:
    """A claim as what it asserts: its kind, source and identity."""
    return (str(claim.get("kind") or ""), str(claim.get("source") or ""),
            str(claim.get("identity") or ""))
