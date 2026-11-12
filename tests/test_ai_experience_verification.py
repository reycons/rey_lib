"""Verification decides what a lesson's evidence establishes, and nothing more (#1344).

No database: Control and the index read are stand-ins. What is asserted is the rule
(user, 2026-11-12): validated only for an observed fact whose evidence all resolves to
the subject and whose checkable claims are all verified; a contradicted claim rejects;
everything else stays a candidate; what a validated lesson establishes is its verified
claims, never its statement.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.ai.experience import verification as module
from rey_lib.ai.experience.verification import (
    CANDIDATE,
    REJECTED,
    VALIDATED,
    ExperienceVerifier,
    VerificationSubject,
)

SUBJECT_IDENTITY = {"kind": "views", "name": "development_backlog_vw", "schema": "code",
                    "catalog": "", "connection": "rey_apps"}
SUBJECT = VerificationSubject(installation_id=1, application_id=6,
                              subject_identity=SUBJECT_IDENTITY)
#: The index as it holds the subject: one object in rey_apps with these columns.
INDEXED = [{"code_db_object_id": 30438, "source_key": "rey_apps", "member_name": name}
           for name in ("backlog_key", "priority", "status", "demand_count")]


class _Control:
    """The subject's executions and conversations, as the governed reads answer them."""

    def __init__(self) -> None:
        self.asked: list[tuple[Any, ...]] = []

    def sql_executions(self, installation_id: int, connection: str, schema: str,
                       name: str) -> list[dict[str, Any]]:
        self.asked.append((installation_id, connection, schema, name))
        return [{"run_log_id": 3290534, "status": "failed"},
                {"run_log_id": 3308638, "status": "success"}]

    def ai_conversations(self, *, conversation_id: int) -> list[dict[str, Any]]:
        identity = SUBJECT_IDENTITY if conversation_id == 12 else {"kind": "tables", "name": "other"}
        return [{"conversation_id": conversation_id, "installation_id": 1, "application_id": 6,
                 "subject_identity": identity}]

    def ai_conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        return [{"message_id": 900 + sequence, "sequence": sequence} for sequence in (1, 2)]


class _Connection:
    def handle(self) -> object:
        return object()


@pytest.fixture(autouse=True)
def _index(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The index read: the statement's parameters are recorded, INDEXED answered."""
    asked: list[dict[str, Any]] = []
    rows = {"code.development_backlog_vw": INDEXED}

    class _Query:
        def __init__(self, statement: str, params: dict[str, Any]) -> None:
            assert "code.db_object_vw" in statement and ":schema" in statement
            self.params = params

        def rows(self, _handle: Any) -> tuple[dict[str, Any], ...]:
            asked.append(dict(self.params))
            return tuple(rows.get(f"{self.params['schema']}.{self.params['name']}", ()))

    monkeypatch.setattr(module, "DatabaseQuery", _Query)
    return asked


def _verify(*lessons: dict[str, Any]) -> Any:
    return ExperienceVerifier(_Control(), _Connection()).verify(SUBJECT, {"lessons": list(lessons)})


def _lesson(**facts: Any) -> dict[str, Any]:
    lesson = {
        "experience_key": "no_priority_rank_column",
        "statement": "priority_rank does not exist in development_backlog_vw; use priority.",
        "knowledge_kind": "observed_fact",
        "evidence": [{"kind": "execution", "run_log_id": 3290534},
                     {"kind": "message", "conversation_id": 12, "sequence": 2},
                     {"kind": "db_member", "source": "rey_apps",
                      "identity": "code.development_backlog_vw.priority"}],
        "claims": [{"kind": "db_member_absent", "source": "rey_apps",
                    "identity": "code.development_backlog_vw.priority_rank"},
                   {"kind": "db_member_present", "source": "rey_apps",
                    "identity": "code.development_backlog_vw.priority"}],
    }
    lesson.update(facts)
    return lesson


class TestWhatIsValidated:
    def test_an_observed_fact_with_resolved_evidence_and_verified_claims_is_validated(self) -> None:
        (lesson,) = _verify(_lesson())

        assert lesson.state == VALIDATED
        assert [one.record_id for one in lesson.evidence] == [3290534, 902, None]
        # What it establishes is its verified claims -- never its statement.
        assert lesson.verified_claims == tuple(_lesson()["claims"])

    def test_one_index_read_serves_every_check_on_the_same_object(self, _index: list) -> None:
        _verify(_lesson())

        assert _index == [{"schema": "code", "name": "development_backlog_vw"}]

    def test_a_present_column_verifies_a_presence_claim_and_nothing_about_values(self) -> None:
        """#1344: demand_count_never_null -- the column exists; that is all it proves.
        The claim verifies; the statement about NULLs is not established by it."""
        (lesson,) = _verify(_lesson(
            statement="demand_count is never NULL: the DDL wraps it in COALESCE.",
            evidence=[{"kind": "db_object", "source": "rey_apps",
                       "identity": "code.development_backlog_vw"}],
            claims=[{"kind": "db_member_present", "source": "rey_apps",
                     "identity": "code.development_backlog_vw.demand_count"}]))

        assert lesson.verified_claims == ({"kind": "db_member_present", "source": "rey_apps",
                                           "identity": "code.development_backlog_vw.demand_count"},)


class TestWhatStaysACandidate:
    @pytest.mark.parametrize("change, reason", [
        ({"knowledge_kind": "inference"}, "never validated automatically"),
        ({"claims": []}, "no checkable claim"),
        ({"evidence": [{"kind": "execution", "run_log_id": 1}]}, "evidence item resolves"),
        ({"evidence": [{"kind": "message", "conversation_id": 5, "sequence": 1}]},
         "evidence item resolves"),
        ({"evidence": [{"kind": "message", "conversation_id": 12, "sequence": 7}]},
         "evidence item resolves"),
        ({"claims": [{"kind": "db_member_absent", "source": "rey_apps",
                      "identity": "code.other_view.priority_rank"}]}, "not every checkable claim"),
        ({"claims": [{"kind": "db_member_absent", "source": "another_source",
                      "identity": "code.development_backlog_vw.priority_rank"}]},
         "not every checkable claim"),
    ])
    def test_not_validated(self, change: dict[str, Any], reason: str) -> None:
        (lesson,) = _verify(_lesson(**change))

        assert lesson.state == CANDIDATE
        assert any(reason in one for one in lesson.reasons), lesson.reasons

    def test_an_object_the_index_does_not_hold_decides_nothing(self) -> None:
        (lesson,) = _verify(_lesson(claims=[{"kind": "db_member_absent", "source": "rey_apps",
                                             "identity": "code.development_backlog_vw.x"}],
                                    evidence=[{"kind": "db_object", "source": "rey_apps",
                                               "identity": "code.missing_vw"}]))

        assert lesson.state == CANDIDATE


class TestWhatIsRejected:
    @pytest.mark.parametrize("claim", [
        {"kind": "db_member_absent", "source": "rey_apps",
         "identity": "code.development_backlog_vw.priority"},
        {"kind": "db_member_present", "source": "rey_apps",
         "identity": "code.development_backlog_vw.priority_rank"},
    ])
    def test_a_claim_the_index_contradicts_rejects_the_lesson(self, claim: dict[str, Any]) -> None:
        (lesson,) = _verify(_lesson(claims=[claim]))

        assert lesson.state == REJECTED
        assert lesson.verified_claims == ()

    def test_an_ambiguous_object_contradicts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two indexed objects of that name: undecided, never a contradiction."""
        twice = INDEXED + [{"code_db_object_id": 1, "source_key": "rey_apps",
                            "member_name": "priority"}]

        class _Twice:
            def __init__(self, statement: str, params: dict[str, Any]) -> None:
                pass

            def rows(self, _handle: Any) -> tuple[dict[str, Any], ...]:
                return tuple(twice)

        monkeypatch.setattr(module, "DatabaseQuery", _Twice)
        (lesson,) = _verify(_lesson())

        assert lesson.state == CANDIDATE


class TestTheBoundary:
    def test_executions_are_read_for_the_subject_alone(self) -> None:
        control = _Control()
        ExperienceVerifier(control, _Connection()).verify(SUBJECT, {"lessons": [_lesson()]})

        assert control.asked == [(1, "rey_apps", "code", "development_backlog_vw")]

    def test_no_lessons_is_no_verification(self) -> None:
        assert _verify() == ()
