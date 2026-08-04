"""Manifest-authoritative, execution-surface-neutral log-run rollback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.files import (
    LogRunRollbackError,
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
    SourceFileMutationEvidenceResult,
    log_source_file_mutation,
    preview_log_run_rollback,
    register_file_compensation,
    rollback_log_run,
    serialize_source_file_mutation,
    serialize_source_file_rollback,
    unregister_file_compensation,
)
from rey_lib.logs import log_file_manifest_record, resolve_run_identity
from rey_lib.logs.file_manifest import FileManifestError, FileManifestSession


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def resolve(self, name: str) -> Path:
        assert name == "file_manifest"
        return self.manifest


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        paths=_Paths(tmp_path / "file_manifest.jsonl"),
        installation="test",
        config_root="test",
        run_log_path=str(tmp_path / "run.jsonl"),
    )


def _run_log(tmp_path: Path, name: str = "run.jsonl") -> Path:
    path = tmp_path / name
    path.write_text('{"record_type":"RUN_START"}\n', encoding="utf-8")
    return path


def _append_mutation(
    ctx: SimpleNamespace,
    *,
    action: str,
    run_log_file: str = "run.jsonl",
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
            source_path=source_path,
            destination_path=destination_path,
            recovery_path=recovery_path,
            previous_version_path=previous_version_path,
            run_log_file=run_log_file,
            run_log_record_id=1,
            application_name="test",
        ),
    )


def _rows(ctx: SimpleNamespace) -> list[dict]:
    path = ctx.paths.manifest
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_shared_mutation_boundary_commits_evidence_before_manifest(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    resolve_run_identity(ctx)
    classification = {
        "type": "file_name_regex",
        "values": {"Unfamiliar": "Kept"},
    }

    manifest_record_id = log_source_file_mutation(
        ctx,
        action="create",
        status="success",
        destination_path=tmp_path / "created.csv",
        application_name="test",
        file_id="file-a",
        classification=classification,
    )

    run_rows = [
        json.loads(line)
        for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
    ]
    manifest_record = _rows(ctx)[0]
    assert manifest_record_id == 1
    assert isinstance(manifest_record_id, int)
    assert isinstance(manifest_record_id, SourceFileMutationEvidenceResult)
    assert manifest_record_id.manifest_record_id == 1
    assert manifest_record_id.run_log_record_id == 1
    assert manifest_record_id.run_log_file == "run.jsonl"
    assert run_rows[0]["record_type"] == "SOURCE_FILE_MUTATION"
    assert manifest_record["record_type"] == "source_file_mutation"
    assert manifest_record["file_id"] == "file-a"
    assert manifest_record["classification"] == classification
    assert manifest_record["producer"] == {"application": "test"}
    assert manifest_record["file"] == {
        "path": str(tmp_path / "created.csv"),
        "file_name": "created.csv",
        "base_name": "created",
        "file_extension": "csv",
    }
    assert manifest_record["evidence"] == {
        "run_log_file": "run.jsonl",
        "run_log_record_id": run_rows[0]["record_id"],
    }


def test_mutation_evidence_failure_before_run_log_commit_is_structured(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    with patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=None
    ), patch(
        "rey_lib.files.log_run_rollback.log_file_manifest_record"
    ) as manifest_append:
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(ctx, action="move", status="success")

    error = raised.value
    assert error.phase is SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED
    assert error.run_log_committed is False
    assert error.run_log_record_id is None
    assert error.run_log_file is None
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
    del ctx.run_log_path
    with patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=17
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(ctx, action="move", status="success")

    error = raised.value
    assert error.phase is (
        SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
    )
    assert error.run_log_committed is True
    assert error.run_log_record_id == 17
    assert error.run_log_file is None
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
    with patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=19
    ), patch(
        "rey_lib.files.log_run_rollback.serialize_source_file_mutation",
        side_effect=failure,
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(ctx, action="move", status="success")

    error = raised.value
    assert error.run_log_committed is True
    assert error.run_log_record_id == 19
    assert error.run_log_file == "run.jsonl"
    assert error.__cause__ is failure


def test_manifest_append_failure_reports_post_run_log_phase_without_a_row(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    with patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=23
    ), patch(
        "rey_lib.files.primitive_file_io.append_jsonl",
        side_effect=OSError("append blocked"),
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(ctx, action="move", status="success")

    error = raised.value
    assert error.phase is (
        SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
    )
    assert error.run_log_committed is True
    assert error.run_log_record_id == 23
    assert error.run_log_file == "run.jsonl"
    assert error.complete_evidence_acknowledged is False
    assert error.manifest_record_id is None
    assert not ctx.paths.manifest.exists()


def test_sequencing_state_failure_preserves_appended_manifest_row(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    with patch(
        "rey_lib.files.log_run_rollback.log_run_record", return_value=29
    ), patch(
        "rey_lib.logs.file_manifest._commit_state",
        side_effect=FileManifestError("sequencing state failed after append"),
    ):
        with pytest.raises(SourceFileMutationEvidenceError) as raised:
            log_source_file_mutation(
                ctx,
                action="move",
                status="success",
                source_path=tmp_path / "inbox" / "source.csv",
                destination_path=tmp_path / "processing" / "source.csv",
                file_id="file-29",
            )

    error = raised.value
    assert error.phase is (
        SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
    )
    assert error.run_log_committed is True
    assert error.run_log_record_id == 29
    assert error.run_log_file == "run.jsonl"
    assert error.manifest_record_id is None
    assert error.complete_evidence_acknowledged is False
    assert not hasattr(error, "manifest_committed")
    rows = _rows(ctx)
    assert len(rows) == 1
    assert rows[0]["record_id"] == 1
    assert rows[0]["file_id"] == "file-29"
    assert rows[0]["evidence"] == {
        "run_log_file": "run.jsonl",
        "run_log_record_id": 29,
    }


def test_mutation_evidence_phase_owns_commit_state() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        SourceFileMutationEvidenceError(
            "failed",
            phase=SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED,
            run_log_file="run.jsonl",
            run_log_record_id=1,
        )
    with pytest.raises(ValueError, match="positive"):
        SourceFileMutationEvidenceError(
            "failed",
            phase=(
                SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
            ),
            run_log_file=None,
            run_log_record_id=None,
        )


def test_appended_mutation_carries_no_legacy_field_names(tmp_path: Path) -> None:
    """Every field the canonical layout groups is gone from the record root."""
    ctx = _ctx(tmp_path)
    resolve_run_identity(ctx)

    log_source_file_mutation(
        ctx,
        action="move",
        status="success",
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
    resolve_run_identity(ctx)

    log_source_file_mutation(
        ctx,
        action="create",
        status="success",
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
        run_log_file="run.jsonl",
        run_log_record_id=1,
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
        run_log_file="run.jsonl",
        run_log_record_id=1,
        classification=classification,
    )

    assert record["classification"] == classification


def test_absent_conversion_and_result_sections_are_omitted() -> None:
    record = serialize_source_file_mutation(
        action="move",
        status="success",
        source_path="/in/a.xlsx",
        destination_path="/proc/a.xlsx",
        run_log_file="run.jsonl",
        run_log_record_id=1,
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
        run_log_file="run.jsonl",
        run_log_record_id=1,
    )

    assert record["file"] == expected_file
    assert "rollback" not in record


def test_compensation_metadata_is_grouped_and_omitted_when_absent() -> None:
    recorded = serialize_source_file_mutation(
        action="delete",
        status="success",
        source_path="/in/a.xlsx",
        recovery_path="/trash/a.xlsx",
        run_log_file="run.jsonl",
        run_log_record_id=1,
    )
    assert recorded["rollback"] == {"recovery_path": "/trash/a.xlsx"}

    replaced = serialize_source_file_mutation(
        action="replace",
        status="success",
        source_path="/in/a.xlsx",
        destination_path="/out/a.csv",
        previous_version_path="/bak/a.csv",
        run_log_file="run.jsonl",
        run_log_record_id=1,
    )
    assert replaced["rollback"] == {"previous_version_path": "/bak/a.csv"}


def test_preview_selects_exact_run_and_reverses_manifest_order(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    _append_mutation(
        ctx,
        action="move",
        source_path="/original/one",
        destination_path="/current/one",
    )
    second = _append_mutation(
        ctx,
        action="create",
        destination_path="/created/two",
    )
    _append_mutation(
        ctx,
        action="create",
        run_log_file="other.jsonl",
        destination_path="/created/other",
    )
    first = 1

    plan = preview_log_run_rollback(ctx, "run.jsonl")

    assert [
        item["original_manifest_record_id"] for item in plan["candidates"]
    ] == [second, first]


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
        run_log_file="run.jsonl",
        run_log_record_id=1,
    )
    record[surface_field] = "daily"

    with pytest.raises(FileManifestError, match="unknown root field"):
        log_file_manifest_record(_ctx(tmp_path), record)


def test_move_and_create_are_compensated_with_attempt_and_final_evidence(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    original = tmp_path / "inbox" / "input.csv"
    current = tmp_path / "processed" / "input.csv"
    current.parent.mkdir()
    current.write_text("input", encoding="utf-8")
    created = tmp_path / "output.csv"
    created.write_text("output", encoding="utf-8")
    move_id = _append_mutation(
        ctx,
        action="move",
        source_path=str(original),
        destination_path=str(current),
    )
    create_id = _append_mutation(
        ctx,
        action="create",
        destination_path=str(created),
    )

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert result["status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["appended_rollback_evidence_count"] == 4
    assert result["rollback_run_log_file"].startswith("log_run_rollback.")
    assert result["rollback_run_log_file"].endswith(".jsonl")
    assert original.read_text(encoding="utf-8") == "input"
    assert not current.exists()
    assert not created.exists()
    rollback_rows = [
        row for row in _rows(ctx) if row["record_type"] == "source_file_rollback"
    ]
    assert [
        (row["rollback"]["phase"], row["status"]) for row in rollback_rows
    ] == [
        ("attempt", "attempted"),
        ("final", "success"),
        ("attempt", "attempted"),
        ("final", "success"),
    ]
    assert [
        row["rollback"]["original_record_id"] for row in rollback_rows
    ] == [create_id, create_id, move_id, move_id]
    assert (
        rollback_rows[1]["rollback"]["attempt_record_id"]
        == rollback_rows[0]["record_id"]
    )


def test_delete_and_replace_require_recorded_recovery_paths(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    _append_mutation(ctx, action="delete", source_path="/missing/original")
    _append_mutation(ctx, action="replace", destination_path="/missing/current")

    plan = preview_log_run_rollback(ctx, "run.jsonl")

    assert plan["candidate_count"] == 0
    assert plan["non_recoverable_count"] == 2


def test_delete_and_replace_restore_only_recorded_versions(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    deleted_original = tmp_path / "source" / "deleted.csv"
    recovery = tmp_path / "recovery" / "deleted.csv"
    recovery.parent.mkdir()
    recovery.write_text("deleted content", encoding="utf-8")
    replacement = tmp_path / "output" / "report.csv"
    replacement.parent.mkdir()
    replacement.write_text("new content", encoding="utf-8")
    previous = tmp_path / "recovery" / "report.previous.csv"
    previous.write_text("old content", encoding="utf-8")
    _append_mutation(
        ctx,
        action="delete",
        source_path=str(deleted_original),
        recovery_path=str(recovery),
    )
    _append_mutation(
        ctx,
        action="replace",
        destination_path=str(replacement),
        previous_version_path=str(previous),
    )

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert result["status"] == "success"
    assert replacement.read_text(encoding="utf-8") == "old content"
    assert deleted_original.read_text(encoding="utf-8") == "deleted content"


def test_failure_is_recorded_and_remaining_compensations_continue(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    _append_mutation(
        ctx,
        action="move",
        source_path=str(tmp_path / "missing-original"),
        destination_path=str(tmp_path / "missing-current"),
    )
    created = tmp_path / "created.csv"
    created.write_text("created", encoding="utf-8")
    _append_mutation(
        ctx,
        action="create",
        destination_path=str(created),
    )

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert result["status"] == "partial_success"
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    assert result["appended_rollback_evidence_count"] == 4
    assert not created.exists()
    audit_rows = [
        json.loads(line)
        for line in Path(result["audit_log_path"]).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    durable_summary = next(
        row["summary"]
        for row in audit_rows
        if row["record_type"] == "RUN_SUMMARY"
    )
    assert durable_summary["failures"] == result["failed"]
    assert durable_summary["failures"][0]["failure_reason"]
    finals = [
        row
        for row in _rows(ctx)
        if row["record_type"] == "source_file_rollback"
        and row["rollback"]["phase"] == "final"
    ]
    assert [row["status"] for row in finals] == ["success", "failure"]
    # The canonical rollback record carries no failure_reason; the run log and
    # the returned summary above hold the detail.
    assert "failure_reason" not in finals[1]


def test_successful_compensation_is_idempotently_excluded(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    created = tmp_path / "created.csv"
    created.write_text("created", encoding="utf-8")
    mutation_id = _append_mutation(
        ctx,
        action="create",
        destination_path=str(created),
    )
    run_log = _run_log(tmp_path)

    rollback_log_run(ctx, run_log)
    plan = preview_log_run_rollback(ctx, run_log)

    assert plan["candidate_count"] == 0
    assert plan["already_compensated"] == [mutation_id]


def test_unfinished_attempt_is_indeterminate_and_not_reapplied(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    mutation_id = _append_mutation(
        ctx,
        action="create",
        destination_path=str(tmp_path / "created.csv"),
    )
    log_file_manifest_record(
        ctx,
        serialize_source_file_rollback(
            original_record_id=mutation_id,
            phase="attempt",
            status="attempted",
            run_log_file="rollback.jsonl",
            run_log_record_id=1,
            application_name="rey_lib",
        ),
    )

    plan = preview_log_run_rollback(ctx, "run.jsonl")

    assert plan["candidate_count"] == 0
    assert plan["indeterminate"] == [mutation_id]


def test_future_operation_uses_registered_compensation_without_engine_change(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    calls: list[int] = []

    register_file_compensation(
        "future_action",
        compensating_action="future_compensation",
        validate=lambda _record: None,
        execute=lambda record: calls.append(int(record["record_id"])) or {},
    )
    try:
        mutation_id = _append_mutation(ctx, action="future_action")
        result = rollback_log_run(ctx, _run_log(tmp_path))
    finally:
        unregister_file_compensation("future_action")

    assert calls == [mutation_id]
    assert result["status"] == "success"


def test_unknown_action_is_reported_without_filesystem_inference(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    mutation_id = _append_mutation(ctx, action="unknown")

    plan = preview_log_run_rollback(ctx, "run.jsonl")

    assert plan["candidate_count"] == 0
    assert plan["unsupported"][0]["original_manifest_record_id"] == mutation_id

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert result["status"] == "failure"
    assert result["appended_rollback_evidence_count"] == 2
    finals = [
        row
        for row in _rows(ctx)
        if row["record_type"] == "source_file_rollback"
        and row["rollback"]["phase"] == "final"
    ]
    assert finals[0]["status"] == "failure"
    assert "failure_reason" not in finals[0]
    assert result["failed"][0]["failure_reason"] == (
        "no registered compensating operation"
    )


def test_malformed_manifest_fails_before_compensation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.paths.manifest.write_text("{bad json\n", encoding="utf-8")

    with pytest.raises(LogRunRollbackError, match="Invalid JSONL"):
        preview_log_run_rollback(ctx, "run.jsonl")


def test_missing_manifest_fails_instead_of_inventing_empty_authority(
    tmp_path: Path,
) -> None:
    with pytest.raises(LogRunRollbackError, match="does not exist"):
        preview_log_run_rollback(_ctx(tmp_path), "run.jsonl")


def test_malformed_mutation_schema_fails_before_compensation(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    log_file_manifest_record(
        ctx,
        {
            "record_type": "source_file_mutation",
            "action": "create",
            "status": "success",
            "evidence": {
                "run_log_file": "run.jsonl",
                "run_log_record_id": 1,
            },
        },
    )

    with pytest.raises(LogRunRollbackError, match="file must be an object"):
        preview_log_run_rollback(ctx, "run.jsonl")


def test_malformed_mutation_location_fails_before_compensation(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    log_file_manifest_record(
        ctx,
        {
            "record_type": "source_file_mutation",
            "action": "create",
            "status": "success",
            "file": {"path": ["/created/file"]},
            "evidence": {
                "run_log_file": "run.jsonl",
                "run_log_record_id": 1,
            },
        },
    )

    with pytest.raises(LogRunRollbackError, match="file.path must be a string"):
        preview_log_run_rollback(ctx, "run.jsonl")


def test_attempt_append_failure_prevents_filesystem_compensation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ctx = _ctx(tmp_path)
    created = tmp_path / "created.csv"
    created.write_text("created", encoding="utf-8")
    _append_mutation(ctx, action="create", destination_path=str(created))
    monkeypatch.setattr(
        FileManifestSession,
        "append",
        lambda _self, _record: (_ for _ in ()).throw(
            FileManifestError("attempt append failed")
        ),
    )

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert created.exists()
    assert result["status"] == "failure"
    assert result["appended_rollback_evidence_count"] == 0


def test_final_append_failure_leaves_traceable_indeterminate_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ctx = _ctx(tmp_path)
    created = tmp_path / "created.csv"
    created.write_text("created", encoding="utf-8")
    mutation_id = _append_mutation(
        ctx,
        action="create",
        destination_path=str(created),
    )
    original_append = FileManifestSession.append
    calls = 0

    def fail_second_append(session, record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FileManifestError("final append failed")
        return original_append(session, record)

    monkeypatch.setattr(FileManifestSession, "append", fail_second_append)

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert not created.exists()
    assert result["status"] == "failure"
    assert result["appended_rollback_evidence_count"] == 1
    plan = preview_log_run_rollback(ctx, "run.jsonl")
    assert plan["indeterminate"] == [mutation_id]


def test_preview_is_read_only_and_execution_rereads_manifest(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    first = tmp_path / "first.csv"
    first.write_text("first", encoding="utf-8")
    _append_mutation(ctx, action="create", destination_path=str(first))
    manifest_before = ctx.paths.manifest.read_bytes()

    preview = preview_log_run_rollback(ctx, "run.jsonl")

    assert preview["candidate_count"] == 1
    assert ctx.paths.manifest.read_bytes() == manifest_before
    assert not list(tmp_path.glob("log_run_rollback.*.jsonl"))

    second = tmp_path / "second.csv"
    second.write_text("second", encoding="utf-8")
    _append_mutation(ctx, action="create", destination_path=str(second))

    result = rollback_log_run(ctx, _run_log(tmp_path))

    assert result["succeeded_count"] == 2
    assert not first.exists()
    assert not second.exists()


@pytest.mark.parametrize("value", ["", None])
def test_run_log_file_is_required(tmp_path: Path, value: object) -> None:
    with pytest.raises(LogRunRollbackError, match="run_log_file"):
        preview_log_run_rollback(_ctx(tmp_path), value)  # type: ignore[arg-type]


def test_rollback_records_are_written_only_through_the_serializer(
    tmp_path: Path,
) -> None:
    """Every appended rollback row matches the serializer's canonical shape."""
    ctx = _ctx(tmp_path)
    created = tmp_path / "created.csv"
    created.write_text("created", encoding="utf-8")
    _append_mutation(ctx, action="create", destination_path=str(created))

    rollback_log_run(ctx, _run_log(tmp_path))

    rollback_rows = [
        row for row in _rows(ctx) if row["record_type"] == "source_file_rollback"
    ]
    assert rollback_rows
    for row in rollback_rows:
        expected = serialize_source_file_rollback(
            original_record_id=row["rollback"]["original_record_id"],
            phase=row["rollback"]["phase"],
            status=row["status"],
            run_log_file=row["evidence"]["run_log_file"],
            run_log_record_id=row["evidence"]["run_log_record_id"],
            attempt_record_id=row["rollback"].get("attempt_record_id"),
            application_name=row["producer"]["application"],
            recorded_at=row["recorded_at"],
        )
        assert {key: value for key, value in row.items() if key != "record_id"} == (
            expected
        )


def test_canonical_rollback_records_pass_validation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    mutation_id = _append_mutation(
        ctx, action="create", destination_path=str(tmp_path / "created.csv")
    )
    for record in (
        serialize_source_file_rollback(
            original_record_id=mutation_id,
            phase="attempt",
            status="attempted",
            run_log_file="rollback.jsonl",
            run_log_record_id=1,
        ),
        serialize_source_file_rollback(
            original_record_id=mutation_id,
            phase="final",
            status="success",
            run_log_file="rollback.jsonl",
            run_log_record_id=2,
            attempt_record_id=2,
        ),
    ):
        log_file_manifest_record(ctx, record)

    plan = preview_log_run_rollback(ctx, "run.jsonl")
    assert plan["already_compensated"] == [mutation_id]


def test_legacy_rollback_records_are_rejected(tmp_path: Path) -> None:
    """The legacy layout is not readable, and is no longer writable either.

    The row is written straight to the file because the manifest writer now
    refuses to create one. Nothing here interprets it: the canonical reader
    must fail on it rather than tolerate it.
    """
    ctx = _ctx(tmp_path)
    mutation_id = _append_mutation(
        ctx, action="create", destination_path=str(tmp_path / "created.csv")
    )
    with ctx.paths.manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "record_id": mutation_id + 1,
            "record_type": "source_file_rollback",
            "schema_version": "1.0",
            "original_manifest_record_id": mutation_id,
            "original_run_log_file": "run.jsonl",
            "original_action": "create",
            "compensating_action": "delete_created_file",
            "phase": "attempt",
            "status": "attempted",
            "evidence": {"run_log_file": "rollback.jsonl", "run_log_record_id": 1},
        }) + "\n")

    with pytest.raises(LogRunRollbackError, match="rollback must be an object"):
        preview_log_run_rollback(ctx, "run.jsonl")


def test_optional_rollback_fields_are_omitted_when_absent() -> None:
    attempt = serialize_source_file_rollback(
        original_record_id=5,
        phase="attempt",
        status="attempted",
        run_log_file="rollback.jsonl",
        run_log_record_id=1,
    )
    assert "attempt_record_id" not in attempt["rollback"]
    assert "result" not in attempt
    assert "file" not in attempt

    final = serialize_source_file_rollback(
        original_record_id=5,
        phase="final",
        status="failure",
        run_log_file="rollback.jsonl",
        run_log_record_id=2,
        attempt_record_id=7,
        reason="operator asked",
    )
    assert final["rollback"]["attempt_record_id"] == 7
    assert final["result"] == {"reason": "operator asked"}


def test_flat_fields_lead_every_governed_record() -> None:
    """Identity and type come first; the record's objects follow."""
    mutation = serialize_source_file_mutation(
        action="delete",
        status="success",
        source_path="/in/a.xlsx",
        recovery_path="/trash/a.xlsx",
        run_log_file="run.jsonl",
        run_log_record_id=1,
        application_name="file_operator",
        file_id="f1",
        conversion={"operator": "excel_conversion", "name": "all"},
        reason="processing",
    )
    assert list(mutation) == [
        "file_id",
        "recorded_at",
        "record_type",
        "action",
        "status",
        "evidence",
        "file",
        "rollback",
        "conversion",
        "result",
        "producer",
    ]

    rollback = serialize_source_file_rollback(
        original_record_id=57,
        phase="final",
        status="success",
        run_log_file="rollback.jsonl",
        run_log_record_id=4,
        attempt_record_id=61,
        application_name="rey_lib",
        reason="operator reset",
    )
    assert list(rollback) == [
        "recorded_at",
        "record_type",
        "status",
        "evidence",
        "rollback",
        "result",
        "producer",
    ]
