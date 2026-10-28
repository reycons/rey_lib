"""Tests for deterministic pre-publication candidate validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from rey_lib.files.rule_sets import (
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
)
from rey_lib.files.structural_normalization import (
    execute_structural_normalization,
)
from rey_lib.files.structural_validation import (
    validate_structural_normalization_candidates,
)
from tests.files.file_operator.test_rule_sets import _rule_set


def _source_text() -> str:
    return (
        "Monthly statement\n"
        "AccountID,TradeDate\n"
        "00123,2026-07-01\n"
        "\n"
        "AccountID,TradeDate\n"
        "page 12\n"
        "Grand Total,ignored\n"
        "SecurityID,Amount\n"
        "00007,10.00\n"
        "\n"
        "SecurityID,Amount\n"
        "00008,20.00\n"
    )


def _resolved_rule_set(expected_total: str = "30.00"):
    entry = deepcopy(_rule_set())
    entry["structural_normalization"]["tables"][1]["row_rules"] = [
        {
            "name": "drop_blank_rows",
            "order": 10,
            "action": "drop",
            "standard": "blank",
        },
        {
            "name": "drop_repeated_headers",
            "order": 20,
            "action": "drop",
            "standard": "repeated_header",
        },
    ]
    control = entry["structural_normalization"]["validations"][-1]
    control.update(
        table="securities",
        column="Amount",
        expected_values=[{"source_file": "statement.csv", "value": expected_total}],
    )
    entry["canonicalization"]["rule_sha256"] = canonical_rule_sha256(entry)
    return resolve_file_operator_rule_set(
        SimpleNamespace(file_operator_rule_sets=[entry]),
        entry["name"],
    )


def _candidate(tmp_path: Path, run_log, rule_set=None):
    source = tmp_path / "statement.csv"
    source.write_text(_source_text(), encoding="utf-8")
    resolved = rule_set or _resolved_rule_set()
    result = execute_structural_normalization(
        None, run_log,
        resolved,
        source,
        tmp_path / "candidates",
    )
    return resolved, result


def _check(result, validation_type: str):
    return next(check for check in result.checks if check.type == validation_type)


def test_all_named_validations_pass_and_evidence_is_deterministic(run_log, 
    tmp_path: Path,
) -> None:
    rule_set, candidate = _candidate(tmp_path, run_log)
    validation_dir = tmp_path / "validation"

    first = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv"],
        validation_dir,
    )
    first_bytes = first.evidence_path.read_bytes()
    second = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv"],
        validation_dir,
    )

    assert first.status == "passed"
    assert [check.type for check in first.checks] == [
        "matching_files",
        "table_headers",
        "table_regions",
        "output_rows",
        "deterministic_reruns",
        "control_total",
    ]
    assert all(check.status == "passed" for check in first.checks)
    assert second.evidence_path.read_bytes() == first_bytes
    assert first.evidence_sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert second.evidence_sha256 == first.evidence_sha256

    evidence = json.loads(first_bytes)
    assert evidence["status"] == "passed"
    assert evidence["expected_source_files"] == ["statement.csv"]
    assert evidence["candidate_source_files"] == ["statement.csv"]
    assert evidence["validation_count"] == 6
    deterministic = _check(first, "deterministic_reruns")
    assert deterministic.details["rerun_count"] == 2
    assert deterministic.details["deterministic"] is True
    control = _check(first, "control_total")
    assert control.details["totals"][0]["actual"] == "30.00"
    assert control.details["totals"][0]["passed"] is True


def test_matching_files_fails_when_authoritative_file_has_no_candidate(run_log, 
    tmp_path: Path,
) -> None:
    rule_set, candidate = _candidate(tmp_path, run_log)

    result = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv", "statement_2.csv"],
        tmp_path / "validation",
    )

    assert result.status == "failed"
    matching = _check(result, "matching_files")
    assert matching.status == "failed"
    assert matching.details["missing"] == ["statement_2.csv"]
    assert result.evidence_path.exists()


def test_output_rows_fail_when_candidate_no_longer_preserves_source_text(run_log, 
    tmp_path: Path,
) -> None:
    rule_set, candidate = _candidate(tmp_path, run_log)
    securities = candidate.outputs[1].output_path
    securities.write_text(
        securities.read_text(encoding="utf-8").replace("00007", "7"),
        encoding="utf-8",
    )

    result = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv"],
        tmp_path / "validation",
    )

    output_rows = _check(result, "output_rows")
    assert result.status == "failed"
    assert output_rows.status == "failed"
    assert any(
        "output hash does not match" in error for error in output_rows.details["errors"]
    )
    assert any(
        "not exact kept source rows" in error for error in output_rows.details["errors"]
    )
    summary = next(
        output
        for output in output_rows.details["outputs"]
        if output["table"] == "securities"
    )
    assert summary["exact_kept_rows"] is False


def test_header_validation_rejects_reintroduced_repeated_header(run_log, 
    tmp_path: Path,
) -> None:
    rule_set, candidate = _candidate(tmp_path, run_log)
    accounts = candidate.outputs[0].output_path
    accounts.write_text(
        accounts.read_text(encoding="utf-8") + "AccountID,TradeDate\n",
        encoding="utf-8",
    )

    result = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv"],
        tmp_path / "validation",
    )

    headers = _check(result, "table_headers")
    assert headers.status == "failed"
    assert any(
        "contains 2 matching output headers" in error
        for error in headers.details["errors"]
    )


def test_region_validation_rejects_overlapping_candidate_evidence(run_log, 
    tmp_path: Path,
) -> None:
    rule_set, candidate = _candidate(tmp_path, run_log)
    accounts, securities = candidate.outputs
    overlapping_securities = replace(
        securities,
        region_start_line=accounts.region_end_line_exclusive - 1,
        header_line_number=accounts.region_end_line_exclusive - 1,
    )
    overlapping_candidate = replace(
        candidate,
        outputs=(accounts, overlapping_securities),
    )

    result = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [overlapping_candidate],
        ["statement.csv"],
        tmp_path / "validation",
    )

    regions = _check(result, "table_regions")
    assert regions.status == "failed"
    assert any(
        "overlaps the previous table region" in error
        for error in regions.details["errors"]
    )


def test_control_total_failure_is_structured_and_blocks_pass(run_log, 
    tmp_path: Path,
) -> None:
    rule_set = _resolved_rule_set(expected_total="31.00")
    _, candidate = _candidate(tmp_path, run_log, rule_set)

    result = validate_structural_normalization_candidates(
        run_log,
        rule_set,
        [candidate],
        ["statement.csv"],
        tmp_path / "validation",
    )

    control = _check(result, "control_total")
    assert result.status == "failed"
    assert control.status == "failed"
    assert control.details["totals"] == [
        {
            "source_file": "statement.csv",
            "table": "securities",
            "column": "Amount",
            "aggregation": "sum",
            "actual": "30.00",
            "expected": "31.00",
            "difference": "1.00",
            "tolerance": "0.00",
            "passed": False,
        }
    ]
