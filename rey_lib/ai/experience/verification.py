"""Verify candidate lessons against the evidence they name (#1344 step 3).

**It decides what the evidence establishes -- and nothing more.** An extraction
proposes lessons; this checks each one: does every evidence item it names exist and
belong to the subject, and does every checkable claim hold in the authoritative
record. A lesson is never promoted beyond what that proves:

- ``validated`` -- an ``observed_fact`` whose evidence all resolves to the subject and
  which carries at least one checkable claim, every one verified. What it establishes
  is its verified claims; the model's statement is kept beside them, never certified.
- ``rejected`` -- a checkable claim the authoritative record contradicts.
- ``candidate`` -- everything else: an inference, too few verifiable claims, a claim
  that cannot be decided, or evidence that does not resolve.

**What each check proves.** An execution proves it ran on this object with that
status -- not that its SQL or its result was right. A conversation message proves it
was said about this object. A ``db_member_present`` claim is verified when the object
resolves to exactly one indexed object, in the named source, that has the column; a
``db_member_absent`` claim when that one object has no column of that name. Both are
the indexed state only, never the live database; either is contradicted only when
the object resolves and the opposite holds; anything else is undecided.

**Where it reads.** The subject's executions and conversations, and the database
index (``code.f_experience_indexed_object`` over ``code.db_object_vw`` and
``code.db_member_vw``, #1360), all through Control's governed reads. The host
resolves Control and hands it in; nothing here reads installation context. Publication enforces admissibility
again on its own (the database never trusts a submitted state).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping

__all__ = [
    "CANDIDATE", "REJECTED", "VALIDATED", "ClaimCheck", "EvidenceCheck",
    "ExperienceVerifier", "VerificationSubject", "VerifiedLesson",
]

#: The validation states a verification decides, as ai.ai_experience names them.
VALIDATED: Final = "validated"
CANDIDATE: Final = "candidate"
REJECTED: Final = "rejected"

#: What a claim's check concluded.
VERIFIED: Final = "verified"
CONTRADICTED: Final = "contradicted"
UNDECIDED: Final = "undecided"

#: The only kind a verification validates on its own (user, 2026-11-12).
_OBSERVED_FACT: Final = "observed_fact"
_PRESENT: Final = "db_member_present"
_ABSENT: Final = "db_member_absent"


@dataclass(frozen=True)
class VerificationSubject:
    """The subject the lessons are about: its scope and the database object it is."""

    installation_id: int
    application_id: int
    subject_identity: Mapping[str, Any]

    @property
    def connection(self) -> str:
        """The object's connection: the source its index evidence must be in."""
        return str(self.subject_identity.get("connection") or "")

    @property
    def schema(self) -> str:
        """The object's schema."""
        return str(self.subject_identity.get("schema") or "")

    @property
    def name(self) -> str:
        """The object's name."""
        return str(self.subject_identity.get("name") or "")

    @property
    def target(self) -> str:
        """The object as the index names it: ``schema.object``."""
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class EvidenceCheck:
    """One evidence item as proposed, whether it resolved, and the record it named."""

    item: Mapping[str, Any]
    resolved: bool
    reason: str
    #: The record it resolved to: the run_log_id, or the conversation message's id.
    record_id: int | None = None


@dataclass(frozen=True)
class ClaimCheck:
    """One checkable claim as proposed, and what the authoritative record said of it."""

    claim: Mapping[str, Any]
    verdict: str
    reason: str


@dataclass(frozen=True)
class VerifiedLesson:
    """A proposed lesson with what its evidence established."""

    experience_key: str
    statement: str
    knowledge_kind: str
    state: str
    evidence: tuple[EvidenceCheck, ...]
    claims: tuple[ClaimCheck, ...]
    relates_to: Mapping[str, Any] | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verified_claims(self) -> tuple[Mapping[str, Any], ...]:
        """What the lesson establishes: its claims the record verified, and only those."""
        return tuple(one.claim for one in self.claims if one.verdict == VERIFIED)


@dataclass(frozen=True)
class _Indexed:
    """One object as the index holds it, decided once per verification."""

    objects: int
    in_source: bool
    members: frozenset[str]

    @property
    def resolves(self) -> bool:
        return self.objects == 1 and self.in_source


class ExperienceVerifier:
    """Checks proposed lessons against their evidence, for one subject at a time."""

    def __init__(self, control: Any, index_connection: Any) -> None:
        """Hold what the host resolved.

        Args:
            control: The host's Control: the subject's executions and conversations.
            index_connection: A shared connection whose login may read the database
                index views.
        """
        self._control = control
        self._index = index_connection

    def verify(self, subject: VerificationSubject,
               answer: Mapping[str, Any]) -> tuple[VerifiedLesson, ...]:
        """Each lesson the extraction proposed, with what its evidence established.

        Args:
            subject: The subject the extraction was about.
            answer: The extraction's validated answer: ``{"lessons": [...]}``.

        Raises:
            AppError: As Control or the index read raises -- a read that fails is
                never taken for evidence that does not exist.
        """
        executions = {
            int(row["run_log_id"]) for row in self._control.sql_executions(
                subject.installation_id, subject.connection, subject.schema, subject.name)
        }
        indexed: dict[tuple[str, str], _Indexed] = {}
        conversations: dict[int, dict[int, int] | None] = {}
        return tuple(
            self._lesson(subject, dict(one), executions, indexed, conversations)
            for one in (answer.get("lessons") or ())
        )

    # -- one lesson ------------------------------------------------------------------

    def _lesson(self, subject: VerificationSubject, lesson: dict[str, Any],
                executions: set[int], indexed: dict[tuple[str, str], _Indexed],
                conversations: dict[int, dict[int, int] | None]) -> VerifiedLesson:
        evidence = tuple(self._evidence(subject, dict(item), executions, indexed, conversations)
                         for item in (lesson.get("evidence") or ()))
        claims = tuple(self._claim(subject, dict(item), indexed)
                       for item in (lesson.get("claims") or ()))
        kind = str(lesson.get("knowledge_kind") or "")
        reasons: list[str] = []
        if any(one.verdict == CONTRADICTED for one in claims):
            state = REJECTED
            reasons.append("a checkable claim is contradicted by the indexed state")
        else:
            if kind != _OBSERVED_FACT:
                reasons.append(f"a {kind or 'kind-less'} lesson is never validated automatically")
            if not claims:
                reasons.append("no checkable claim establishes what it would teach")
            if any(one.verdict != VERIFIED for one in claims):
                reasons.append("not every checkable claim is verified")
            if not evidence or any(not one.resolved for one in evidence):
                reasons.append("not every evidence item resolves to this subject")
            state = CANDIDATE if reasons else VALIDATED
        relates = lesson.get("relates_to")
        return VerifiedLesson(
            experience_key=str(lesson.get("experience_key") or ""),
            statement=str(lesson.get("statement") or ""),
            knowledge_kind=kind, state=state, evidence=evidence, claims=claims,
            relates_to=dict(relates) if isinstance(relates, Mapping) else None,
            reasons=tuple(reasons),
        )

    # -- evidence --------------------------------------------------------------------

    def _evidence(self, subject: VerificationSubject, item: dict[str, Any],
                  executions: set[int], indexed: dict[tuple[str, str], _Indexed],
                  conversations: dict[int, dict[int, int] | None]) -> EvidenceCheck:
        kind = str(item.get("kind") or "")
        if kind == "execution":
            run_log_id = int(item.get("run_log_id") or 0)
            if run_log_id in executions:
                return EvidenceCheck(item, True, "an execution of this object", run_log_id)
            return EvidenceCheck(item, False, "no execution of this object has that run_log_id")
        if kind == "message":
            messages = self._conversation(subject, int(item.get("conversation_id") or 0),
                                          conversations)
            if messages is None:
                return EvidenceCheck(item, False, "no conversation about this object has that id")
            message_id = messages.get(int(item.get("sequence") or 0))
            if message_id is None:
                return EvidenceCheck(item, False, "the conversation has no message at that sequence")
            return EvidenceCheck(item, True, "a message about this object", message_id)
        if kind in ("db_object", "db_member"):
            target, member = _parts(str(item.get("identity") or ""), kind == "db_member")
            held = self._indexed(subject, str(item.get("source") or ""), target, indexed)
            if not held.resolves:
                return EvidenceCheck(item, False, "the object does not resolve to exactly one "
                                                  "indexed object in that source")
            if member is not None and member not in held.members:
                return EvidenceCheck(item, False, "the indexed object has no such column")
            return EvidenceCheck(item, True, "in the database index")
        return EvidenceCheck(item, False, f"evidence of kind {kind!r} is not one this checks")

    def _conversation(self, subject: VerificationSubject, conversation_id: int,
                      conversations: dict[int, dict[int, int] | None]) -> dict[int, int] | None:
        """The conversation's messages by sequence -- only if it is this subject's."""
        if conversation_id not in conversations:
            rows = self._control.ai_conversations(conversation_id=conversation_id)
            ours = [row for row in rows
                    if int(row.get("installation_id") or 0) == subject.installation_id
                    and int(row.get("application_id") or 0) == subject.application_id
                    and dict(row.get("subject_identity") or {}) == dict(subject.subject_identity)]
            conversations[conversation_id] = (
                {int(one["sequence"]): int(one["message_id"])
                 for one in self._control.ai_conversation_messages(conversation_id)}
                if ours else None
            )
        return conversations[conversation_id]

    # -- claims ----------------------------------------------------------------------

    def _claim(self, subject: VerificationSubject, claim: dict[str, Any],
               indexed: dict[tuple[str, str], _Indexed]) -> ClaimCheck:
        kind = str(claim.get("kind") or "")
        if kind not in (_PRESENT, _ABSENT):
            return ClaimCheck(claim, UNDECIDED, f"a claim of kind {kind!r} is not one this checks")
        target, member = _parts(str(claim.get("identity") or ""), True)
        if target != subject.target:
            return ClaimCheck(claim, UNDECIDED, "the claim is not about this object")
        held = self._indexed(subject, str(claim.get("source") or ""), target, indexed)
        if not held.resolves:
            return ClaimCheck(claim, UNDECIDED, "the object does not resolve to exactly one "
                                                "indexed object in that source")
        present = member in held.members
        if (kind == _PRESENT) == present:
            return ClaimCheck(claim, VERIFIED,
                              "the indexed object has the column" if present
                              else "the indexed object has no such column")
        return ClaimCheck(claim, CONTRADICTED,
                          "the indexed object has the column" if present
                          else "the indexed object has no such column")

    # -- the index -------------------------------------------------------------------

    def _indexed(self, subject: VerificationSubject, source: str, target: str,
                 indexed: dict[tuple[str, str], _Indexed]) -> _Indexed:
        """The object as the index holds it; read once per verification."""
        key = (source, target)
        if key not in indexed:
            schema, _, name = target.rpartition(".")
            rows = self._control.experience_indexed_object(schema, name)
            objects = {int(row["code_db_object_id"]): str(row.get("source_key") or "")
                       for row in rows}
            only = next(iter(objects), None)
            indexed[key] = _Indexed(
                objects=len(objects),
                in_source=len(objects) == 1 and objects[only] == source == subject.connection,
                members=frozenset(str(row["member_name"]) for row in rows
                                  if row.get("member_name") is not None
                                  and int(row["code_db_object_id"]) == only),
            )
        return indexed[key]


def _parts(identity: str, has_member: bool) -> tuple[str, str | None]:
    """``schema.object`` and, for a member, its column name."""
    if not has_member:
        return identity, None
    target, _, member = identity.rpartition(".")
    return target, member
