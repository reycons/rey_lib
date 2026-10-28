"""
Tests that file_operator logs its outputs as grounded producer artifacts
(SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector, Phase 2).

The redacted sample and profile are written as ARTIFACT_REFERENCE records tagged
producer=redactor, and the Phase 1 normalizer groups them under the redactor.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.files.layouts.common import write_outputs
from rey_lib.logs import group_artifacts_by_producer, normalize_artifacts


def _run_ctx(tmp_path: Path) -> SimpleNamespace:
    """A minimal run-bound ctx that log_artifact_reference/write_file can use."""
    return SimpleNamespace(
        run_log_path=str(tmp_path / "run_log.20260708_000000.jsonl"),
        run_id="r1",
        run_timestamp="20260708_000000",
        owner_app_name="data_engine",
    )


def _artifacts(log_path: Path) -> list[dict]:
    return [
        record
        for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()
        for record in [json.loads(line)]
        if record.get("record_type") == "ARTIFACT_REFERENCE"
    ]


def test_write_outputs_logs_redactor_artifacts(run_log, tmp_path: Path) -> None:
    """The redacted sample and profile are logged as producer=redactor artifacts."""
    ctx = _run_ctx(tmp_path)
    source = tmp_path / "customers.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    write_outputs(ctx, run_log, source, output_dir, ["a,b", "1,2"], {"rows": 1})

    by_type = {a["artifact_type"]: a for a in _artifacts(Path(run_log.path()))}
    # Redacted sample: first-class redacted_file, safe to preview, from the source.
    sample = by_type["redacted_file"]
    assert sample["producer"] == "redactor"
    assert sample["source_path"] == str(source)
    assert sample["safe_to_preview"] is True
    assert sample["path"].endswith("customers.redacted.sample.csv")
    # Profile is also a redactor artifact.
    assert by_type["profile"]["producer"] == "redactor"


def test_redactor_artifacts_group_under_redactor_via_normalizer(run_log, tmp_path: Path) -> None:
    """End to end: emitted artifacts normalize/group under the redactor producer."""
    ctx = _run_ctx(tmp_path)
    source = tmp_path / "trades.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    write_outputs(ctx, run_log, source, output_dir, ["x", "1"], {"rows": 1})

    records = [json.loads(line) for line in Path(run_log.path()).read_text().splitlines()
               if line.strip()]
    grouped = group_artifacts_by_producer(normalize_artifacts(records))
    assert "redactor" in grouped
    labels = {a["label"] for a in grouped["redactor"]}
    assert any("redacted.sample" in label for label in labels)
    # The redacted sample references the original source for lineage/preference.
    sample = next(a for a in grouped["redactor"] if "redacted.sample" in a["label"])
    assert sample["source_path"] == str(source)
    assert sample["artifact_type"] == "redacted_file"
