"""Contract tests for the expanded run-level ARTIFACT_MANIFEST."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    build_artifact_manifest_entries,
    log_artifact_manifest_from_run_log,
    log_artifact_reference,
    log_config_file_reference,
    log_file_operation,
    log_input_file_reference,
)


REQUIRED_FIELDS = {
    "path", "display_name", "artifact_group", "file_role",
    "producing_app", "producing_step", "status", "actions", "exists",
    "safe_to_preview", "size_bytes", "modified_at",
}


def _ctx(tmp_path: Path, app: str = "manifest_test") -> SimpleNamespace:
    return SimpleNamespace(
        run_log_path=str(tmp_path / "run.20260713_120000.jsonl"),
        run_id="run-1",
        run_timestamp="20260713_120000",
        app_name=app,
    )


def _records(ctx: SimpleNamespace) -> list[dict]:
    path = Path(ctx.run_log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_manifest_contains_complete_producer_declared_inventory(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    input_path = tmp_path / "input.csv"
    config_path = tmp_path / "config.yaml"
    output_path = tmp_path / "result.json"
    for path in (input_path, config_path, output_path):
        path.write_text("data", encoding="utf-8")

    log_input_file_reference(
        ctx, str(input_path), file_role="source", consumed_by_step="prepare",
    )
    log_config_file_reference(
        ctx, str(config_path), file_role="pipeline_config",
    )
    log_artifact_reference(
        ctx, str(output_path), role="analysis_result", event="written",
        artifact_group="analysis_results", producing_app="rey_analyzer",
        producing_step="analyze",
    )

    manifest = build_artifact_manifest_entries(_records(ctx))
    assert [entry["artifact_group"] for entry in manifest] == [
        "input_files", "config_files", "analysis_results",
    ]
    assert all(REQUIRED_FIELDS <= set(entry) for entry in manifest)
    assert manifest[-1]["producing_app"] == "rey_analyzer"
    assert manifest[-1]["producing_step"] == "analyze"
    assert manifest[-1]["actions"] == ["view", "copy_path", "open_external"]


def test_all_supported_inventory_categories_are_explicit_declarations(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    declared = [
        ("output.json", "output_files", "generated_output"),
        ("context.json", "context_files", "ctx_snapshot"),
        ("result.json", "analysis_results", "analysis_result"),
        ("analysis-context.json", "analysis_context", "analysis_context"),
        ("profile.json", "profiles", "profile"),
        ("diagnostic.log", "diagnostics", "diagnostic"),
    ]
    input_path = tmp_path / "input.csv"
    config_path = tmp_path / "pipeline.yaml"
    input_path.write_text("input", encoding="utf-8")
    config_path.write_text("config", encoding="utf-8")
    log_input_file_reference(ctx, str(input_path), file_role="source")
    log_config_file_reference(ctx, str(config_path), file_role="pipeline_config")
    for name, group, role in declared:
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        log_artifact_reference(
            ctx, str(path), role=role, artifact_group=group,
            producing_app="fixture_app", producing_step="fixture_step",
        )

    manifest = build_artifact_manifest_entries(_records(ctx))
    assert [entry["artifact_group"] for entry in manifest] == [
        "input_files", "config_files", *(group for _, group, _ in declared),
    ]
    assert all(REQUIRED_FIELDS <= set(entry) for entry in manifest)


def test_file_operations_alone_never_create_manifest_entries(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    log_file_operation(
        ctx, "move", source_path=str(tmp_path / "source.csv"),
        target_path=str(tmp_path / "target.csv"),
    )
    assert build_artifact_manifest_entries(_records(ctx)) == []


def test_unknown_groups_pass_through_unchanged(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    path = tmp_path / "future.bin"
    path.write_bytes(b"x")
    log_artifact_reference(
        ctx, str(path), role="future_role", artifact_group="future_group",
        producing_app="future_app",
    )
    assert build_artifact_manifest_entries(_records(ctx))[0]["artifact_group"] == "future_group"


def test_deduplication_uses_canonical_path_and_first_order(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    log_artifact_reference(
        ctx, str(first), role="result", artifact_group="analysis_results",
        producing_app="analyzer",
    )
    log_artifact_reference(
        ctx, str(second), role="report", artifact_group="diagnostics",
        producing_app="diagnostics",
    )
    log_artifact_reference(
        ctx, str(first.parent / "." / first.name), role="result",
        artifact_group="analysis_results", producing_app="analyzer",
        producing_step="finalize",
    )

    manifest = build_artifact_manifest_entries(_records(ctx))
    assert [Path(entry["path"]).name for entry in manifest] == ["first.json", "second.json"]
    assert manifest[0]["producing_step"] == "finalize"


def test_duplicate_merge_updates_later_explicit_missing_state(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    path = tmp_path / "result.json"
    path.write_text("result", encoding="utf-8")
    log_artifact_reference(
        ctx, str(path), role="result", artifact_group="analysis_results",
        producing_app="analyzer",
    )
    path.unlink()
    log_artifact_reference(
        ctx, str(path), role="result", artifact_group="analysis_results",
        producing_app="analyzer", status="missing",
    )

    entry = build_artifact_manifest_entries(_records(ctx))[0]
    assert entry["status"] == "missing"
    assert entry["exists"] is False
    assert entry["actions"] == ["copy_path"]


@pytest.mark.parametrize("run_status", ["success", "failed"])
def test_completed_empty_manifest_is_emitted_once(
    tmp_path: Path, run_status: str,
) -> None:
    ctx = _ctx(tmp_path)
    Path(ctx.run_log_path).write_text(
        json.dumps({"record_type": "RUN_COMPLETE", "status": run_status}) + "\n",
        encoding="utf-8",
    )
    log_artifact_manifest_from_run_log(ctx)
    log_artifact_manifest_from_run_log(ctx)

    manifests = [
        record for record in _records(ctx)
        if record.get("record_type") == "ARTIFACT_MANIFEST"
    ]
    assert len(manifests) == 1
    assert manifests[0]["record_group"] == "files"
    assert manifests[0]["artifacts"] == []


def test_manifest_is_not_emitted_before_run_completion(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    path = tmp_path / "partial.json"
    path.write_text("{}", encoding="utf-8")
    log_artifact_reference(
        ctx, str(path), role="partial", artifact_group="output_files",
        producing_app="manifest_test",
    )
    log_artifact_manifest_from_run_log(ctx)
    assert not any(
        record.get("record_type") == "ARTIFACT_MANIFEST" for record in _records(ctx)
    )


def test_legacy_declaration_without_group_is_not_classified_centrally(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    path = tmp_path / "legacy.json"
    path.write_text("{}", encoding="utf-8")
    log_artifact_reference(ctx, str(path), role="report")
    assert build_artifact_manifest_entries(_records(ctx)) == []
