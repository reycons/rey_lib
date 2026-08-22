"""
Tests for run lineage stamped onto every durable record.

The execution tree is readable from the log itself rather than reconstructed
from runtime state, and the lineage that carries it is generic:

    run_id -> parent_run_id -> parent_run_id -> ...

What a run is *of* lives in its subject. That is what lets a pipeline, a
workflow, an app, an FTP sync, a query or a kind nobody has written yet nest
without the lineage contract growing a field per kind.

No caller supplies these. A record cannot be written without the tree it belongs
to, which is what stops lineage being present on the records someone remembered
and absent on the rest.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs.record_enrichment import (
    RUN_DOMAIN_FIELDS,
    RUN_LINEAGE_FIELDS,
    _lineage_value,
    bind_run,
    clear_run,
    current_run,
)
from rey_lib.logs.run_log import RunLog


def _base_record(ctx, record_type: str, message: str) -> dict:
    """The enriched record the run log for this execution would write.

    The lineage lives on the owner, so the owner is built from the launch facts
    the context carries and asked what it stamps. There is no second builder.
    """
    lineage = {}
    for field in (*RUN_LINEAGE_FIELDS, *RUN_DOMAIN_FIELDS):
        found = _lineage_value(ctx, field)
        if found:
            lineage[field] = found
    run_log = RunLog(
        app=str(getattr(ctx, "owner_app_name", "") or getattr(ctx, "app_name", "")
                or getattr(ctx, "name", "") or ""),
        run_id=str(getattr(ctx, "run_id", "")),
        run_timestamp=str(getattr(ctx, "run_timestamp", "")),
        workflow=getattr(ctx, "workflow_name", None),
        pipeline=getattr(ctx, "pipeline_name", None),
        lineage=lineage,
    )
    return run_log._record(record_type, message, {})


def _ctx(**fields: object) -> SimpleNamespace:
    """A context carrying the standard identity every record already reads."""
    base = {"run_id": "R102", "run_timestamp": "20260821_101500", "app_name": "rey_loader"}
    base.update(fields)
    return SimpleNamespace(**base)


class TestLineageIsGeneric:
    """The tree is run_id and parent_run_id. The kind lives in the subject."""

    def test_the_contract_names_no_execution_kind(self) -> None:
        # The point of the shape: adding FTP, SQL, AI or an external app never
        # adds a field here.
        assert RUN_LINEAGE_FIELDS == (
            "parent_run_id", "subject_type", "subject_id", "subject_name",
        )

    def test_a_leaf_record_names_its_parent_and_its_subject(self) -> None:
        record = _base_record(
            _ctx(
                run_id="R102",
                parent_run_id="R101",
                subject_type="app",
                subject_id="rey_loader",
                subject_name="Rey Loader",
            ),
            "RUN_START",
            "starting",
        )

        assert record["run_id"] == "R102"
        assert record["parent_run_id"] == "R101"
        assert record["subject_type"] == "app"
        assert record["subject_id"] == "rey_loader"

    def test_any_leaf_walks_back_to_the_root(self) -> None:
        # R100 pipeline -> R101 workflow -> R102 app, and R104 directly under
        # the pipeline. Reconstructed from records alone.
        tree = {
            "R100": _base_record(_ctx(run_id="R100", subject_type="pipeline"), "RUN_START", ""),
            "R101": _base_record(
                _ctx(run_id="R101", parent_run_id="R100", subject_type="workflow"),
                "RUN_START", "",
            ),
            "R102": _base_record(
                _ctx(run_id="R102", parent_run_id="R101", subject_type="app"),
                "RUN_START", "",
            ),
            "R104": _base_record(
                _ctx(run_id="R104", parent_run_id="R100", subject_type="app"),
                "RUN_START", "",
            ),
        }

        def root_of(run: str) -> str:
            while tree[run].get("parent_run_id"):
                run = tree[run]["parent_run_id"]
            return run

        assert root_of("R102") == "R100"
        assert root_of("R104") == "R100"
        assert root_of("R100") == "R100"

    def test_two_kinds_nest_through_the_same_two_fields(self) -> None:
        # An FTP sync beneath a workflow, and a query beneath an app, described
        # by exactly the fields a pipeline and a workflow use.
        ftp = _base_record(
            _ctx(run_id="R201", parent_run_id="R101", subject_type="ftp_sync",
                 subject_id="feeds_nightly"),
            "RUN_START", "",
        )
        query = _base_record(
            _ctx(run_id="R202", parent_run_id="R102", subject_type="sql",
                 subject_id="orders_reconcile"),
            "RUN_START", "",
        )

        assert ftp["parent_run_id"] == "R101"
        assert query["parent_run_id"] == "R102"
        for record in (ftp, query):
            assert set(RUN_LINEAGE_FIELDS) & set(record)


class TestLineageIsNotInvented:
    """Absent is absent. Nothing is defaulted, guessed or forced."""

    def test_a_context_with_no_lineage_carries_none(self) -> None:
        record = _base_record(_ctx(), "RUN_START", "")

        for field in RUN_LINEAGE_FIELDS:
            assert field not in record

    def test_a_root_run_has_no_parent(self) -> None:
        # A run nobody spawned says so by omission rather than by pointing at
        # itself, so "has a parent" stays a question with a real answer.
        record = _base_record(_ctx(run_id="R100", subject_type="pipeline"), "RUN_START", "")

        assert record["run_id"] == "R100"
        assert "parent_run_id" not in record

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_lineage_value_is_left_off(self, empty: object) -> None:
        record = _base_record(_ctx(parent_run_id=empty), "RUN_START", "")

        assert "parent_run_id" not in record


class TestDomainMetadataIsNotLineage:
    """The pipeline and workflow fields are kept, and kept apart."""

    def test_domain_fields_are_classified_separately(self) -> None:
        assert not set(RUN_LINEAGE_FIELDS) & set(RUN_DOMAIN_FIELDS)
        for field in ("pipeline_run_id", "workflow_run_id", "pipeline_id", "workflow_id"):
            assert field in RUN_DOMAIN_FIELDS

    def test_names_are_kept_alongside_ids_not_replaced_by_them(self) -> None:
        record = _base_record(
            _ctx(pipeline_id="P42", workflow_id="W7",
                 pipeline_name="amalgamate", workflow_name="load_only"),
            "RUN_START", "",
        )

        assert record["pipeline_name"] == "amalgamate"
        assert record["workflow_name"] == "load_only"
        assert record["pipeline_id"] == "P42"

    def test_the_enclosing_pipeline_run_is_read_from_runtime(self) -> None:
        # The one enclosing run identity the estate already computes:
        # pipeline_coordinator stamps it on ctx.runtime, and until now nothing
        # recorded it -- so two runs of one pipeline were indistinguishable to
        # the readers that group by pipeline_name.
        record = _base_record(
            _ctx(runtime=SimpleNamespace(pipeline_run_id="R100")), "STEP_START", "",
        )

        assert record["pipeline_run_id"] == "R100"

    def test_a_runtime_mapping_answers_as_well_as_an_object(self) -> None:
        record = _base_record(_ctx(runtime={"pipeline_run_id": "R100"}), "STEP_END", "")

        assert record["pipeline_run_id"] == "R100"

    def test_two_runs_of_one_pipeline_are_distinguishable(self) -> None:
        first = _base_record(
            _ctx(run_id="R102", pipeline_name="amalgamate",
                 runtime=SimpleNamespace(pipeline_run_id="R100")),
            "RUN_START", "",
        )
        second = _base_record(
            _ctx(run_id="R202", pipeline_name="amalgamate",
                 runtime=SimpleNamespace(pipeline_run_id="R200")),
            "RUN_START", "",
        )

        assert first["pipeline_name"] == second["pipeline_name"]
        assert first["pipeline_run_id"] != second["pipeline_run_id"]


class TestAmbientRecordsCarryTheSameLineage:
    """A file operation written through the bound run is enriched identically."""

    def test_bind_run_captures_lineage_for_ambient_records(self, tmp_path: Path) -> None:
        log = tmp_path / "amalgamate.20260821_101500.jsonl"
        log.write_text("", encoding="utf-8")
        import json

        from tests.conftest import make_run_log

        from rey_lib.logs.file_records import record_file_operation

        ambient = make_run_log(tmp_path, path=str(log), app="rey_loader",
                               run_id="R102", run_timestamp="20260821_101500")
        ambient.bind_lineage(parent_run_id="R101", subject_type="app",
                             pipeline_run_id="R100")
        bind_run(ambient)
        try:
            bound = current_run()
            assert bound is not None and bound["run_id"] == "R102"

            # The bound run log writes the ambient record, so it carries the same
            # lineage as anything else that run log writes.
            record_file_operation("read", source_path=str(tmp_path / "x"))
            record = [json.loads(line) for line in
                      log.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
            assert record["record_type"] == "FILE_OPERATION"
            assert record["parent_run_id"] == "R101"
            assert record["subject_type"] == "app"
            assert record["pipeline_run_id"] == "R100"
        finally:
            clear_run()
