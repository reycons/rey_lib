"""Tests for deterministic structural normalization execution."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.files.error_utils import InputError
from rey_lib.files.rule_sets import (
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
)
from rey_lib.files.structural_normalization import (
    execute_structural_normalization,
)
from tests.files.file_operator.test_rule_sets import _rule_set


def _ctx() -> SimpleNamespace:
    return SimpleNamespace()


def _resolved_rule_set(**overrides: object):
    entry = deepcopy(_rule_set())
    entry.update(overrides)
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
    entry["canonicalization"]["rule_sha256"] = canonical_rule_sha256(entry)
    ctx = SimpleNamespace(data_engine_rule_sets=[entry])
    return resolve_file_operator_rule_set(ctx, entry["name"])


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


def test_extracts_two_tables_and_applies_ordered_drop_rules(run_log, 
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.csv"
    original = _source_text()
    source.write_text(original, encoding="utf-8")
    candidate_dir = tmp_path / "candidates"

    result = execute_structural_normalization(
        _ctx(), run_log,
        _resolved_rule_set(),
        source,
        candidate_dir,
    )

    assert source.read_text(encoding="utf-8") == original
    assert len(result.outputs) == 2
    assert result.outside_table_line_numbers == (1,)
    assert (candidate_dir / "statement.accounts.csv").read_text(encoding="utf-8") == (
        "AccountID,TradeDate\n" "00123,2026-07-01\n"
    )
    assert (candidate_dir / "statement.securities.csv").read_text(encoding="utf-8") == (
        "SecurityID,Amount\n" "00007,10.00\n" "00008,20.00\n"
    )

    accounts, securities = result.outputs
    assert accounts.region_start_line == 2
    assert accounts.region_end_line_exclusive == 8
    assert accounts.output_row_count == 2
    assert accounts.data_row_count == 1
    assert accounts.dropped_row_count == 4
    assert securities.region_start_line == 8
    assert securities.region_end_line_exclusive == 13
    assert securities.output_row_count == 3
    assert securities.dropped_row_count == 2

    dropped = {
        decision.line_number: (
            decision.rule_name,
            decision.rule_order,
            decision.rule_kind,
        )
        for decision in accounts.decisions
        if decision.action == "drop"
    }
    assert dropped == {
        4: ("drop_blank_rows", 10, "standard:blank"),
        5: ("drop_repeated_headers", 20, "standard:repeated_header"),
        6: ("drop_page_number", 40, "match:regex"),
        7: ("drop_footer", 30, "match:contains"),
    }


def test_writes_deterministic_outputs_hashes_and_evidence(run_log, 
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.csv"
    source.write_text(_source_text(), encoding="utf-8")
    candidate_dir = tmp_path / "candidates"
    rule_set = _resolved_rule_set()

    with patch(
        "rey_lib.files.structural_normalization.read_text_file",
        wraps=Path.read_text,
    ) as source_read:
        first = execute_structural_normalization(
            _ctx(), run_log,
            rule_set,
            source,
            candidate_dir,
        )

    source_read.assert_called_once_with(source.resolve(), encoding="utf-8-sig")
    first_evidence = first.evidence_path.read_bytes()
    first_outputs = {
        output.output_path.name: output.output_path.read_bytes()
        for output in first.outputs
    }

    second = execute_structural_normalization(
        _ctx(), run_log,
        rule_set,
        source,
        candidate_dir,
    )

    assert second.evidence_path.read_bytes() == first_evidence
    assert {
        output.output_path.name: output.output_path.read_bytes()
        for output in second.outputs
    } == first_outputs
    evidence = json.loads(first_evidence)
    assert evidence["rule_set"]["name"] == "monthly_statement__v0001"
    assert evidence["outside_table_line_numbers"] == [1]
    assert [table["name"] for table in evidence["tables"]] == [
        "accounts",
        "securities",
    ]
    assert all(len(table["output_sha256"]) == 64 for table in evidence["tables"])
    assert all(
        "text" not in decision
        for table in evidence["tables"]
        for decision in table["decisions"]
    )


def test_matches_case_insensitive_headers_and_contains_field_boundaries(run_log, 
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.csv"
    source.write_text(
        "accountid,tradedate\n"
        "00001,2026-01-01\n"
        "prefix,securityid,extra,amount\n"
        "00002,3.00\n",
        encoding="utf-8",
    )

    result = execute_structural_normalization(
        _ctx(), run_log,
        _resolved_rule_set(),
        source,
        tmp_path / "candidates",
    )

    assert result.outputs[0].region_end_line_exclusive == 3
    assert result.outputs[1].region_start_line == 3
    assert result.outputs[1].data_row_count == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda entry: entry["structural_normalization"]["tables"][0]["end"][
                "match"
            ].update(values=["Missing", "Boundary"]),
            "end boundary was not found",
        ),
        (
            lambda entry: entry["structural_normalization"]["tables"][1]["header"][
                "match"
            ].update(values=["Missing", "Header"])
            or entry["structural_normalization"]["tables"][1].update(
                expected_columns=["Missing", "Header"]
            ),
            "header was not found",
        ),
        (
            lambda entry: entry["structural_normalization"]["tables"][0].update(
                output_name="../{source_stem}.accounts.csv"
            ),
            "must resolve to one filename",
        ),
    ],
)
def test_fails_before_writing_when_regions_or_outputs_are_invalid(run_log, 
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    entry = deepcopy(_rule_set())
    mutate(entry)  # type: ignore[operator]
    entry["canonicalization"]["rule_sha256"] = canonical_rule_sha256(entry)
    rule_set = resolve_file_operator_rule_set(
        SimpleNamespace(data_engine_rule_sets=[entry]),
        entry["name"],
    )
    source = tmp_path / "statement.csv"
    source.write_text(_source_text(), encoding="utf-8")
    candidate_dir = tmp_path / "candidates"

    with pytest.raises(InputError, match=message):
        execute_structural_normalization(
            _ctx(), run_log,
            rule_set,
            source,
            candidate_dir,
        )

    assert not candidate_dir.exists()


def test_rejects_disabled_rule_set_without_reading_source(run_log, tmp_path: Path) -> None:
    source = tmp_path / "statement.csv"
    source.write_text(_source_text(), encoding="utf-8")
    rule_set = _resolved_rule_set(enabled=False)

    with patch("rey_lib.files.structural_normalization.read_text_file") as source_read:
        with pytest.raises(InputError, match="is disabled"):
            execute_structural_normalization(
                _ctx(), run_log,
                rule_set,
                source,
                tmp_path / "candidates",
            )

    source_read.assert_not_called()
