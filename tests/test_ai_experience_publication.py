"""Staging is data movement; admission is a governed operation (#1344 slice 2).

These run without a database, with the inferred-architecture publisher's fakes:
an adapter that records staged rows and refuses assembled SQL, and a recorder
in place of the procedure map. What they assert is the part a plausible
refactor would break silently -- the ai map and its bindings, the parent step,
the publication key on every row, identity on every child row, and the order
the procedure expects. What the procedure refuses (evidence that does not
support, cycles, unresolvable code, provenance, a user-confirmed correction)
is the procedure's, validated against the database in a rolled-back
transaction, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.ai.experience import publication as module
from rey_lib.ai.experience import (
    ExperienceIdentity,
    ExperiencePublisher,
    PublicationError,
    StagedCodeEvidence,
    StagedDependency,
    StagedEvidence,
    StagedExperience,
    StagedGeneration,
)

_SUBJECT = {"kind": "tables", "name": "run_vw"}
_EPISODE = ExperienceIdentity(1, 6, _SUBJECT, "sql_counts")
_RULE = ExperienceIdentity(1, 6, _SUBJECT, "count_rule")


def _generation() -> StagedGeneration:
    """An extraction: an episode, and knowledge citing it within the same publication."""
    return StagedGeneration(
        generated_ts="2026-11-12T12:00:00Z",
        generator="test",
        recording_act="extraction",
        recording_run_manifest_id=6200,
        experiences=(
            StagedExperience(
                identity=_EPISODE, experience_level="episode",
                statement="generated a count over run_vw", knowledge_kind="observed_fact",
                validation_state="candidate", originating_run_manifest_id=6173,
                ai_task_id=3, ai_contract_id=195,
                evidence=(StagedEvidence("recommendation", run_manifest_id=6173),),
            ),
            StagedExperience(
                identity=_RULE, experience_level="knowledge",
                statement="counts over run_vw group by run", knowledge_kind="inference",
                validation_state="candidate",
                evidence=(StagedEvidence("recommendation", cites=_EPISODE),),
                code_evidence=(StagedCodeEvidence("recommendation", "rey_lib", "rey_lib/ai", "package"),),
                dependencies=(StagedDependency(development_rule_id=126),),
            ),
        ),
    )


class _Adapter:
    """Records staged rows; fails the test on anything that looks like assembled SQL."""

    def __init__(self) -> None:
        self.staged: list[tuple[str, list[dict]]] = []

    def bulk_insert(self, _conn, schema, table, rows, columns):
        assert schema == "ai_stage"
        for row in rows:
            assert set(row) == set(columns), f"{table}: row/column mismatch"
        self.staged.append((table, list(rows)))
        return len(rows)

    def execute_sql(self, *_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("the publisher must not assemble SQL")


class _Calls(list):
    """Stands in for ProcedureMap, recording each call's map, binding and values."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: str | None = None

    def install(self, monkeypatch) -> None:
        calls = self

        class _ProcedureMap:
            def __init__(self, map_name, action):
                self.map_name, self.action = map_name, action

            @classmethod
            def resolve(cls, _conn, map_cfg, map_name, action, adapter=None):
                calls.maps.append(map_cfg)
                return cls(map_name, action)

            def call(self, _conn, values, _run_log, run_ctx=None):
                calls.append((self.map_name, self.action, dict(values)))
                if self.action == calls.fail_on:
                    raise RuntimeError("experiences whose evidence does not support them")
                return {"outputs": {}, "rows": None}

        self.maps: list = []
        monkeypatch.setattr(module, "ProcedureMap", _ProcedureMap)


@pytest.fixture()
def calls(monkeypatch):
    recorded = _Calls()
    recorded.install(monkeypatch)
    return recorded


#: The installation's control map entry, as the host resolved it.
_CONTROL_MAP = object()


def _publish(adapter: _Adapter, parent=42) -> str:
    return ExperiencePublisher(adapter, object(), _CONTROL_MAP).publish(object(), _generation(), parent)


class TestAGenerationIsStagedAndAdmitted:
    def test_clear_then_stage_then_publish_through_the_control_map(self, calls) -> None:
        key = _publish(_Adapter())

        assert [(m, a) for m, a, _ in calls] == [
            ("control", "clear_experience_stage"),
            ("control", "publish_experience"),
        ]
        assert all(values == {"publication_key": key, "batch_step_id": 42} for _, _, values in calls)

    def test_the_map_resolved_is_the_one_the_host_handed_in(self, calls) -> None:
        _publish(_Adapter())

        assert calls.maps == [_CONTROL_MAP, _CONTROL_MAP]

    def test_all_five_tables_are_staged_under_one_key(self, calls) -> None:
        adapter = _Adapter()
        key = _publish(adapter)

        staged = dict(adapter.staged)
        assert list(staged) == [
            "ai_experience_generation_stage", "ai_experience_stage",
            "ai_experience_evidence_stage", "ai_experience_code_evidence_stage",
            "ai_experience_dependency_stage",
        ]
        assert [len(rows) for rows in staged.values()] == [1, 2, 2, 1, 1]
        assert all(row["publication_key"] == key for rows in staged.values() for row in rows)

    def test_every_child_row_names_its_experience_by_identity(self, calls) -> None:
        adapter = _Adapter()
        _publish(adapter)
        staged = dict(adapter.staged)

        for table in ("ai_experience_evidence_stage", "ai_experience_code_evidence_stage",
                      "ai_experience_dependency_stage"):
            for row in staged[table]:
                assert (row["installation_id"], row["application_id"]) == (1, 6)
                assert json.loads(row["subject_identity"]) == _SUBJECT
        assert sorted(row["experience_key"] for row in staged["ai_experience_evidence_stage"]) == [
            "count_rule", "sql_counts"]

    def test_a_staged_citation_names_the_cited_identity_and_a_record_names_none(self, calls) -> None:
        adapter = _Adapter()
        _publish(adapter)
        rows = {row["experience_key"]: row for row in dict(adapter.staged)["ai_experience_evidence_stage"]}

        citing = rows["count_rule"]
        assert citing["supporting_experience_key"] == "sql_counts"
        assert json.loads(citing["supporting_subject_identity"]) == _SUBJECT
        assert citing["supporting_ai_experience_id"] is None and citing["run_manifest_id"] is None

        recorded = rows["sql_counts"]
        assert recorded["run_manifest_id"] == 6173
        assert all(recorded[f"supporting_{name}"] is None for name in (
            "installation_id", "application_id", "subject_identity", "experience_key"))

    def test_provenance_is_staged_as_given(self, calls) -> None:
        adapter = _Adapter()
        _publish(adapter)
        staged = dict(adapter.staged)

        episode = staged["ai_experience_stage"][0]
        assert (episode["originating_run_manifest_id"], episode["ai_task_id"], episode["ai_contract_id"]) == (
            6173, 3, 195)
        assert staged["ai_experience_generation_stage"][0]["recording_act"] == "extraction"

    def test_two_publications_get_different_keys(self, calls) -> None:
        assert _publish(_Adapter()) != _publish(_Adapter())


class TestTheParentStepIsRequiredHere:
    def test_a_missing_parent_is_refused_before_anything_is_attempted(self, calls) -> None:
        adapter = _Adapter()
        with pytest.raises(PublicationError, match="batch step"):
            _publish(adapter, parent=None)

        assert not calls
        assert not adapter.staged


class TestAProcedureRefusalIsNotAPublication:
    def test_a_refused_admission_propagates(self, calls) -> None:
        calls.fail_on = "publish_experience"
        with pytest.raises(RuntimeError, match="does not support them"):
            _publish(_Adapter())

        assert [a for _, a, _ in calls] == ["clear_experience_stage", "publish_experience"]


class TestTheBoundary:
    def test_the_module_assembles_no_statements(self) -> None:
        """Staging goes through bulk_insert, admission through the map; no
        connection, transaction or execution of its own."""
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("execute_sql", "BEGIN", "COMMIT", "ROLLBACK", "DELETE ", "CALL ",
                          "connect(", "shared_connection", "get_procedure_map"):
            assert forbidden not in source, f"{forbidden!r} suggests Python reaching past the map"
