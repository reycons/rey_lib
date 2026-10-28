"""Tests for deterministic candidate-to-durable-rule materialization."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files.error_utils import InputError, RedactorError
from rey_lib.files.rule_set_materialization import (
    ControlTotal,
    ControlTotalExpectation,
    MaterializationRequest,
    materialize_file_operator_rule_set,
)
from rey_lib.files.rule_sets import (
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
)
from rey_lib.config.config_utils import parse_yaml


_FILE_TYPE = "file_type_0123456789abcdef"

#: The analyzer's contract as control.analysis_contract stores it (backlog 903):
#: lineage verifies it by declared name and the hash of this text, never a file.
_CONTRACT_TEXT = (
    "---\n"
    "name: infer_tabular_discard_rules\n"
    'version: "02"\n'
    'effective_date: "2026-10-12"\n'
    "---\n"
    "\n"
    "deterministic test contract\n"
)


class _Control:
    """The runtime's Control, answering get_analysis_contract."""

    installation_id = 1

    def analysis_contract(self, name: str) -> dict | None:
        if name != "infer_tabular_discard_rules":
            return None
        return {"name": name, "format": "markdown", "contract_text": _CONTRACT_TEXT}


_CTX = SimpleNamespace(shared_control=_Control())


def _source_text(second_total: str) -> str:
    return (
        "Monthly statement\n"
        "AccountID,TradeDate\n"
        "00123,2026-07-01\n"
        "\n"
        "AccountID,TradeDate\n"
        "00456,2026-07-02\n"
        "Grand Total,ignored\n"
        "SecurityID,Amount\n"
        f"00007,{second_total}\n"
        "\n"
        "SecurityID,Amount\n"
        "00008,20.00\n"
    )


def _write_inputs(tmp_path: Path) -> tuple[MaterializationRequest, dict[str, object]]:
    source_a = tmp_path / "statement_a.csv"
    source_b = tmp_path / "statement_b.csv"
    source_a.write_text(_source_text("10.00"), encoding="utf-8")
    source_b.write_text(_source_text("20.00"), encoding="utf-8")

    evidence = [
        {
            "source_file": source_name,
            "physical_line_number": line,
            "logical_row_number": line,
        }
        for source_name in (source_a.name, source_b.name)
        for line in range(1, 13)
    ]
    analysis = {
        "schema_version": 2,
        "file_type": _FILE_TYPE,
        "file_type_artifact": f"{_FILE_TYPE}.json",
        "signature": "a" * 64,
        "layout": "delimited",
        "ordered_headers": ["AccountID", "TradeDate"],
        "normalized_headers": ["accountid", "tradedate"],
        "matching_files": [source_a.name, source_b.name],
        "profile_artifacts": [],
        "inspection_artifacts": [],
        "analysis_limits": {},
        "completeness": {
            "bounded": True,
            "truncated": False,
            "patterns": {"observed": 8, "included": 8, "omitted": 0},
            "ordered_context": {
                "observed": 24,
                "included": 24,
                "omitted": 0,
            },
        },
        "structural_patterns": {
            "primary_payload": True,
            "records": [{"representatives": evidence}],
        },
        "ordered_context": {
            "secondary_evidence": True,
            "records": evidence,
        },
    }
    analysis_path = tmp_path / f"{_FILE_TYPE}.analysis.json"
    analysis_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    candidate = _candidate(source_a.name)
    candidate_path = tmp_path / f"{_FILE_TYPE}.analysis.candidate.json"
    candidate_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result_path = tmp_path / "result.json"
    result_path.write_text('{"status":"success"}\n', encoding="utf-8")
    context = {
        "request_id": "request-1",
        "run_id": "run-1",
        "workflow_name": "infer_bad_row_redaction_rules",
        "source_name": "file_operator_structural_file_type",
        "analysis_name": "infer_tabular_discard_rules",
        "model_profile": "local_precision",
        "provider": "local",
        "model": "test-model",
        "source_artifact_path": str(analysis_path),
        "source_artifact_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
        "contract_name": "infer_tabular_discard_rules",
        "contract_hash": hashlib.sha256(_CONTRACT_TEXT.encode("utf-8")).hexdigest(),
        "schema_hash": "b" * 64,
        "idempotency_mode": "reuse_success",
        "result_artifact_path": str(result_path),
        "result_artifact_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "candidate_artifact_path": str(candidate_path),
        "candidate_artifact_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "status": "success",
        "errors": [],
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(context, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    request = MaterializationRequest(
        rule_set_name="monthly_statement",
        version=1,
        analysis_artifact_path=analysis_path,
        candidate_rules_path=candidate_path,
        analyzer_context_path=context_path,
        model_profile="local_precision",
        source_paths=(source_b, source_a),
        delimiter=",",
        encoding="utf-8",
        control_totals=(
            ControlTotal(
                name="security_total",
                table="securities",
                column="Amount",
                tolerance="0.00",
                expected_values=(
                    ControlTotalExpectation(source_a.name, "30.00"),
                    ControlTotalExpectation(source_b.name, "40.00"),
                ),
            ),
        ),
        approved_by="test-reviewer",
        approved_at="2026-07-28T12:00:00Z",
        output_dir=tmp_path / "generated",
    )
    return request, candidate


def _point(source_file: str, line: int) -> list[dict[str, object]]:
    return [
        {
            "source_file": source_file,
            "physical_line_number": line,
        }
    ]


def _refresh_context_candidate_hash(request: MaterializationRequest) -> None:
    context = json.loads(request.analyzer_context_path.read_text(encoding="utf-8"))
    context["candidate_artifact_sha256"] = hashlib.sha256(
        request.candidate_rules_path.read_bytes()
    ).hexdigest()
    request.analyzer_context_path.write_text(
        json.dumps(context, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _header(
    source_file: str,
    line: int,
    values: list[str],
    match_type: str = "exact_fields",
) -> dict[str, object]:
    return {
        "match": {
            "type": match_type,
            "values": values,
            "case_sensitive": False,
            "trim_fields": True,
        },
        "keep_first": True,
        "repeated": {
            "comparison": "normalized_exact",
            "action": "drop",
            "evidence": _point(source_file, line),
            "confidence": 1.0,
        },
        "evidence": _point(source_file, line),
        "confidence": 1.0,
    }


def _candidate(source_file: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "file_type": _FILE_TYPE,
        "tables": [
            {
                "name": "accounts",
                "expected_columns": ["AccountID", "TradeDate"],
                "header": _header(
                    source_file,
                    2,
                    ["AccountID", "TradeDate"],
                ),
                "start": {
                    "type": "header",
                    "evidence": _point(source_file, 2),
                    "confidence": 1.0,
                },
                "end": {
                    "type": "before_match",
                    "match": {
                        "type": "exact_fields",
                        "values": ["SecurityID", "Amount"],
                        "case_sensitive": False,
                        "trim_fields": True,
                    },
                    "evidence": _point(source_file, 8),
                    "confidence": 1.0,
                },
                "row_rules": [
                    {
                        "name": "drop_blank",
                        "order": 1,
                        "action": "drop",
                        "standard": "blank",
                        "evidence": _point(source_file, 4),
                        "confidence": 1.0,
                    },
                    {
                        "name": "drop_repeated_header",
                        "order": 2,
                        "action": "drop",
                        "standard": "repeated_header",
                        "evidence": _point(source_file, 5),
                        "confidence": 1.0,
                    },
                    {
                        "name": "drop_total",
                        "order": 3,
                        "action": "drop",
                        "match": {
                            "type": "contains",
                            "value": "Grand Total",
                            "case_sensitive": False,
                        },
                        "evidence": _point(source_file, 7),
                        "confidence": 1.0,
                    },
                ],
            },
            {
                "name": "securities",
                "expected_columns": ["SecurityID", "Amount"],
                "header": _header(
                    source_file,
                    8,
                    ["SecurityID", "Amount"],
                ),
                "start": {
                    "type": "header",
                    "evidence": _point(source_file, 8),
                    "confidence": 1.0,
                },
                "end": {
                    "type": "eof",
                    "evidence": _point(source_file, 12),
                    "confidence": 1.0,
                },
                "row_rules": [
                    {
                        "name": "drop_blank",
                        "order": 1,
                        "action": "drop",
                        "standard": "blank",
                        "evidence": _point(source_file, 10),
                        "confidence": 1.0,
                    },
                    {
                        "name": "drop_repeated_header",
                        "order": 2,
                        "action": "drop",
                        "standard": "repeated_header",
                        "evidence": _point(source_file, 11),
                        "confidence": 1.0,
                    },
                ],
            },
        ],
        "unresolved_ambiguities": [],
    }


def test_materializes_only_after_two_full_sample_executions(run_log, 
    tmp_path: Path,
) -> None:
    request, _ = _write_inputs(tmp_path)

    first = materialize_file_operator_rule_set(_CTX, run_log, request)
    first_yaml = first.yaml_path.read_bytes()
    first_validation = first.validation.evidence_path.read_bytes()
    second = materialize_file_operator_rule_set(_CTX, run_log, request)

    assert first.validation.status == "passed"
    deterministic = next(
        check
        for check in first.validation.checks
        if check.type == "deterministic_reruns"
    )
    assert deterministic.details["rerun_count"] == 2
    assert len(deterministic.details["hashes_by_run"]) == 2
    assert all(
        sorted(run) == ["statement_a.csv", "statement_b.csv"]
        for run in deterministic.details["hashes_by_run"]
    )
    assert second.yaml_path.read_bytes() == first_yaml
    assert second.validation.evidence_path.read_bytes() == first_validation

    parsed = parse_yaml(first_yaml.decode("utf-8"))
    item = parsed["data_engine_rule_sets"][0]
    assert item["name"] == "monthly_statement__v0001"
    assert item["file_references"] == [
        {
            "name": "authoritative_header",
            "header": "AccountID,TradeDate",
        }
    ]
    assert item["structural_normalization"]["tables"][0]["output_name"] == (
        "{source_stem}.accounts.csv"
    )
    assert item["structural_normalization"]["tables"][0]["row_rules"][0]["order"] == 1
    assert item["approval"]["validated_file_count"] == 2
    assert item["approval"]["matching_file_count"] == 2
    assert item["canonicalization"]["rule_sha256"] == canonical_rule_sha256(item)
    assert (
        item["lineage"]["analysis_artifact_sha256"]
        == hashlib.sha256(request.analysis_artifact_path.read_bytes()).hexdigest()
    )
    assert (
        item["lineage"]["candidate_rules_sha256"]
        == hashlib.sha256(request.candidate_rules_path.read_bytes()).hexdigest()
    )
    assert item["lineage"]["validation_evidence_sha256"] == (
        first.validation.evidence_sha256
    )


def test_failed_validation_writes_evidence_but_never_yaml(run_log, tmp_path: Path) -> None:
    request, _ = _write_inputs(tmp_path)
    bad_control = ControlTotal(
        name="security_total",
        table="securities",
        column="Amount",
        tolerance="0.00",
        expected_values=(
            ControlTotalExpectation("statement_a.csv", "31.00"),
            ControlTotalExpectation("statement_b.csv", "40.00"),
        ),
    )
    request = MaterializationRequest(
        **{
            **request.__dict__,
            "control_totals": (bad_control,),
        }
    )

    with pytest.raises(InputError, match="YAML was not written"):
        materialize_file_operator_rule_set(_CTX, run_log, request)

    assert not (request.output_dir / "monthly_statement__v0001.yaml").exists()
    evidence_path = (
        request.output_dir / "validation" / f"{_FILE_TYPE}.rule_validation.json"
    )
    assert evidence_path.exists()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    control = next(
        item for item in evidence["validations"] if item["type"] == "control_total"
    )
    assert control["status"] == "failed"


def test_rejects_candidate_evidence_absent_from_analysis(run_log, tmp_path: Path) -> None:
    request, candidate = _write_inputs(tmp_path)
    candidate["tables"][0]["row_rules"][0]["evidence"][0][  # type: ignore[index]
        "physical_line_number"
    ] = 99
    request.candidate_rules_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _refresh_context_candidate_hash(request)

    with pytest.raises(InputError, match="not present in analysis evidence"):
        materialize_file_operator_rule_set(_CTX, run_log, request)

    assert not request.output_dir.exists()


def test_rejects_noncontiguous_rule_order(run_log, tmp_path: Path) -> None:
    request, candidate = _write_inputs(tmp_path)
    candidate["tables"][0]["row_rules"][1]["order"] = 3  # type: ignore[index]
    request.candidate_rules_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _refresh_context_candidate_hash(request)

    with pytest.raises(InputError, match="start at 1 and be contiguous"):
        materialize_file_operator_rule_set(_CTX, run_log, request)


def test_rejects_source_set_that_differs_from_authoritative_group(run_log, 
    tmp_path: Path,
) -> None:
    request, _ = _write_inputs(tmp_path)
    request = MaterializationRequest(
        **{
            **request.__dict__,
            "source_paths": request.source_paths[:1],
        }
    )

    with pytest.raises(InputError, match="exactly match analysis matching_files"):
        materialize_file_operator_rule_set(_CTX, run_log, request)


def test_materialization_rejects_expected_vs_actual_lineage_mismatch(run_log, 
    tmp_path: Path,
) -> None:
    request, _ = _write_inputs(tmp_path)
    context = json.loads(request.analyzer_context_path.read_text(encoding="utf-8"))
    competing_root = tmp_path / "competing"
    competing_root.mkdir()
    competing = competing_root / request.analysis_artifact_path.name
    competing.write_bytes(request.analysis_artifact_path.read_bytes())
    context["source_artifact_path"] = str(competing)
    request.analyzer_context_path.write_text(
        json.dumps(context, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(InputError, match="source artifact does not match"):
        materialize_file_operator_rule_set(_CTX, run_log, request)


def test_analyzer_contract_lineage_is_verified_against_the_stored_contract(run_log,
    tmp_path: Path,
) -> None:
    """Contract lineage is the declared name plus the hash of the stored text
    (backlog 903): a hash the stored contract does not have is refused, and so
    is a name the installation does not store."""
    request, _ = _write_inputs(tmp_path)
    context = json.loads(request.analyzer_context_path.read_text(encoding="utf-8"))

    context["contract_hash"] = "c" * 64
    request.analyzer_context_path.write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(InputError, match="contract hash does not match stored contract"):
        materialize_file_operator_rule_set(_CTX, run_log, request)

    context["contract_name"] = "not_stored"
    request.analyzer_context_path.write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(InputError, match="cannot be resolved"):
        materialize_file_operator_rule_set(_CTX, run_log, request)


def test_analyzer_context_source_and_result_hashes_are_verified(run_log, 
    tmp_path: Path,
) -> None:
    request, _ = _write_inputs(tmp_path)
    context = json.loads(request.analyzer_context_path.read_text(encoding="utf-8"))
    result_path = Path(context["result_artifact_path"])
    result_path.write_text('{"status":"tampered"}\n', encoding="utf-8")

    with pytest.raises(InputError, match="result artifact hash does not match"):
        materialize_file_operator_rule_set(_CTX, run_log, request)


def test_resolver_rejects_tampered_executable_content(run_log, tmp_path: Path) -> None:
    request, _ = _write_inputs(tmp_path)
    result = materialize_file_operator_rule_set(_CTX, run_log, request)
    item = deepcopy(result.rule_set_item)
    item["source_format"]["encoding"] = "utf-16"

    with pytest.raises(
        RedactorError,
        match="does not match the canonical executable rule content",
    ):
        resolve_file_operator_rule_set(
            SimpleNamespace(data_engine_rule_sets=[item]),
            item["name"],
        )
