"""Staging is data movement; promotion is a governed operation.

These run without a database. What is asserted here is the part a plausible
refactor would break silently: that Python never assembles SQL, that every
staged row carries its publication key, and that the order of operations is the
one the procedure expects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.system_capability import publication as module
from rey_lib.system_capability import (
    CapabilityPublisher,
    PublicationError,
    StagedCapability,
    StagedEvidence,
    StagedGeneration,
    generation_from_payload,
)


def _generation(*keys: str) -> StagedGeneration:
    """A generation with one evidence item per capability."""
    return StagedGeneration(
        generated_ts="2026-09-23T12:00:00Z",
        index_indexed_ts="2026-09-23T07:22:12Z",
        index_repository_count=11,
        generator="test",
        development_recipe_id=26,
        recipe_source_hash="eed3d7f9",
        capabilities=tuple(
            StagedCapability(
                capability_key=key, label=key, statement="A capability.",
                maturity="established",
                evidence=(StagedEvidence("repository", ".*", 11, 11),),
            )
            for key in keys
        ),
    )


class _Adapter:
    """Records staged rows; refuses anything that looks like assembled SQL."""

    def __init__(self) -> None:
        self.staged: list[tuple[str, list[dict]]] = []

    def bulk_insert(self, _conn, schema, table, rows, columns):
        assert schema == "code"
        for row in rows:
            assert set(row) == set(columns), f"{table}: row/column mismatch"
        self.staged.append((table, list(rows)))
        return len(rows)

    def execute_sql(self, *_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("the publisher must not assemble SQL")


class _Calls(list):
    """Stands in for execute_mapped_routine, recording binding and values."""

    def __call__(self, _ctx, _run_log, _conn, procedure_map, routine, values):
        self.append((procedure_map, routine, dict(values)))


@pytest.fixture()
def calls(monkeypatch):
    recorded = _Calls()
    monkeypatch.setattr(module, "execute_mapped_routine", recorded)
    return recorded


class TestPromotionIsGoverned:
    """Through the map, never through a string."""

    def test_both_operations_go_through_the_map(self, calls) -> None:
        adapter = _Adapter()

        CapabilityPublisher(adapter, object()).publish(
            object(), object(), _generation("a"), parent_batch_step_id=77)

        assert [(m, r) for m, r, _ in calls] == [
            ("code", "clear_capability_stage"),
            ("code", "publish_system_capability"),
        ]

    def test_the_parent_step_is_passed_to_both(self, calls) -> None:
        """So each routine records its step beneath the caller's, not as a root."""
        CapabilityPublisher(_Adapter(), object()).publish(
            object(), object(), _generation("a"), parent_batch_step_id=77)

        assert all(v["batch_step_id"] == 77 for _m, _r, v in calls)

    def test_a_missing_parent_is_refused_HERE_not_by_the_database(self, calls) -> None:
        """Found by running it: control.f_batch_step_begin raises "a batch or
        a parent step is required" when given neither. An earlier draft of
        this design claimed a NULL parent records a root step -- it does so
        only when a batch is supplied, and with both absent it refuses.

        So publication outside a run is impossible, and the caller should
        learn that from a message naming the publication rather than from a
        control function it never invoked.
        """
        with pytest.raises(PublicationError) as raised:
            CapabilityPublisher(_Adapter(), object()).publish(
                object(), object(), _generation("a"), None)

        assert "batch step" in str(raised.value)
        assert not calls, "nothing may be attempted without a parent"

    def test_the_module_assembles_no_sql(self) -> None:
        """THE BOUNDARY, asserted against the source.

        A built CALL string would work and would be invisible to the code
        index: searching edge_vw for p_publish returns nothing precisely
        because CodeIndexDatabaseWriter passes the name as a literal.
        """
        source = Path(module.__file__).read_text(encoding="utf-8")

        for forbidden in ("execute_sql", "BEGIN", "COMMIT", "ROLLBACK",
                          "DELETE ", "INSERT INTO", "CALL "):
            assert forbidden not in source, \
                f"{forbidden!r} suggests Python reaching past the map"


class TestStagingIsScopedToOnePublication:
    """The fix for a pass promoting another pass's generation."""

    def test_every_staged_row_carries_the_key(self, calls) -> None:
        adapter = _Adapter()

        key = CapabilityPublisher(adapter, object()).publish(
            object(), object(), _generation("a", "b"), 77)

        assert adapter.staged, "nothing was staged"
        for table, rows in adapter.staged:
            for row in rows:
                assert row["publication_key"] == key, table

    def test_both_operations_use_that_same_key(self, calls) -> None:
        key = CapabilityPublisher(_Adapter(), object()).publish(
            object(), object(), _generation("a"), 77)

        assert {v["publication_key"] for _m, _r, v in calls} == {key}

    def test_two_publications_get_different_keys(self, calls) -> None:
        """Per ATTEMPT, not per run -- two attempts in one run must differ."""
        publisher = CapabilityPublisher(_Adapter(), object())

        first = publisher.publish(object(), object(), _generation("a"), 77)
        second = publisher.publish(object(), object(), _generation("a"), 77)

        assert first != second


class TestTheOrderTheProcedureExpects:
    """Clear, stage, publish. Any other order admits the wrong thing."""

    def test_staging_happens_between_the_two_calls(self, monkeypatch) -> None:
        """The regression guard for a real bug in the design.

        An earlier draft had the procedure clear on entry, which would have
        deleted the rows staged just before it and then reported success on an
        empty stage. Order is the only thing that keeps the two apart.
        """
        sequence: list[str] = []

        class _Recording(_Adapter):
            def bulk_insert(self, *a, **k):
                sequence.append("stage")
                return super().bulk_insert(*a, **k)

        def _call(_ctx, _log, _conn, _map, routine, _values):
            sequence.append(routine)

        monkeypatch.setattr(module, "execute_mapped_routine", _call)

        CapabilityPublisher(_Recording(), object()).publish(
            object(), object(), _generation("a"), 77)

        assert sequence[0] == "clear_capability_stage"
        assert sequence[-1] == "publish_system_capability"
        assert "stage" in sequence[1:-1]

    def test_all_three_tables_are_staged(self, calls) -> None:
        adapter = _Adapter()

        CapabilityPublisher(adapter, object()).publish(
            object(), object(), _generation("a"), 77)

        assert [table for table, _ in adapter.staged] == [
            "system_capability_generation_stage",
            "system_capability_stage",
            "system_capability_evidence_stage",
        ]


class TestThePayloadIsRefusedNotRepaired:
    """A payload missing a field is a pass that did not finish."""

    @staticmethod
    def _payload() -> dict:
        return {
            "generated_ts": "t", "index_indexed_ts": "t",
            "index_repository_count": 11, "generator": "ai",
            "development_recipe_id": 26, "recipe_source_hash": "h",
            "capabilities": [{
                "capability_key": "a", "label": "A", "statement": "Prose.",
                "maturity": "established",
                "evidence": [{"population_kind": "repository",
                              "reference": ".*",
                              "population_size": 11, "matched_count": 11}],
            }],
        }

    def test_a_complete_payload_builds(self) -> None:
        generation = generation_from_payload(self._payload())

        assert generation.capabilities[0].capability_key == "a"
        assert generation.capabilities[0].evidence[0].matched_count == 11

    def test_a_missing_field_is_refused(self) -> None:
        payload = self._payload()
        del payload["recipe_source_hash"]

        with pytest.raises(PublicationError) as raised:
            generation_from_payload(payload)

        assert "recipe_source_hash" in str(raised.value)

    def test_a_payload_with_no_capabilities_is_refused_by_name(self) -> None:
        """The procedure refuses it too; saying so here names the payload."""
        payload = self._payload()
        payload["capabilities"] = []

        with pytest.raises(PublicationError) as raised:
            generation_from_payload(payload)

        assert "concluded no capabilities" in str(raised.value)

    def test_nothing_is_defaulted_into_existence(self) -> None:
        """A capability missing its statement must not become an empty one."""
        payload = self._payload()
        del payload["capabilities"][0]["statement"]

        with pytest.raises(PublicationError):
            generation_from_payload(payload)
