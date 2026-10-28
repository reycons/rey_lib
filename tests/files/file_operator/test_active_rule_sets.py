"""Tests for rule-set activation and LLM-free future-file execution."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files.active_rule_sets import (
    activate_generated_rule_set,
    execute_active_rule_set,
    resolve_active_rule_set_for_file,
)
from rey_lib.files.error_utils import InputError
from rey_lib.files.rule_set_materialization import (
    MaterializationRequest,
    materialize_file_operator_rule_set,
)
from rey_lib.files.rule_sets import canonical_rule_sha256
from rey_lib.config.config_utils import parse_yaml
from tests.files.file_operator.test_rule_set_materialization import _CTX, _Control, _write_inputs


class _Paths:
    def __init__(self, configs: Path, destination: Path) -> None:
        self._values = {
            "configs": configs,
            "file_operator_rule_sets_config": destination,
        }

    def resolve(self, name: str) -> Path:
        return self._values[name]


def _activation_ctx(
    tmp_path: Path,
    entries: list[dict[str, object]] | None = None,
) -> SimpleNamespace:
    configs = tmp_path / "installation"
    destination = configs / "work" / "file_operator" / "file_operator_rule_sets.yaml"
    return SimpleNamespace(
        paths=_Paths(configs, destination),
        file_operator_rule_sets=entries or [],
        shared_control=_Control(),
    )


def _materialized(
    tmp_path: Path, run_log,
    *,
    version: int = 1,
    output_name: str = "generated",
):
    request, _candidate = _write_inputs(tmp_path)
    request = MaterializationRequest(
        **{
            **request.__dict__,
            "version": version,
            "output_dir": tmp_path / output_name,
        }
    )
    return request, materialize_file_operator_rule_set(_CTX, run_log, request)


def _installed_items(path: Path) -> list[dict[str, object]]:
    root = parse_yaml(path.read_text(encoding="utf-8"))
    return root["file_operator_rule_sets"]


def test_activation_preserves_versions_and_atomically_switches_active_version(run_log, 
    tmp_path: Path,
) -> None:
    _request_v1, generated_v1 = _materialized(tmp_path, run_log)
    ctx = _activation_ctx(tmp_path)

    first = activate_generated_rule_set(ctx, run_log, generated_v1.yaml_path)
    first_bytes = first.installation_path.read_bytes()
    assert first.active_rule_set_name == "monthly_statement__v0001"
    assert first.preserved_versions == ()

    _request_v2, generated_v2 = _materialized(
        tmp_path, run_log,
        version=2,
        output_name="generated_v2",
    )
    second = activate_generated_rule_set(ctx, run_log, generated_v2.yaml_path)
    items = _installed_items(second.installation_path)

    assert first_bytes != second.installation_path.read_bytes()
    assert [item["name"] for item in items] == [
        "monthly_statement__v0001",
        "monthly_statement__v0002",
    ]
    assert items[0]["enabled"] is False
    assert items[1]["enabled"] is True
    assert (
        items[0]["canonicalization"]["rule_sha256"] == generated_v1.rule_set.rule_sha256
    )
    assert items[0]["canonicalization"]["rule_sha256"] == canonical_rule_sha256(
        items[0]
    )
    assert items[1]["canonicalization"]["rule_sha256"] == canonical_rule_sha256(
        items[1]
    )
    assert second.preserved_versions == ("monthly_statement__v0001",)


def test_activation_never_overwrites_existing_revision(run_log, tmp_path: Path) -> None:
    _request, generated = _materialized(tmp_path, run_log)
    ctx = _activation_ctx(tmp_path)
    first = activate_generated_rule_set(ctx, run_log, generated.yaml_path)
    original = first.installation_path.read_bytes()

    with pytest.raises(InputError, match="never overwritten"):
        activate_generated_rule_set(ctx, run_log, generated.yaml_path)

    assert first.installation_path.read_bytes() == original


def test_activation_verifies_contract_lineage_against_the_stored_contract(
    run_log, tmp_path: Path,
) -> None:
    """Activation re-verifies the contract by name and stored-text hash, not a file."""
    _request, generated = _materialized(tmp_path, run_log)

    class _ChangedControl(_Control):
        def analysis_contract(self, name: str) -> dict | None:
            row = super().analysis_contract(name)
            return None if row is None else {**row, "contract_text": row["contract_text"] + "changed\n"}

    ctx = _activation_ctx(tmp_path)
    ctx.shared_control = _ChangedControl()

    with pytest.raises(InputError, match="contract hash does not match stored contract"):
        activate_generated_rule_set(ctx, run_log, generated.yaml_path)


def test_activation_target_must_be_installation_owned(run_log, tmp_path: Path) -> None:
    _request, generated = _materialized(tmp_path, run_log)
    configs = tmp_path / "installation"
    ctx = SimpleNamespace(
        paths=_Paths(configs, tmp_path / "outside" / "rules.yaml"),
    )

    with pytest.raises(InputError, match="inside the installation config root"):
        activate_generated_rule_set(ctx, run_log, generated.yaml_path)


def test_exactly_one_active_matching_identity_is_required(run_log, tmp_path: Path) -> None:
    request, generated = _materialized(tmp_path, run_log)
    item = deepcopy(generated.rule_set_item)
    source = request.source_paths[0]

    exact = resolve_active_rule_set_for_file(
        SimpleNamespace(file_operator_rule_sets=[item]),
        source,
    )
    assert exact.name == "monthly_statement__v0001"

    disabled = deepcopy(item)
    disabled["enabled"] = False
    disabled["canonicalization"]["rule_sha256"] = canonical_rule_sha256(disabled)
    with pytest.raises(InputError, match="found 0"):
        resolve_active_rule_set_for_file(
            SimpleNamespace(file_operator_rule_sets=[disabled]),
            source,
        )

    overlapping = deepcopy(item)
    overlapping["name"] = "overlapping_statement__v0001"
    overlapping["rule_set_name"] = "overlapping_statement"
    overlapping["canonicalization"]["rule_sha256"] = canonical_rule_sha256(overlapping)
    with pytest.raises(InputError, match="found 2"):
        resolve_active_rule_set_for_file(
            SimpleNamespace(file_operator_rule_sets=[item, overlapping]),
            source,
        )


def test_file_reference_requires_filename_and_header_when_both_are_present(run_log, 
    tmp_path: Path,
) -> None:
    request, generated = _materialized(tmp_path, run_log)
    source = request.source_paths[0]
    item = deepcopy(generated.rule_set_item)
    item["file_references"] = [
        {
            "name": "combined_identity",
            "file_pattern": "other_*.csv",
            "header": "AccountID,TradeDate",
        }
    ]
    item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)

    with pytest.raises(InputError, match="found 0"):
        resolve_active_rule_set_for_file(
            SimpleNamespace(file_operator_rule_sets=[item]),
            source,
        )

    item["file_references"][0]["file_pattern"] = "statement_*.csv"
    item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)
    matched = resolve_active_rule_set_for_file(
        SimpleNamespace(file_operator_rule_sets=[item]),
        source,
    )
    assert matched.name == "monthly_statement__v0001"


def test_future_file_executes_active_yaml_and_publishes_separate_tables(run_log, 
    tmp_path: Path,
) -> None:
    request, generated = _materialized(tmp_path, run_log)
    ctx = SimpleNamespace(file_operator_rule_sets=[generated.rule_set_item])
    source = next(
        path for path in request.source_paths if path.name == "statement_a.csv"
    )

    result = execute_active_rule_set(ctx, run_log,
        source,
        tmp_path / "future_outputs",
        tmp_path / "review",
    )

    assert result.rule_set.name == "monthly_statement__v0001"
    assert [path.name for path in result.output_paths] == [
        "statement_a.accounts.csv",
        "statement_a.securities.csv",
    ]
    assert result.output_paths[0].read_text(encoding="utf-8") == (
        "AccountID,TradeDate\n" "00123,2026-07-01\n" "00456,2026-07-02\n"
    )
    assert result.output_paths[1].read_text(encoding="utf-8") == (
        "SecurityID,Amount\n" "00007,10.00\n" "00008,20.00\n"
    )
    assert result.validation.status == "passed"
    assert result.evidence_path.exists()
    assert not (tmp_path / "review").exists()


def test_future_validation_failure_routes_review_without_outputs(run_log, 
    tmp_path: Path,
) -> None:
    request, generated = _materialized(tmp_path, run_log)
    ctx = SimpleNamespace(file_operator_rule_sets=[generated.rule_set_item])
    source = next(
        path for path in request.source_paths if path.name == "statement_a.csv"
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace("00007,10.00", "00007,11.00"),
        encoding="utf-8",
    )
    output_dir = tmp_path / "future_outputs"
    review_dir = tmp_path / "review"

    with pytest.raises(InputError, match="outputs were not published"):
        execute_active_rule_set(ctx, run_log, source, output_dir, review_dir)

    assert not output_dir.exists()
    review = review_dir / "statement_a.structural_review.json"
    assert review.exists()
    assert '"automatic_rule_regeneration": false' in review.read_text(encoding="utf-8")
    assert list(review_dir.glob("*.validation.json"))


def test_zero_match_routes_identity_failure_for_controlled_review(run_log, 
    tmp_path: Path,
) -> None:
    _request, generated = _materialized(tmp_path, run_log)
    ctx = SimpleNamespace(file_operator_rule_sets=[generated.rule_set_item])
    source = tmp_path / "unknown.csv"
    source.write_text("Wrong,Header\n1,2\n", encoding="utf-8")
    output_dir = tmp_path / "future_outputs"
    review_dir = tmp_path / "review"

    with pytest.raises(InputError, match="found 0"):
        execute_active_rule_set(ctx, run_log, source, output_dir, review_dir)

    assert not output_dir.exists()
    assert (review_dir / "unknown.structural_review.json").exists()
