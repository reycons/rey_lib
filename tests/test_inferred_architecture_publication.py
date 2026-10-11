"""Staging is data movement; admission is a governed operation.

These run without a database, with the capability publisher's fakes: an
adapter that records staged rows and refuses assembled SQL, and a recorder in
place of the procedure map. What they assert is the part a plausible refactor
would break silently -- the payload contract, the parent step, the publication
key on every row, and the order the procedure expects. What the procedure
itself refuses (cycles, memberships naming nothing, zero-only evidence) is
validated against the live database in a rolled-back transaction, not here.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from rey_lib.inferred_architecture import publication as module
from rey_lib.inferred_architecture import (
    ArchitecturePublisher,
    PublicationError,
    generation_from_payload,
)


def _payload() -> dict:
    """A complete document in the contract's shape: two roots, one child."""
    evidence = [{"population_kind": "package", "reference": "^rey_lib/rey_lib/db$",
                 "population_size": 228, "matched_count": 1}]
    return {
        "generated_ts": "2026-10-11T12:00:00Z",
        "index_indexed_ts": "2026-10-11T07:00:00Z",
        "index_repository_count": 11,
        "generator": "test",
        "ai_contract_id": 190,
        "ai_task_id": 182,
        "areas": [
            {"area_key": "data_access", "parent_area_key": None, "label": "Data access",
             "statement": "Database objects behind one adapter.", "sort_order": 1,
             "memberships": [{"repository_key": "rey_lib", "identity": "rey_lib/db", "member_kind": "package"}],
             "evidence": copy.deepcopy(evidence)},
            {"area_key": "database_objects", "parent_area_key": "data_access", "label": "Database objects",
             "statement": "One class per object kind.",
             "memberships": [
                 {"repository_key": "rey_lib", "identity": "rey_lib.db.database_objects.DatabaseTable",
                  "member_kind": "symbol"},
                 {"repository_key": "rey_lib", "identity": "rey_lib.errors.error_utils.DatabaseError",
                  "member_kind": "symbol"}],
             "evidence": copy.deepcopy(evidence)},
            {"area_key": "logging", "parent_area_key": None, "label": "Logging",
             "statement": "Application log writing.",
             "memberships": [
                 {"repository_key": "rey_lib", "identity": "rey_lib.logs.app_logger", "member_kind": "module"},
                 {"repository_key": "rey_lib", "identity": "rey_lib.errors.error_utils.DatabaseError",
                  "member_kind": "symbol"}],
             "evidence": copy.deepcopy(evidence)},
        ],
    }


class _Adapter:
    """Records staged rows; fails the test on anything that looks like assembled SQL."""

    def __init__(self) -> None:
        self.staged: list[tuple[str, list[dict]]] = []

    def bulk_insert(self, _conn, schema, table, rows, columns):
        assert schema == "code_stage"
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
            def resolve(cls, _conn, _map_cfg, map_name, action, adapter=None):
                return cls(map_name, action)

            def call(self, _conn, values, _run_log, run_ctx=None):
                calls.append((self.map_name, self.action, dict(values)))
                if self.action == calls.fail_on:
                    raise RuntimeError("Publication refused: staged areas in a parent cycle")
                return {"outputs": {}, "rows": None}

        monkeypatch.setattr(module, "ProcedureMap", _ProcedureMap)
        monkeypatch.setattr(module, "get_procedure_map", lambda _ctx, name: object())


@pytest.fixture()
def calls(monkeypatch):
    recorded = _Calls()
    recorded.install(monkeypatch)
    return recorded


def _publish(adapter: _Adapter, parent=42) -> str:
    return ArchitecturePublisher(adapter, object()).publish(
        object(), object(), generation_from_payload(_payload()), parent)


class TestAValidGenerationIsStagedAndAdmitted:
    def test_clear_then_stage_then_publish_through_the_map(self, calls) -> None:
        adapter = _Adapter()
        key = _publish(adapter)

        assert [(m, a) for m, a, _ in calls] == [
            ("code", "clear_inferred_architecture_stage"),
            ("code", "publish_inferred_architecture"),
        ]
        assert all(values == {"publication_key": key, "batch_step_id": 42} for _, _, values in calls)

    def test_all_four_tables_are_staged_under_one_key(self, calls) -> None:
        adapter = _Adapter()
        key = _publish(adapter)

        staged = dict(adapter.staged)
        assert list(staged) == [
            "inferred_architecture_generation_stage", "inferred_architecture_area_stage",
            "inferred_architecture_membership_stage", "inferred_architecture_evidence_stage",
        ]
        assert len(staged["inferred_architecture_area_stage"]) == 3
        assert len(staged["inferred_architecture_membership_stage"]) == 5
        assert len(staged["inferred_architecture_evidence_stage"]) == 3
        assert all(row["publication_key"] == key for rows in staged.values() for row in rows)

    def test_the_tree_and_shared_code_are_staged_as_inferred(self, calls) -> None:
        adapter = _Adapter()
        _publish(adapter)
        staged = dict(adapter.staged)

        parents = {row["area_key"]: row["parent_area_key"] for row in staged["inferred_architecture_area_stage"]}
        assert parents == {"data_access": None, "database_objects": "data_access", "logging": None}
        shared = [row["area_key"] for row in staged["inferred_architecture_membership_stage"]
                  if row["identity"].endswith("DatabaseError")]
        assert sorted(shared) == ["database_objects", "logging"]

    def test_two_publications_get_different_keys(self, calls) -> None:
        assert _publish(_Adapter()) != _publish(_Adapter())


class TestTheParentStepIsRequiredHere:
    def test_a_missing_parent_is_refused_before_anything_is_attempted(self, calls) -> None:
        """The routines accept a NULL parent at the signature; the batch
        machinery refuses one. So the refusal is made here, by name."""
        adapter = _Adapter()
        with pytest.raises(PublicationError, match="batch step"):
            _publish(adapter, parent=None)

        assert not calls
        assert not adapter.staged


class TestAProcedureFailureIsNotAPublication:
    def test_a_refused_admission_propagates(self, calls) -> None:
        """Nothing is caught: a rejected generation must not look like a
        published one. The procedure admits whole or raises, so the
        database holds no partial publication either."""
        calls.fail_on = "publish_inferred_architecture"
        with pytest.raises(RuntimeError, match="Publication refused"):
            _publish(_Adapter())

        assert [a for _, a, _ in calls] == ["clear_inferred_architecture_stage", "publish_inferred_architecture"]


class TestAMalformedPayloadIsRefusedBeforeStaging:
    @pytest.mark.parametrize("mutate, message", [
        (lambda p: p.pop("ai_task_id"), "ai_task_id is absent"),
        (lambda p: p.update(areas=[]), "inferred no areas"),
        (lambda p: p["areas"][0].pop("parent_area_key"), "parent_area_key is absent"),
        (lambda p: p["areas"][0].update(memberships=[]), "no memberships"),
        (lambda p: p["areas"][0].update(evidence=[]), "no evidence"),
        (lambda p: p["areas"][0]["memberships"][0].update(member_kind="file"), "member_kind 'file'"),
        (lambda p: p["areas"][0]["evidence"][0].update(population_kind="inferred_architecture"),
         "never evidence"),
        (lambda p: p["areas"][0]["evidence"][0].update(matched_count=300), "M of N"),
        (lambda p: p["areas"][0].update(statement="Holds 18 modules."), "digit"),
        (lambda p: p["areas"][1].update(area_key="data_access"), "repeats area_key"),
        (lambda p: p["areas"][0].update(label="  "), "label is missing or blank"),
    ])
    def test_refused_by_name(self, calls, mutate, message) -> None:
        payload = _payload()
        mutate(payload)
        adapter = _Adapter()

        with pytest.raises(PublicationError, match=message):
            ArchitecturePublisher(adapter, object()).publish(
                object(), object(), generation_from_payload(payload), 42)

        assert not adapter.staged
        assert not calls

    def test_a_complete_payload_builds(self) -> None:
        generation = generation_from_payload(_payload())
        assert [one.area_key for one in generation.areas] == ["data_access", "database_objects", "logging"]
        assert generation.ai_contract_id == 190 and generation.ai_task_id == 182

    def test_nothing_is_defaulted_into_existence(self) -> None:
        """Only sort_order may default; a root says null, it is not assumed."""
        generation = generation_from_payload(_payload())
        assert generation.areas[1].sort_order == 0
        assert generation.areas[0].parent_area_key is None


class TestTheBoundary:
    def test_the_module_assembles_no_statements(self) -> None:
        """Staging goes through bulk_insert, admission through the map. The
        fake adapter already fails on execute_sql; this also rejects
        transaction control and procedure or delete text in the source."""
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("execute_sql", "BEGIN", "COMMIT", "ROLLBACK", "DELETE ", "CALL "):
            assert forbidden not in source, f"{forbidden!r} suggests Python reaching past the map"
