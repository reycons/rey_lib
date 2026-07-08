"""
Tests that rey_loader's transform outputs surface as 'loader' artifacts
(SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector, Phase 5.1).

rey_loader delegates transforming to rey_lib.files.file_loader, which now logs
each prepared/transformed output as an ARTIFACT_REFERENCE. Attribution flows
through the record's app name, so a rey_loader run groups under producer
'loader'. The full transform pipeline is config-heavy to fixture, so these cover
the wiring and the app -> producer resolution end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import rey_lib
from rey_lib.logs import (
    group_artifacts_by_producer,
    log_artifact_reference,
    normalize_artifacts,
)


def _records(run_log: Path) -> list[dict]:
    return [json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_transform_outputs_group_under_loader(tmp_path: Path) -> None:
    """Transform outputs emitted under a rey_loader run normalize to producer 'loader'."""
    run_log = tmp_path / "run_log.20260708_000000.jsonl"
    ctx = SimpleNamespace(run_log_path=str(run_log), run_id="r1",
                          run_timestamp="20260708_000000", app_name="rey_loader")
    source = tmp_path / "trades.raw.csv"
    # Emit exactly what file_loader now logs at its two output points.
    log_artifact_reference(ctx, str(tmp_path / "trades.canonical.csv"), role="prepared",
                           artifact_type="prepared_file", source_path=str(source),
                           viewer_type="file", safe_to_preview=True)
    log_artifact_reference(ctx, str(tmp_path / "trades.transformed.csv"), role="transformed",
                           artifact_type="transformed_file", source_path=str(source),
                           viewer_type="file", safe_to_preview=True)

    grouped = group_artifacts_by_producer(normalize_artifacts(_records(run_log)))
    assert set(grouped) == {"loader"}                      # app rey_loader -> loader
    types = {a["artifact_type"] for a in grouped["loader"]}
    assert types == {"prepared_file", "transformed_file"}
    assert all(a["source_path"] == str(source) for a in grouped["loader"])
    assert all(a["safe_to_preview"] is True for a in grouped["loader"])


def test_file_loader_wires_artifact_emission_at_both_output_points() -> None:
    """The transform helper logs both prepared (byte-copy) and transformed outputs."""
    source = (Path(rey_lib.__file__).resolve().parent / "files" / "file_loader.py").read_text(
        encoding="utf-8"
    )
    assert source.count("log_artifact_reference(") >= 2
    assert 'artifact_type="prepared_file"' in source
    assert 'artifact_type="transformed_file"' in source
    assert "source_path=str(file_path)" in source
