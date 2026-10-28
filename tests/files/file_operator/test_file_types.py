"""Focused tests for deterministic File Operator classification artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files.error_utils import InputError
from rey_lib.files.file_types import update_file_type_artifact


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_log_path=str(tmp_path / "run.jsonl"),
        run_id="run-1",
        run_timestamp="20260723_120000",
        owner_app_name="file_operator",
    )


def _profile(headers: list[str]) -> dict:
    return {
        "columns": [
            {"raw_name": header, "ordinal": index}
            for index, header in enumerate(headers, start=1)
        ]
    }


def test_same_ordered_normalized_headers_update_one_artifact(run_log, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    first = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["Run Date", "Account Number"],
        header_index=2,
        header_line="Run Date,Account Number",
        source_files=["first.csv"],
        profile=_profile(["Run Date", "Account Number"]),
        profile_artifacts=["first.profile.json"],
    )
    second = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["run_date", "ACCOUNT_NUMBER"],
        header_index=0,
        header_line="run_date,ACCOUNT_NUMBER",
        source_files=["second.csv"],
        profile=_profile(["run_date", "ACCOUNT_NUMBER"]),
        profile_artifacts=["second.profile.json"],
    )

    assert first == second
    assert len(list(output_dir.glob("file_type_*.json"))) == 1
    artifact = json.loads(first.read_text(encoding="utf-8"))
    assert artifact["file_type"] == first.stem
    assert artifact["normalized_headers"] == ["run_date", "account_number"]
    assert artifact["ordered_headers"] == ["Run Date", "Account Number"]
    assert artifact["matching_files"] == ["first.csv", "second.csv"]
    assert artifact["profile_artifacts"] == [
        "first.profile.json",
        "second.profile.json",
    ]
    assert artifact["columns"] == [
        {"ordinal": 1, "raw_name": "Run Date"},
        {"ordinal": 2, "raw_name": "Account Number"},
    ]
    assert artifact["detected_header"] == {
        "index": 2,
        "line": "Run Date,Account Number",
        "row_number": 3,
    }


def test_different_header_order_creates_distinct_artifact(run_log, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    first = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["A", "B"],
        header_index=0,
        header_line="A,B",
        source_files=["first.csv"],
        profile=_profile(["A", "B"]),
        profile_artifacts=["first.profile.json"],
    )
    second = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["B", "A"],
        header_index=0,
        header_line="B,A",
        source_files=["second.csv"],
        profile=_profile(["B", "A"]),
        profile_artifacts=["second.profile.json"],
    )

    assert first != second
    assert len(list(output_dir.glob("file_type_*.json"))) == 2


def test_filename_classifications_are_exact_deduplicated_and_sorted(run_log, 
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    regex = r"^(?P<owner>.+?)(?P<kind>hold|tran)(?P<period>[A-Za-z]{3}\d{2})[.]csv$"
    artifact_path = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["Account ID", "Trade Date"],
        header_index=0,
        header_line="Account ID,Trade Date",
        source_files=["ZEDtranAug26.csv", "ABCholdJul26.csv"],
        profile=_profile(["Account ID", "Trade Date"]),
        profile_artifacts=["first.profile.json"],
        filename_regex=regex,
    )
    update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["account_id", "trade_date"],
        header_index=0,
        header_line="account_id,trade_date",
        source_files=["ABCholdJul26.csv"],
        profile=_profile(["account_id", "trade_date"]),
        profile_artifacts=["second.profile.json"],
        filename_regex=regex,
    )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["filename_classifications"] == [
        {
            "filename": "ABCholdJul26.csv",
            "values": {
                "kind": "hold",
                "owner": "ABC",
                "period": "Jul26",
            },
        },
        {
            "filename": "ZEDtranAug26.csv",
            "values": {
                "kind": "tran",
                "owner": "ZED",
                "period": "Aug26",
            },
        },
    ]


def test_existing_artifact_without_filename_classifications_is_extended(run_log, 
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    first = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["Account ID", "Trade Date"],
        header_index=0,
        header_line="Account ID,Trade Date",
        source_files=["ABCholdJul26.csv"],
        profile=_profile(["Account ID", "Trade Date"]),
        profile_artifacts=["first.profile.json"],
    )
    original = json.loads(first.read_text(encoding="utf-8"))
    assert "filename_classifications" not in original

    second = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        output_dir,
        header_fields=["account_id", "trade_date"],
        header_index=0,
        header_line="account_id,trade_date",
        source_files=["ABCholdJul26.csv"],
        profile=_profile(["account_id", "trade_date"]),
        profile_artifacts=["second.profile.json"],
        filename_regex=(
            r"^(?P<owner>.+?)(?P<kind>hold|tran)"
            r"(?P<period>[A-Za-z]{3}\d{2})[.]csv$"
        ),
    )

    assert first == second
    artifact = json.loads(second.read_text(encoding="utf-8"))
    assert artifact["signature"] == (
        "97018a46b8a0baf987a31096134306ba5fcb1fe9ed969730ee50868e1b57f858"
    )
    assert artifact["file_type"] == "file_type_97018a46b8a0baf9"
    assert artifact["filename_classifications"][0]["values"]["owner"] == "ABC"


def test_no_regex_preserves_existing_artifact_shape(run_log, tmp_path: Path) -> None:
    artifact_path = update_file_type_artifact(
        _ctx(tmp_path), run_log,
        tmp_path / "out",
        header_fields=["A", "B"],
        header_index=0,
        header_line="A,B",
        source_files=["source.csv"],
        profile=_profile(["A", "B"]),
        profile_artifacts=["source.profile.json"],
    )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert "filename_classifications" not in artifact


def test_filename_classification_non_match_raises_input_error(run_log, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not match"):
        update_file_type_artifact(
            _ctx(tmp_path), run_log,
            tmp_path / "out",
            header_fields=["A", "B"],
            header_index=0,
            header_line="A,B",
            source_files=["unexpected.csv"],
            profile=_profile(["A", "B"]),
            profile_artifacts=["unexpected.profile.json"],
            filename_regex=r"^(?P<owner>ABC)holdJul26[.]csv$",
        )

    assert not list((tmp_path / "out").glob("file_type_*.json"))
