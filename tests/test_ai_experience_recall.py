"""#1344 phase 5: bounded recall of verified experience through the #1337 context path.

Hydration reads a subject's experience only where the caller names its application and
subject and the task's governed limits both permit recall. Only authoritative, validated,
current experiences qualify, and only their verified claims are rendered -- after the
shared instructions and the contract, never before. LLM_PACKAGE records what was
retrieved, what was supplied and how the block was measured.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from rey_lib.ai.ai import AI
from rey_lib.ai.evidence import package_of
from rey_lib.ai.experience.recall import HEADING, MEASURE_METHOD, recall
from rey_lib.ai.task import AITask
from tests.test_ai_shared_instructions import GOVERNANCE, _configuration, _shared
from tests.test_ai_task_hydration import ENGINES, INSTRUCTIONS

SUBJECT = {"kind": "views", "name": "development_backlog_vw", "schema": "code",
           "catalog": "", "connection": "rey_apps"}
GENERATED = datetime(2026, 11, 12, 14, 37, tzinfo=timezone.utc)


def _experience(experience_id: int, *claims: tuple[str, str], generation: int = 18,
                generated_ts: datetime = GENERATED, **state: Any) -> dict[str, Any]:
    """One f_ai_experience_get row, every column it returns, under the routine's own names:
    authoritative, validated and current unless stated."""
    return {
        "ai_experience_id": experience_id, "ai_experience_generation_id": generation,
        "generated_ts": generated_ts, "recording_act": "extraction",
        "experience_key": f"lesson_{experience_id}", "experience_level": "subject",
        "knowledge_kind": "observed_fact",
        "validation_state": "validated", "statement": "THE MODEL'S OWN WORDS, NEVER SENT",
        "is_superseded": False, "staleness": "current", "is_authoritative": True,
        "verified_claims": [{"kind": kind, "source": "rey_apps", "identity": identity}
                            for kind, identity in claims],
        "evidence": [],
        **state,
    }


#: Experiences 34 and 35 as the first live cycle published them.
E35 = _experience(35, ("db_member_absent", "code.development_backlog_vw.superseded_by_id"),
                  ("db_member_present", "code.development_backlog_vw.superseded_by_key"))
E34 = _experience(34, ("db_member_present", "code.development_backlog_vw.demand_count"),
                  ("db_member_present", "code.development_backlog_vw.last_demanded_ts"))

BLOCK = (
    HEADING + "\n"
    "- ai_experience 35 (generation 18): rey_apps code.development_backlog_vw.superseded_by_id is absent\n"
    "- ai_experience 35 (generation 18): rey_apps code.development_backlog_vw.superseded_by_key is present\n"
    "- ai_experience 34 (generation 18): rey_apps code.development_backlog_vw.demand_count is present\n"
    "- ai_experience 34 (generation 18): rey_apps code.development_backlog_vw.last_demanded_ts is present"
)


def _limited(lessons: int | None = 5, tokens: int | None = 1000,
             default_lessons: int = 5, default_tokens: int = 1000) -> list[dict[str, Any]]:
    """The configuration rows with the view's resolved and default recall limits."""
    rows = _configuration(_shared(GOVERNANCE))
    for row in rows:
        row["default_experience_lesson_limit"] = default_lessons
        row["default_experience_token_limit"] = default_tokens
        row["experience_lesson_limit"] = default_lessons if lessons is None else lessons
        row["experience_token_limit"] = default_tokens if tokens is None else tokens
    return rows


class _Control:
    """A Control reading the configuration above and the subject's experience."""

    installation_id = 1

    def __init__(self, configuration: list[dict[str, Any]],
                 experiences: list[dict[str, Any]]) -> None:
        self.configuration = configuration
        self.experiences = experiences
        self.reads: list[tuple[int, int, dict[str, Any]]] = []

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        return deepcopy(self.configuration)

    def ai_engine_profiles(self, installation: str) -> list[dict[str, Any]]:
        return deepcopy(ENGINES)

    def ai_instructions(self) -> list[dict[str, Any]]:
        return [*deepcopy(INSTRUCTIONS), GOVERNANCE]

    def ai_experiences(self, installation_id: int, application_id: int,
                       subject_identity: dict[str, Any]) -> list[dict[str, Any]]:
        self.reads.append((installation_id, application_id, subject_identity))
        return deepcopy(self.experiences)


class TestSelection:
    def test_34_and_35_are_supplied_as_their_verified_claims_only(self) -> None:
        recalled = recall([E34, E35], 5, 1000)

        assert recalled.retrieved == (35, 34)
        assert [one["ai_experience_id"] for one in recalled.supplied] == [35, 34]
        assert recalled.block == BLOCK
        assert "MODEL'S OWN WORDS" not in recalled.block
        assert recalled.measure == {"method": MEASURE_METHOD, "exact": False,
                                    "count": len(BLOCK.encode("utf-8")),
                                    "lesson_limit": 5, "token_limit": 1000}

    def test_only_authoritative_validated_current_experience_with_claims_qualifies(self) -> None:
        rows = [
            _experience(1, ("db_member_present", "s.v.a"), is_authoritative=False),
            _experience(2, ("db_member_present", "s.v.a"), validation_state="candidate"),
            _experience(3, ("db_member_present", "s.v.a"), staleness="stale"),
            _experience(4),
            _experience(5, ("db_member", "s.v.a")),
            _experience(6, ("db_member_present", "s.v.a")),
        ]

        assert recall(rows, 5, 1000).retrieved == (6,)

    def test_the_newest_generation_ranks_first_then_the_higher_id(self) -> None:
        older = _experience(40, ("db_member_present", "s.v.a"),
                            generated_ts=datetime(2026, 11, 1, tzinfo=timezone.utc))
        rows = [older, _experience(7, ("db_member_present", "s.v.b")),
                _experience(9, ("db_member_present", "s.v.c"))]

        assert recall(rows, 5, 1000).retrieved == (9, 7, 40)

    def test_the_lesson_limit_bounds_what_is_supplied_not_what_is_retrieved(self) -> None:
        recalled = recall([E34, E35], 1, 1000)

        assert recalled.retrieved == (35, 34)
        assert [one["ai_experience_id"] for one in recalled.supplied] == [35]

    def test_an_oversized_lesson_is_skipped_and_a_later_one_that_fits_is_taken(self) -> None:
        large = _experience(50, *(("db_member_present", f"s.v.column_{n:03}") for n in range(20)))
        small = _experience(10, ("db_member_present", "s.v.a"),
                            generated_ts=datetime(2026, 11, 1, tzinfo=timezone.utc))
        budget = len(f"{HEADING}\n{recall([small], 1, 10_000).supplied[0]['content']}".encode())

        recalled = recall([large, small], 5, budget)

        assert recalled.retrieved == (50, 10)
        assert [one["ai_experience_id"] for one in recalled.supplied] == [10]
        assert recalled.measure["count"] == budget

    def test_the_whole_block_heading_included_is_counted(self) -> None:
        exact = len(BLOCK.encode("utf-8"))

        assert [one["ai_experience_id"] for one in recall([E34, E35], 5, exact).supplied] == [35, 34]
        assert [one["ai_experience_id"] for one in recall([E34, E35], 5, exact - 1).supplied] == [35]

    def test_nothing_fits_retrieved_but_none_supplied_and_no_block(self) -> None:
        recalled = recall([E35], 5, 10)

        assert (recalled.retrieved, recalled.supplied, recalled.block) == ((35,), (), "")


class TestHydration:
    def test_the_named_application_and_subject_are_read_and_recalled(self) -> None:
        control = _Control(_limited(), [E34, E35])

        task = AITask.from_governed(control, "local", "search_index",
                                    application_id=6, subject_reference=SUBJECT)

        assert control.reads == [(1, 6, SUBJECT)]
        assert task.application_id == 6
        assert task.experiences is not None and task.experiences.block == BLOCK

    def test_without_an_application_or_subject_nothing_is_read(self) -> None:
        control = _Control(_limited(), [E34, E35])

        assert AITask.from_governed(control, "local", "search_index",
                                    subject_reference=SUBJECT).experiences is None
        assert AITask.from_governed(control, "local", "search_index",
                                    application_id=6).experiences is None
        assert control.reads == []

    def test_a_zero_limit_disables_recall_nothing_is_read(self) -> None:
        for lessons, tokens in ((0, 1000), (5, 0)):
            control = _Control(_limited(lessons, tokens), [E34, E35])

            task = AITask.from_governed(control, "local", "search_index",
                                        application_id=6, subject_reference=SUBJECT)

            assert (task.experiences, control.reads) == (None, [])

    def test_the_tasks_own_limit_is_the_one_applied(self) -> None:
        control = _Control(_limited(lessons=1), [E34, E35])

        task = AITask.from_governed(control, "local", "search_index",
                                    application_id=6, subject_reference=SUBJECT)

        assert [one["ai_experience_id"] for one in task.experiences.supplied] == [35]
        assert task.experiences.measure["lesson_limit"] == 1

    def test_an_unconfigured_task_takes_the_installations_defaults(self) -> None:
        control = _Control(_limited(lessons=0, default_lessons=5), [E34, E35])

        task = AITask.from_governed(control, "local", "not_configured",
                                    application_id=6, subject_reference=SUBJECT)

        assert task.experiences is not None and task.experiences.measure["lesson_limit"] == 5

    def test_a_configuration_stating_no_limits_recalls_nothing(self) -> None:
        control = _Control(_configuration(_shared(GOVERNANCE)), [E34, E35])

        task = AITask.from_governed(control, "local", "search_index",
                                    application_id=6, subject_reference=SUBJECT)

        assert (task.experiences, control.reads) == (None, [])

    def test_rehydration_keeps_the_application_and_reads_again(self) -> None:
        ai, control = AI(), _Control(_limited(), [E35])
        first = ai.hydrate_task(control, "local", task_key="search_index",
                                application_id=6, subject_reference=SUBJECT)
        control.experiences = [E34, E35]
        second = ai.rehydrate_task(control, first.id, {})

        assert second.application_id == 6
        assert first.experiences.retrieved == (35,)
        assert second.experiences.retrieved == (35, 34)


class TestComposition:
    def test_the_experience_block_comes_after_the_shared_instructions_and_the_contract(self) -> None:
        task = AITask.hydrate(_limited(), ENGINES, [*INSTRUCTIONS, GOVERNANCE],
                              experiences=[E34, E35], installation="local",
                              task_key="search_index")

        assert task.system_instructions() == (
            GOVERNANCE["body"] + "\n\n" + task.contract_body + "\n\n" + BLOCK)

    def test_with_nothing_supplied_there_is_no_block(self) -> None:
        task = AITask.hydrate(_limited(tokens=10), ENGINES, [*INSTRUCTIONS, GOVERNANCE],
                              experiences=[E35], installation="local", task_key="search_index")

        assert task.system_instructions() == GOVERNANCE["body"] + "\n\n" + task.contract_body


class TestPackage:
    def test_the_package_records_what_was_retrieved_supplied_and_how_it_was_measured(self) -> None:
        task = AITask.hydrate(_limited(lessons=1), ENGINES, [*INSTRUCTIONS, GOVERNANCE],
                              experiences=[E34, E35], installation="local",
                              task_key="search_index")

        experience = package_of(task, None, "", None)["experience"]

        assert experience["retrieved"] == [35, 34]
        assert experience["supplied"] == [{"ai_experience_id": 35, "generation": 18,
                                           "content": task.experiences.supplied[0]["content"]}]
        assert experience["block"] in task.system_instructions()
        assert experience["measure"]["method"] == MEASURE_METHOD
        assert experience["measure"]["exact"] is False

    def test_recall_disabled_or_nothing_retrieved_leaves_no_key(self) -> None:
        disabled = AITask.hydrate(_limited(lessons=0), ENGINES, [*INSTRUCTIONS, GOVERNANCE],
                                  experiences=[E35], installation="local", task_key="search_index")
        empty = AITask.hydrate(_limited(), ENGINES, [*INSTRUCTIONS, GOVERNANCE],
                               experiences=[], installation="local", task_key="search_index")

        assert "experience" not in package_of(disabled, None, "", None)
        assert "experience" not in package_of(empty, None, "", None)
