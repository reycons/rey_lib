"""Tests for durable File Operator rule-set configuration validation."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from rey_lib.files.error_utils import RedactorError
from rey_lib.files.rule_sets import (
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
    resolve_file_operator_rule_sets,
)

_HASH = "a" * 64


def _rule_set() -> dict[str, object]:
    item: dict[str, object] = {
        "name": "monthly_statement__v0001",
        "schema_version": 1,
        "rule_set_name": "monthly_statement",
        "version": 1,
        "status": "approved",
        "enabled": True,
        "file_type": "file_type_statement",
        "file_references": [
            {
                "name": "filename",
                "file_pattern": ["*statement*.csv"],
            },
            {
                "name": "header",
                "file_pattern": "*.csv",
                "header": "AccountID,TradeDate",
            },
        ],
        "source_format": {
            "layout": "delimited",
            "delimiter": ",",
            "encoding": "utf-8-sig",
        },
        "structural_normalization": {
            "enabled": True,
            "mode": "rules",
            "default_action": "keep",
            "unmatched_row_policy": "drop_outside_tables",
            "tables": [
                {
                    "name": "accounts",
                    "output_name": "{source_stem}.accounts.csv",
                    "expected_columns": ["AccountID", "TradeDate"],
                    "header": {
                        "match": {
                            "type": "exact_fields",
                            "values": ["AccountID", "TradeDate"],
                            "case_sensitive": False,
                            "trim_fields": True,
                        },
                        "keep_first": True,
                        "repeated": {
                            "comparison": "normalized_exact",
                            "action": "drop",
                        },
                    },
                    "start": {"type": "header"},
                    "end": {
                        "type": "before_match",
                        "match": {
                            "type": "contains_fields",
                            "values": ["SecurityID", "Amount"],
                            "case_sensitive": False,
                            "trim_fields": True,
                        },
                    },
                    "row_rules": [
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
                        {
                            "name": "drop_footer",
                            "order": 30,
                            "action": "drop",
                            "match": {
                                "type": "contains",
                                "value": "Total",
                                "case_sensitive": False,
                            },
                        },
                        {
                            "name": "drop_page_number",
                            "order": 40,
                            "action": "drop",
                            "match": {
                                "type": "regex",
                                "pattern": r"^Page \d+$",
                                "flags": ["IGNORECASE"],
                            },
                        },
                    ],
                },
                {
                    "name": "securities",
                    "output_name": "{source_stem}.securities.csv",
                    "expected_columns": ["SecurityID", "Amount"],
                    "header": {
                        "match": {
                            "type": "contains_fields",
                            "values": ["SecurityID", "Amount"],
                            "case_sensitive": False,
                            "trim_fields": True,
                        },
                        "keep_first": True,
                        "repeated": {
                            "comparison": "normalized_exact",
                            "action": "drop",
                        },
                    },
                    "start": {"type": "header"},
                    "end": {"type": "eof"},
                    "row_rules": [],
                },
            ],
            "validations": [
                {
                    "name": "matching_files",
                    "type": "matching_files",
                    "require_every_matching_file": True,
                    "allow_missing_matching_files": False,
                },
                {
                    "name": "table_headers",
                    "type": "table_headers",
                    "require_unique_table_headers": True,
                    "require_exactly_one_output_header": True,
                },
                {
                    "name": "table_regions",
                    "type": "table_regions",
                    "require_non_overlapping_table_regions": True,
                },
                {
                    "name": "output_rows",
                    "type": "output_rows",
                    "require_consistent_field_count": True,
                    "require_expected_column_order": True,
                    "minimum_data_rows_per_table": 1,
                    "reject_unresolved_rows": True,
                    "preserve_leading_zero_values": True,
                },
                {
                    "name": "deterministic_reruns",
                    "type": "deterministic_reruns",
                    "count": 2,
                },
                {
                    "name": "account_total",
                    "type": "control_total",
                    "table": "accounts",
                    "column": "TradeDate",
                    "aggregation": "sum",
                    "tolerance": "0.00",
                    "expected_values": [
                        {"source_file": "statement.csv", "value": "10.00"}
                    ],
                },
            ],
        },
        "lineage": {
            "analysis_artifact": "file_type.analysis.json",
            "analysis_artifact_sha256": _HASH,
            "llm_contract_name": "infer_tabular_discard_rules",
            "llm_contract_sha256": _HASH,
            "analyzer_workflow": "infer_rules",
            "analysis_name": "infer_tabular_discard_rules",
            "model_profile": "local",
            "provider": "local",
            "model": "test-model",
            "request_id": "request-1",
            "analyzer_run_id": "run-1",
            "analyzer_result_path": "result.json",
            "analyzer_result_sha256": _HASH,
            "candidate_rules_path": "candidate.json",
            "candidate_rules_sha256": _HASH,
            "validation_evidence_path": "validation.json",
            "validation_evidence_sha256": _HASH,
        },
        "approval": {
            "status": "passed",
            "validated_file_count": 2,
            "matching_file_count": 2,
            "unresolved_ambiguities": [],
            "deterministic_output_hashes_match": True,
            "approved_by": "reviewer",
            "approved_at": "2026-07-28T12:00:00Z",
        },
        "canonicalization": {
            "algorithm": "canonical-json-sorted-keys-utf8",
            "rule_sha256": _HASH,
        },
    }
    item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)  # type: ignore[index]
    return item


def _ctx(*entries: object) -> SimpleNamespace:
    return SimpleNamespace(data_engine_rule_sets=list(entries))


def _refresh_rule_hash(item: dict[str, object]) -> dict[str, object]:
    item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)  # type: ignore[index]
    return item


def test_resolves_complete_contract_to_immutable_values() -> None:
    resolved = resolve_file_operator_rule_set(
        _ctx(_rule_set()),
        "monthly_statement__v0001",
    )

    assert resolved.rule_set_name == "monthly_statement"
    assert resolved.version == 1
    assert resolved.file_references[0].header is None
    assert resolved.file_references[0].file_patterns == ("*statement*.csv",)
    assert resolved.structural_normalization.tables[0].end_type == "before_match"
    assert resolved.structural_normalization.tables[1].end_type == "eof"
    assert [
        rule.order for rule in resolved.structural_normalization.tables[0].row_rules
    ] == [10, 20, 30, 40]
    with pytest.raises(TypeError):
        resolved.lineage["request_id"] = "changed"  # type: ignore[index]


def test_accepts_header_only_file_reference() -> None:
    entry = _rule_set()
    entry["file_references"] = [
        {"name": "header_only", "header": "AccountID,TradeDate"}
    ]
    _refresh_rule_hash(entry)

    resolved = resolve_file_operator_rule_sets(_ctx(entry))

    assert resolved[0].file_references[0].file_patterns == ()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda item: item.update(name="monthly_statement__v0002"),
            "name.*version.*equal",
        ),
        (
            lambda item: item.update(status="draft"),
            "status.*approved",
        ),
        (
            lambda item: item["file_references"][0].update(header=None)
            or item["file_references"][0].pop("file_pattern"),
            "file_pattern, header, or both",
        ),
        (
            lambda item: item["structural_normalization"]["tables"][0]["row_rules"][
                0
            ].update(
                match={
                    "type": "contains",
                    "value": "x",
                    "case_sensitive": False,
                }
            ),
            "exactly one of 'standard' or 'match'",
        ),
        (
            lambda item: item["structural_normalization"]["tables"][0]["row_rules"][
                1
            ].update(order=5),
            "ascending order",
        ),
        (
            lambda item: item["structural_normalization"]["tables"][0].update(
                end={"type": "eof"}
            ),
            "only the final table",
        ),
        (
            lambda item: item["structural_normalization"]["validations"].pop(2),
            "missing required types: table_regions",
        ),
        (
            lambda item: item["structural_normalization"]["validations"][0].update(
                require_every_matching_file=False
            ),
            "require_every_matching_file.*true",
        ),
        (
            lambda item: item["structural_normalization"]["validations"][-1].update(
                column="Missing"
            ),
            "references unknown column",
        ),
        (
            lambda item: item["approval"].update(matching_file_count=3),
            "validated_file_count must equal",
        ),
        (
            lambda item: item["canonicalization"].update(rule_sha256="b" * 64),
            "does not match the canonical executable rule content",
        ),
        (
            lambda item: item["canonicalization"].update(rule_sha256="bad"),
            "64-character SHA-256",
        ),
        (
            lambda item: item.update(unexpected=True),
            "unknown fields: unexpected",
        ),
    ],
)
def test_rejects_contract_violations(mutate: object, message: str) -> None:
    entry = deepcopy(_rule_set())
    mutate(entry)  # type: ignore[operator]

    with pytest.raises(RedactorError, match=message):
        resolve_file_operator_rule_sets(_ctx(entry))


def test_rejects_invalid_regex_during_configuration_resolution() -> None:
    entry = _rule_set()
    regex_rule = entry["structural_normalization"]["tables"][0]["row_rules"][3]
    regex_rule["match"]["pattern"] = "["

    with pytest.raises(RedactorError, match="invalid regex pattern"):
        resolve_file_operator_rule_sets(_ctx(entry))


def test_rejects_duplicate_rule_set_revision() -> None:
    entry = _rule_set()

    with pytest.raises(RedactorError, match="names must be unique"):
        resolve_file_operator_rule_sets(_ctx(entry, deepcopy(entry)))


@pytest.mark.parametrize(
    ("ctx", "message"),
    [
        (SimpleNamespace(), "missing required 'data_engine_rule_sets'"),
        (SimpleNamespace(data_engine_rule_sets={}), "must be a list"),
        (_ctx(), "must not be empty"),
    ],
)
def test_rejects_missing_or_malformed_section(
    ctx: SimpleNamespace,
    message: str,
) -> None:
    with pytest.raises(RedactorError, match=message):
        resolve_file_operator_rule_sets(ctx)
