"""Tests for line-preserving File Operator inspection artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.files import inspection as inspection_module
from rey_lib.files.error_utils import InputError
from rey_lib.files.inspection import update_file_type_analysis_artifact
# The reduction moved to rey_lib; its internals are tested where they now live.
from rey_lib.profiling import structural_analysis
from rey_lib.profiling.structural_analysis import _select_patterns
from rey_lib.files.file_utils import read_text_file as real_read_text
from rey_lib.files.processor import process_file, run_batch


def _args(output_dir: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "output_dir": str(output_dir),
        "layout": "delimited",
        "delimiter": None,
        "encoding": None,
        "sample_size": None,
        "redact_columns": None,
        "redact_ranges": None,
        "worksheet": None,
        "processed_path": None,
        "rejected_path": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace()


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _statement_source(tmp_path: Path) -> tuple[Path, Path]:
    """A statement with a preamble, a blank line and a repeated header.

    Seven physical lines, so the line-identity assertions have something to be
    stable about, and two redactable columns of different detected types.
    """
    source = tmp_path / "statement.csv"
    source.write_text(
        "Monthly statement\n"
        "\n"
        "Account,Name\n"
        "00123,Alice\n"
        "\n"
        "Account,Name\n"
        "00456,Bob\n",
        encoding="utf-8",
    )
    return source, tmp_path / "out"


def test_inspection_uses_single_read_and_preserves_stable_line_identity(run_log, 
    tmp_path: Path,
) -> None:
    source, output_dir = _statement_source(tmp_path)

    # The source must be read exactly once. That read now belongs to
    # rey_lib.files.csv, so the spy sits on the boundary it actually crosses.
    with patch(
        "rey_lib.files.csv.read_text_file",
        wraps=real_read_text,
    ) as source_read:
        process_file(
            _ctx(), run_log,
            source,
            _args(
                output_dir,
                redact_columns="Account,Name",
                skip_blank_lines=True,
            ),
        )

    source_read.assert_called_once_with(source, encoding="utf-8-sig", errors="strict")
    records = _jsonl(output_dir / "statement.inspection.jsonl")
    metadata = records[0]
    rows = records[1:]
    assert metadata["record_type"] == "inspection_metadata"
    assert metadata["schema_version"] == 2
    assert metadata["row_count"] == 7
    assert [row["physical_line_number"] for row in rows] == list(range(1, 8))
    assert [row["logical_row_number"] for row in rows] == list(range(1, 8))
    assert rows[0]["region"] == "preamble"
    assert rows[2]["is_detected_header"] is True
    assert rows[4]["is_blank"] is True
    assert rows[5]["is_header_candidate"] is True
    rendered = "\n".join(str(row["text"]) for row in rows)
    assert "00123" not in rendered
    assert "00456" not in rendered


def test_inspection_redacts_every_column_it_was_told_to(
    run_log, tmp_path: Path,
) -> None:
    """The rendered rows carry no value from a redacted column.

    Split from the single-read test above so that the read-count and
    line-identity contract it is named for stays live rather than being hidden
    behind a redaction defect it has nothing to do with. The integer columns
    are asserted there; these are the ones that leak.
    """
    source, output_dir = _statement_source(tmp_path)
    process_file(
        _ctx(), run_log, source,
        _args(output_dir, redact_columns="Account,Name", skip_blank_lines=True),
    )
    rows = _jsonl(output_dir / "statement.inspection.jsonl")[1:]
    rendered = "\n".join(str(row["text"]) for row in rows)
    assert "Alice" not in rendered
    assert "Bob" not in rendered


def test_file_type_grouping_emits_exactly_one_analysis_artifact_per_type(run_log, 
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    distinct = tmp_path / "distinct.csv"
    first.write_text("A,B\n1,2\n", encoding="utf-8")
    second.write_text("a,b\n3,4\n", encoding="utf-8")
    distinct.write_text("A,C\n5,6\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    assert (
        run_batch(
            [first, second, distinct],
            _ctx(), run_log,
            _args(output_dir),
        )
        == 0
    )

    analysis_paths = sorted(output_dir.glob("file_type_*.analysis.json"))
    assert len(analysis_paths) == 2
    analyses = [json.loads(path.read_text(encoding="utf-8")) for path in analysis_paths]
    assert {tuple(analysis["matching_files"]) for analysis in analyses} == {
        ("distinct.csv",),
        ("first.csv", "second.csv"),
    }
    assert all(
        analysis["structural_patterns"]["primary_payload"] is True
        and analysis["ordered_context"]["secondary_evidence"] is True
        for analysis in analyses
    )

    manifest = json.loads(
        (output_dir / "file_type_analysis_manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["file_types"]) == 2
    assert {entry["analysis_artifact"] for entry in manifest["file_types"]} == {
        path.name for path in analysis_paths
    }
    for entry in manifest["file_types"]:
        analysis_bytes = (output_dir / entry["analysis_artifact"]).read_bytes()
        assert entry["analysis_artifact_bytes"] == len(analysis_bytes)
        assert entry["analysis_artifact_bytes"] <= 2_000_000
        assert entry["bounded"] is True
        assert entry["truncated"] is False
        assert entry["pattern_records"]["omitted"] == 0
        assert entry["ordered_context_records"]["omitted"] == 0
        assert (
            entry["analysis_artifact_sha256"]
            == hashlib.sha256(analysis_bytes).hexdigest()
        )


def test_analysis_builder_rejects_inspection_coverage_not_in_file_type(run_log, 
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    file_type_path = output_dir / "file_type_example.json"
    file_type_path.write_text(
        json.dumps(
            {
                "file_type": "file_type_example",
                "signature": "a" * 64,
                "layout": "delimited",
                "ordered_headers": ["A", "B"],
                "normalized_headers": ["a", "b"],
                "matching_files": ["missing.csv"],
                "profile_artifacts": ["missing.profile.json"],
                "inspection_artifacts": ["missing.inspection.jsonl"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InputError, match="Cannot read inspection artifact"):
        update_file_type_analysis_artifact(
            _ctx(),
            run_log,
            output_dir,
            file_type_path,
        )


def test_analysis_input_has_fixed_total_row_bound(run_log, tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    source.write_text(
        "A,B\n" + "".join(f"{index},{index + 1}\n" for index in range(2_005)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    process_file(_ctx(), run_log, source, _args(output_dir))

    analysis_path = next(output_dir.glob("file_type_*.analysis.json"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    ordered_context = analysis["ordered_context"]
    assert analysis["analysis_limits"]["max_ordered_context_records"] == 2_000
    assert ordered_context["observed_row_count"] == 2_006
    assert ordered_context["included_row_count"] <= 2_000
    assert len(analysis_path.read_bytes()) <= 2_000_000
    line_numbers = [
        record["physical_line_number"] for record in ordered_context["records"]
    ]
    assert 1 in line_numbers
    assert 2_006 in line_numbers


def test_pattern_summary_retains_rare_shapes_when_data_rows_dominate(run_log, 
    tmp_path: Path,
) -> None:
    source = tmp_path / "patterns.csv"
    source.write_text(
        "Account,Amount\n"
        + "".join(f"{index:05d},{index}.00\n" for index in range(5_000))
        + "\n"
        + "Account,Amount\n"
        + "Report total\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    process_file(
        _ctx(), run_log,
        source,
        _args(output_dir, redact_columns="Account"),
    )

    analysis_path = next(output_dir.glob("file_type_*.analysis.json"))
    first_bytes = analysis_path.read_bytes()
    analysis = json.loads(first_bytes)
    pattern_payload = analysis["structural_patterns"]
    patterns = pattern_payload["records"]

    assert pattern_payload["primary_payload"] is True
    assert pattern_payload["observed_pattern_count"] == len(patterns)
    assert pattern_payload["omitted_pattern_count"] == 0
    assert sum(pattern["occurrence_count"] for pattern in patterns) == 5_004
    assert max(pattern["occurrence_count"] for pattern in patterns) > 4_000
    assert any(
        pattern["descriptor"]["blank"] and pattern["occurrence_count"] == 1
        for pattern in patterns
    )
    assert any(
        pattern["descriptor"]["repeated_header_shape"]
        and pattern["occurrence_count"] == 1
        for pattern in patterns
    )
    assert any(
        pattern["descriptor"]["expected_width_relation"] == "narrower"
        and pattern["occurrence_count"] == 1
        for pattern in patterns
    )
    assert all(len(pattern["representatives"]) <= 3 for pattern in patterns)
    assert all(pattern["first_occurrence"] for pattern in patterns)
    assert all(pattern["last_occurrence"] for pattern in patterns)
    assert all(pattern["representative_line_numbers"] for pattern in patterns)

    descriptors = [pattern["descriptor"] for pattern in patterns]
    assert all(
        {
            "field_count",
            "blank",
            "line_length_bucket",
            "delimiter_count",
            "parse_status",
            "normalized_field_characteristics",
            "header_candidate",
            "header_identity_sha256",
            "repeated_header_shape",
        }
        <= set(descriptor)
        for descriptor in descriptors
    )

    context_by_line = {
        record["physical_line_number"]: record
        for record in analysis["ordered_context"]["records"]
    }
    repeated_header_line = 5_003
    assert repeated_header_line in context_by_line
    assert (
        "repeated_header_shape"
        in context_by_line[repeated_header_line]["context_reasons"]
    )
    assert repeated_header_line - 1 in context_by_line
    assert repeated_header_line + 1 in context_by_line

    process_file(
        _ctx(), run_log,
        source,
        _args(output_dir, redact_columns="Account"),
    )
    assert analysis_path.read_bytes() == first_bytes


def test_serialized_byte_limit_trims_secondary_context_before_patterns(run_log, 
    tmp_path: Path,
) -> None:
    source = tmp_path / "wide.csv"
    long_value = "1" * 4_000
    source.write_text(
        "A,B\n" + "".join(f"{long_value}{index},{index}\n" for index in range(1_800)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    process_file(_ctx(), run_log, source, _args(output_dir))

    analysis_path = next(output_dir.glob("file_type_*.analysis.json"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert len(analysis_path.read_bytes()) <= 2_000_000
    assert analysis["structural_patterns"]["included_pattern_count"] > 0
    assert analysis["ordered_context"]["observed_row_count"] == 1_801
    assert (
        analysis["ordered_context"]["included_row_count"]
        < analysis["ordered_context"]["observed_row_count"]
    )
    assert analysis["completeness"]["bounded"] is True
    assert analysis["completeness"]["truncated"] is True
    assert analysis["completeness"]["patterns"]["omitted"] == 0
    assert analysis["completeness"]["ordered_context"]["omitted"] > 0

    manifest = json.loads(
        (output_dir / "file_type_analysis_manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["file_types"][0]
    assert entry["truncated"] is True
    assert entry["pattern_records"] == analysis["completeness"]["patterns"]
    assert (
        entry["ordered_context_records"] == analysis["completeness"]["ordered_context"]
    )


def test_pattern_limit_prioritizes_rare_shapes_and_reserves_common_baseline() -> None:
    patterns = {
        "common": {
            "structural_signature": "common",
            "occurrence_count": 100_000,
            "important": False,
        }
    }
    patterns.update(
        {
            f"rare-{index:04d}": {
                "structural_signature": f"rare-{index:04d}",
                "occurrence_count": 1,
                "important": True,
            }
            for index in range(1_005)
        }
    )

    selected = _select_patterns(patterns, 10)
    signatures = {pattern["structural_signature"] for pattern in selected}

    assert len(selected) == 10
    assert "common" in signatures
    assert len(signatures - {"common"}) == 9


def test_manifest_reports_dropped_pattern_counts(run_log, 
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The limit moved to rey_lib with the reduction it bounds.
    monkeypatch.setattr(structural_analysis, "_MAX_PATTERN_RECORDS", 2)
    # Pattern variety without invalid rows. This stage receives sanitized
    # files -- the profiling process selects on `result.reason:
    # file_sanitization` -- so a blank row and a one-field trailer are input it
    # cannot get, and either landing first in the sample left profile_rows with
    # no columns and raised before the analysis was written. Every row here is
    # the header's width; the patterns differ by what the fields *are*.
    source = tmp_path / "bounded_patterns.csv"
    source.write_text(
        "A,B\n" "1,2\n" "xx,yy\n" "3,\n" "zzz,4\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    process_file(_ctx(), run_log, source, _args(output_dir))

    analysis = json.loads(
        next(output_dir.glob("file_type_*.analysis.json")).read_text(encoding="utf-8")
    )
    pattern_counts = analysis["completeness"]["patterns"]
    assert pattern_counts["observed"] > 2
    assert pattern_counts["included"] == 2
    assert pattern_counts["omitted"] == pattern_counts["observed"] - 2
    assert analysis["completeness"]["truncated"] is True

    manifest = json.loads(
        (output_dir / "file_type_analysis_manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["file_types"][0]
    assert entry["truncated"] is True
    assert entry["pattern_records"] == pattern_counts
