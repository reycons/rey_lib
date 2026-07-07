"""
Tests for config-file provenance recording
(SGC_Rey_Config_Utils_Run_Log_Config_File_Recording).

config_utils emits one CONFIG_FILE_REFERENCE per config file that contributed to
the effective execution context, from recorded provenance rather than file reads
or filename inference. The records populate the Log Inspector Config Files node.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.config.config_utils import record_config_file_references
from rey_lib.config.provenance import ConfigMetadata
from rey_lib.logs import read_run_log_sections


def _config_refs(run_log: Path) -> list[dict]:
    """Return CONFIG_FILE_REFERENCE records from a run log."""
    return [
        record
        for line in run_log.read_text(encoding="utf-8").splitlines() if line.strip()
        for record in [json.loads(line)]
        if record.get("record_type") == "CONFIG_FILE_REFERENCE"
    ]


def _ctx_with_metadata(tmp_path: Path, metadata: ConfigMetadata) -> SimpleNamespace:
    """Return a minimal run-bound ctx carrying provenance metadata."""
    run_log = tmp_path / "run_log.20260707_000000.jsonl"
    return SimpleNamespace(
        run_log_path=str(run_log),
        run_id="r1",
        run_timestamp="20260707_000000",
        workflow_name="demo_workflow",
        _config_metadata=metadata,
    )


def _two_layer_metadata() -> ConfigMetadata:
    """Installation config plus a workflow file that overrides one value."""
    metadata = ConfigMetadata()
    metadata.record_tree(
        {"paths": [{"name": "data", "path": "/d"}], "logging": {"level": "INFO"}},
        source_file="/cfg/config.yaml",
        layer="installation",
    )
    metadata.record_tree(
        {"logging": {"level": "DEBUG"}, "workflows": [{"name": "wf", "enabled": True}]},
        source_file="/cfg/workflows/wf.yaml",
        layer="workflow",
    )
    return metadata


def test_effective_context_emits_config_records(tmp_path: Path) -> None:
    """Each contributing config file emits one CONFIG_FILE_REFERENCE record."""
    ctx = _ctx_with_metadata(tmp_path, _two_layer_metadata())
    record_config_file_references(ctx)
    refs = _config_refs(Path(ctx.run_log_path))
    paths = {record["path"] for record in refs}
    assert paths == {"/cfg/config.yaml", "/cfg/workflows/wf.yaml"}


def test_duplicate_config_files_emitted_once(tmp_path: Path) -> None:
    """A file contributing many values is still recorded exactly once."""
    metadata = ConfigMetadata()
    metadata.record_tree(
        {"logging": {"level": "INFO"}, "database": {"host": "h", "port": 5432}},
        source_file="/cfg/config.yaml",
        layer="installation",
    )
    ctx = _ctx_with_metadata(tmp_path, metadata)
    record_config_file_references(ctx)
    refs = _config_refs(Path(ctx.run_log_path))
    assert [record["path"] for record in refs] == ["/cfg/config.yaml"]


def test_role_comes_from_provenance_not_filename(tmp_path: Path) -> None:
    """The role tracks the provenance layer, not the file name."""
    metadata = ConfigMetadata()
    # A file literally named config.yaml but contributed at the workflow layer.
    metadata.record_tree(
        {"workflows": [{"name": "wf", "enabled": True}]},
        source_file="/cfg/workflows/config.yaml",
        layer="workflow",
    )
    ctx = _ctx_with_metadata(tmp_path, metadata)
    record_config_file_references(ctx)
    record = _config_refs(Path(ctx.run_log_path))[0]
    assert record["file_role"] == "Workflow"
    assert record["configuration_layer"] == "workflow"


def test_variable_provenance_and_overrides_associated_with_file(tmp_path: Path) -> None:
    """Contributed sections and overrides stay with their originating file."""
    ctx = _ctx_with_metadata(tmp_path, _two_layer_metadata())
    record_config_file_references(ctx)
    refs = {record["path"]: record for record in _config_refs(Path(ctx.run_log_path))}
    workflow_ref = refs["/cfg/workflows/wf.yaml"]
    # The workflow file overrode logging.level from the installation layer.
    assert workflow_ref["overrides"] == ["logging.level"]
    assert "workflows" in workflow_ref["variables_contributed"]
    # load_order reflects merge order: installation first, workflow second.
    assert refs["/cfg/config.yaml"]["load_order"] == 0
    assert workflow_ref["load_order"] == 1


def test_log_inspector_config_files_populated(tmp_path: Path) -> None:
    """The projected Config Files section is populated from the records."""
    ctx = _ctx_with_metadata(tmp_path, _two_layer_metadata())
    record_config_file_references(ctx)
    sections = read_run_log_sections(Path(ctx.run_log_path))["sections"]
    config_files = sections["files"]["config_files"]
    assert config_files["count"] == 2
    roles = {entry["file_role"] for entry in config_files["files"]}
    assert roles == {"Installation", "Workflow"}


def test_no_metadata_records_nothing(tmp_path: Path) -> None:
    """A ctx without provenance metadata emits no config records."""
    run_log = tmp_path / "run_log.20260707_000000.jsonl"
    ctx = SimpleNamespace(
        run_log_path=str(run_log), run_id="r1", run_timestamp="20260707_000000",
    )
    record_config_file_references(ctx)
    assert not run_log.exists()


def test_recording_is_fail_safe(tmp_path: Path) -> None:
    """A recording failure never raises into the caller."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    ctx = SimpleNamespace(
        run_log_path=str(blocker / "nested" / "run_log.jsonl"),
        run_id="r1", run_timestamp="20260707_000000",
        _config_metadata=_two_layer_metadata(),
    )
    # Must not raise even though the run log path cannot be written.
    record_config_file_references(ctx)


@pytest.fixture(autouse=True)
def _isolate_ambient_run():
    """Config recording binds nothing, but keep ambient run state clean."""
    from rey_lib.logs import clear_run

    clear_run()
    yield
    clear_run()
