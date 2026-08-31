"""Manifest-authoritative, execution-surface-neutral log-run rollback."""

from __future__ import annotations

import json
from pathlib import Path

from contextlib import contextmanager
from tests.conftest import make_db_run_log, start_test_run
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.files import (
    LogRunRollbackError,
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
    SourceFileMutationEvidenceResult,
    log_source_file_mutation,
    register_file_compensation,
    serialize_source_file_mutation,
    unregister_file_compensation,
)
from rey_lib.logs import log_file_manifest_record
from rey_lib.logs.file_manifest import FileManifestError, FileManifestSession


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def resolve(self, name: str) -> Path:
        return {"file_manifest": self.manifest}[name]


def _ctx(tmp_path: Path) -> SimpleNamespace:
    # The governed manifest is a control table, so a context that governs files
    # exposes the Control it reaches them through.
    from tests.conftest import ControlDouble

    return SimpleNamespace(
        paths=_Paths(tmp_path / "file_manifest.jsonl"),
        installation="test",
        config_root="test",
        run_log_path=str(tmp_path / "run.jsonl"),
        shared_control=ControlDouble(),
    )


def _run_log(tmp_path: Path, name: str = "run.jsonl") -> Path:
    path = tmp_path / name
    path.write_text('{"record_type":"RUN_START"}\n', encoding="utf-8")
    return path


def _governed_file(ctx: SimpleNamespace, path: str = "/inbox/a.csv") -> int:
    """Inventory one governed file and return its identity.

    A mutation is an event in a file's history, so it names the file it
    mutates. There is no standalone mutation record any more.
    """
    return ctx.shared_control.inventory_file(
        path=path, file_name="a.csv", base_name="a",
        file_extension="csv", checksum_sha256="abc", size_bytes=1,
        source_name="test",
    )


def _append_mutation(
    ctx: SimpleNamespace,
    *,
    action: str,
    file_id: int | None = None,
    source_path: str = "",
    destination_path: str = "",
    recovery_path: str = "",
    previous_version_path: str = "",
    status: str = "success",
) -> int:
    return log_file_manifest_record(
        ctx,
        serialize_source_file_mutation(
            action=action,
            status=status,
            file_id=file_id if file_id is not None else _governed_file(ctx),
            source_path=source_path,
            destination_path=destination_path,
            recovery_path=recovery_path,
            previous_version_path=previous_version_path,
            run_log_id=1,
            application_name="test",
        ),
    )


def _rows(ctx: SimpleNamespace) -> list[dict]:
    """The mutations actually written, as the control database holds them.

    The manifest is a table, so what a writer produced is read back from the
    rows it created rather than from a JSONL file it no longer writes.
    """
    return list(ctx.shared_control.mutations)


@contextmanager
def _bound(run_log: Any):
    """Bind the run a governed mutation is recorded against.

    Production binds a run around the work it owns, and the mutation writer
    resolves its owner from that binding rather than being handed one.
    """
    from rey_lib.logs import bind_run, clear_run

    bind_run(run_log)
    try:
        yield run_log
    finally:
        clear_run()


def test_mutation_evidence_failure_before_run_log_commit_is_structured(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    with _bound(run_log), patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=None
    ), patch(
        "rey_lib.files.log_run_rollback.log_file_manifest_record"
    ) as manifest_append:
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(
                ctx, action="move", status="success")

    error = raised.value
    assert error.phase is SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED
    assert error.run_log_committed is False
    assert error.run_log_id is None
    assert error.manifest_record_id is None
    assert error.complete_evidence_acknowledged is False
    assert isinstance(error, LogRunRollbackError)
    assert str(error) == (
        "Source-file mutation run-log evidence did not commit; the file manifest "
        "was not modified."
    )
    manifest_append.assert_not_called()


def test_mutation_evidence_filename_failure_preserves_committed_record_id(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    del ctx.run_log_path
    with _bound(run_log), patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=17
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(
                ctx, action="move", status="success")

    error = raised.value
    assert error.phase is (
        SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
    )
    assert error.run_log_committed is True
    assert error.run_log_id == 17
    assert error.complete_evidence_acknowledged is False


@pytest.mark.parametrize(
    "failure",
    [
        LogRunRollbackError("serialization failed"),
        TypeError("copy failed"),
        ValueError("copy failed"),
    ],
)
def test_mutation_serialization_failure_after_run_log_commit_is_structured(
    tmp_path: Path,
    failure: Exception,
) -> None:
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    with _bound(run_log), patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=19
    ), patch(
        "rey_lib.files.log_run_rollback.serialize_source_file_mutation",
        side_effect=failure,
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(
                ctx, action="move", status="success")

    error = raised.value
    assert error.run_log_committed is True
    assert error.run_log_id == 19
    assert error.__cause__ is failure


def test_manifest_append_failure_reports_post_run_log_phase_without_a_row(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    with _bound(run_log), patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=23
    ), patch(
        "rey_lib.files.primitive_file_io.append_jsonl",
        side_effect=OSError("append blocked"),
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(
                ctx, action="move", status="success")

    error = raised.value
    assert error.phase is (
        SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
    )
    assert error.run_log_committed is True
    assert error.run_log_id == 23
    assert error.complete_evidence_acknowledged is False
    assert error.manifest_record_id is None
    assert not ctx.paths.manifest.exists()


def test_mutation_evidence_phase_owns_commit_state() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        SourceFileMutationEvidenceError(
            "failed",
            phase=SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED,
            run_log_id=1,
        )
    with pytest.raises(ValueError, match="positive"):
        SourceFileMutationEvidenceError(
            "failed",
            phase=(
                SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
            ),
            run_log_id=None,
        )


def test_appended_mutation_carries_no_legacy_field_names(tmp_path: Path) -> None:
    """Every field the canonical layout groups is gone from the record root."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    start_test_run(ctx)

    with _bound(run_log):
        log_source_file_mutation(
            ctx, action="move",
            status="success",
            file_id=_governed_file(ctx),
        source_path=tmp_path / "in" / "a.xlsx",
        destination_path=tmp_path / "proc" / "a.xlsx",
        recovery_path=tmp_path / "trash" / "a.xlsx",
        application_name="test",
    )

    manifest_record = _rows(ctx)[0]
    for legacy in (
        "schema_version",
        "source_path",
        "destination_path",
        "recovery_path",
        "previous_version_path",
        "application_name",
    ):
        assert legacy not in manifest_record, legacy


@pytest.mark.parametrize(
    "reserved",
    ["record_id", "record_type", "producer", "file", "evidence", "rollback",
     "result", "recorded_at", "fields"],
)
def test_caller_cannot_inject_a_canonical_root_field(
    tmp_path: Path,
    reserved: str,
) -> None:
    """The boundary takes governed values only; there is no injection surface."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        log_source_file_mutation(
            _ctx(tmp_path),
            action="create",
            status="success",
            destination_path=tmp_path / "created.csv",
            **{reserved: {}},
        )


def test_run_log_fields_never_reach_the_manifest_record(tmp_path: Path) -> None:
    """Run-log enrichment is evidence, not a way into the governed record."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=ctx.run_log_path)
    start_test_run(ctx)

    with _bound(run_log):
        log_source_file_mutation(
            ctx, action="create",
            status="success",
            file_id=_governed_file(ctx),
            destination_path=tmp_path / "created.csv",
        application_name="test",
        run_log_fields={"workbook_name": "book.xlsx", "extraction_kind": "sheet"},
    )

    manifest_record = _rows(ctx)[0]
    assert "workbook_name" not in manifest_record
    assert "extraction_kind" not in manifest_record
    run_rows = [
        json.loads(line)
        for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
    ]
    assert run_rows[0]["workbook_name"] == "book.xlsx"


def test_approved_conversion_and_result_inputs_build_canonical_sections(
    tmp_path: Path,
) -> None:
    record = serialize_source_file_mutation(
        action="create",
        status="success",
        destination_path="/out/a.csv",
        run_log_id=1,
        conversion={
            "operator": "some_other_producer",
            "name": "wolff_popper",
            "source": {"sheet_name": "Sheet1", "table_name": "Table1"},
        },
        reason_code="converted",
        reason="workbook converted",
    )

    # The payload is written exactly as the producer supplied it, including a
    # field this framework has never heard of.
    assert record["conversion"] == {
        "operator": "some_other_producer",
        "name": "wolff_popper",
        "source": {"sheet_name": "Sheet1", "table_name": "Table1"},
    }
    assert record["result"] == {
        "reason_code": "converted",
        "reason": "workbook converted",
    }


def test_supplied_classification_is_serialized_unchanged() -> None:
    classification = {
        "type": "MiXeD",
        "source_field": None,
        "values": {"Unfamiliar": "Kept", "optional": None},
        "future": {"items": [2, 1]},
    }

    record = serialize_source_file_mutation(
        action="create",
        status="success",
        destination_path="/out/a.csv",
        run_log_id=1,
        classification=classification,
    )

    assert record["classification"] == classification


def test_absent_conversion_and_result_sections_are_omitted() -> None:
    record = serialize_source_file_mutation(
        action="move",
        status="success",
        source_path="/in/a.xlsx",
        destination_path="/proc/a.xlsx",
        run_log_id=1,
    )

    assert "conversion" not in record
    assert "result" not in record
    assert "file_id" not in record
    assert "classification" not in record


@pytest.mark.parametrize(
    ("action", "source_path", "destination_path", "expected_file"),
    [
        ("move", "/in/a.xlsx", "/proc/a.xlsx",
         {"path": "/proc/a.xlsx", "original_path": "/in/a.xlsx",
          "file_name": "a.xlsx", "base_name": "a", "file_extension": "xlsx"}),
        ("create", "", "/out/a.csv",
         {"path": "/out/a.csv", "file_name": "a.csv", "base_name": "a",
          "file_extension": "csv"}),
        ("delete", "/in/a.xlsx", "",
         {"original_path": "/in/a.xlsx", "file_name": "a.xlsx",
          "base_name": "a", "file_extension": "xlsx"}),
        ("replace", "/in/a.xlsx", "/out/a.csv",
         {"path": "/out/a.csv", "original_path": "/in/a.xlsx",
          "file_name": "a.csv", "base_name": "a", "file_extension": "csv"}),
    ],
)
def test_serialized_file_object_follows_the_lifecycle_action(
    action: str,
    source_path: str,
    destination_path: str,
    expected_file: dict[str, str],
) -> None:
    """A location the action never had is omitted, not recorded empty."""
    record = serialize_source_file_mutation(
        action=action,
        status="success",
        source_path=source_path,
        destination_path=destination_path,
        run_log_id=1,
    )

    assert record["file"] == expected_file
    assert "rollback" not in record


def test_compensation_metadata_is_grouped_and_omitted_when_absent() -> None:
    recorded = serialize_source_file_mutation(
        action="delete",
        status="success",
        source_path="/in/a.xlsx",
        recovery_path="/trash/a.xlsx",
        run_log_id=1,
    )
    assert recorded["rollback"] == {"recovery_path": "/trash/a.xlsx"}

    replaced = serialize_source_file_mutation(
        action="replace",
        status="success",
        source_path="/in/a.xlsx",
        destination_path="/out/a.csv",
        previous_version_path="/bak/a.csv",
        run_log_id=1,
    )
    assert replaced["rollback"] == {"previous_version_path": "/bak/a.csv"}


@pytest.mark.parametrize(
    "surface_field",
    ["pipeline_name", "workflow_name", "application_name"],
)
def test_execution_surface_fields_cannot_reach_the_manifest(
    tmp_path: Path,
    surface_field: str,
) -> None:
    """Run type never participates: such a field is refused, not stored."""
    record = serialize_source_file_mutation(
        action="create",
        status="success",
        destination_path="/created/output",
        run_log_id=1,
    )
    record[surface_field] = "daily"

    with pytest.raises(FileManifestError, match="unknown root field"):
        log_file_manifest_record(_ctx(tmp_path), record)


def _append_state_record(
    ctx: SimpleNamespace,
    *,
    record_type: str,
    run_log_file: str = "run.jsonl",
) -> int:
    """Append one inventory or classification record, which carries no action."""
    return log_file_manifest_record(
        ctx,
        {
            "recorded_at": "2026-08-05T00:00:00.000+00:00",
            "record_type": record_type,
            "status": "classified",
            "file_id": "governed-1",
            "evidence": {"run_log_file": run_log_file, "run_log_id": 1},
            "file": {"path": "/inbox/a.csv", "file_name": "a.csv"},
            "producer": {"application": "test"},
        },
    )


