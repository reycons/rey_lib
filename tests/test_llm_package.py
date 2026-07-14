"""Focused tests for configured LLM_PACKAGE run records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.logs import create_llm_package, create_results_summary, finalize_run_log


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _configuration(tmp_path: Path) -> tuple[Path, Path]:
    installation = tmp_path / "installation"
    shared = installation / "shared"
    contracts = tmp_path / "llmcontracts"
    shared.mkdir(parents=True)
    contracts.mkdir()

    config_path = installation / "installation.yaml"
    config_path.write_text(
        "paths:\n"
        "  - name: llmcontracts\n"
        f"    path: \"{contracts}\"\n",
        encoding="utf-8",
    )
    (shared / "log_analysis.yaml").write_text(
        "log_analysis:\n"
        "  log_interpreter:\n"
        "    enabled: true\n"
        "    engine: llm\n"
        "    contract: \"{llmcontracts}/rey_log_interpreter.yaml\"\n"
        "    llm_execution_profile: local_precision\n"
        "    fail_on_error: false\n"
        "    output:\n"
        "      destination: stdout\n"
        "      format: json\n"
        "      record_type: LLM_INTERPRETATION\n"
        "      record_group: results\n"
        "  alternate:\n"
        "    enabled: true\n"
        "    engine: llm\n"
        "    contract: \"{llmcontracts}/alternate.yaml\"\n"
        "    llm_execution_profile: local_precision\n",
        encoding="utf-8",
    )
    (contracts / "rey_log_interpreter.yaml").write_text(
        "name: rey_log_interpreter\nversion: 1\nrules:\n  - explain failures\n",
        encoding="utf-8",
    )
    (contracts / "alternate.yaml").write_text(
        "name: alternate\nversion: 2\n",
        encoding="utf-8",
    )
    return config_path, contracts / "rey_log_interpreter.yaml"


def _completed_records(config_path: Path) -> list[dict]:
    return [
        {
            "record_type": "RUN_START",
            "record_group": "execution",
            "run_id": "run-1",
            "run_timestamp": "20260714_120000",
            "run_started_at": "2026-07-14T12:00:00+00:00",
            "app": "demo",
            "pipeline_name": "demo_pipeline",
        },
        {
            "record_type": "CONFIG_FILE_REFERENCE",
            "record_group": "files",
            "run_id": "run-1",
            "run_timestamp": "20260714_120000",
            "path": str(config_path),
            "configuration_layer": "installation",
            "config_type": "installation",
            "load_order": 0,
        },
        {
            "record_type": "RUN_COMPLETE",
            "record_group": "execution",
            "run_id": "run-1",
            "run_timestamp": "20260714_120000",
            "status": "success",
            "timestamp": "2026-07-14T12:00:01+00:00",
        },
    ]


def _unfinalized_run(tmp_path: Path) -> tuple[Path, Path]:
    config_path, contract_path = _configuration(tmp_path)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_jsonl(log_path, _completed_records(config_path))
    return log_path, contract_path


def _prepared_run(tmp_path: Path) -> tuple[Path, Path, dict]:
    log_path, contract_path = _unfinalized_run(tmp_path)
    result = create_results_summary(log_path=log_path)
    assert result["action"] == "created"
    return log_path, contract_path, result["summary"]


def test_finalizer_calls_summary_then_package_with_same_log_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.logs import llm_package, summary

    log_path = tmp_path / "run.jsonl"
    calls: list[tuple[str, Path]] = []

    def fake_summary(*, log_path: Path) -> dict:
        calls.append(("summary", log_path))
        return {"summary": {"record_type": "RESULTS_SUMMARY"}}

    def fake_package(path: Path) -> dict:
        calls.append(("package", path))
        return {"instructions": {}, "results": {}}

    monkeypatch.setattr(summary, "create_results_summary", fake_summary)
    monkeypatch.setattr(llm_package, "create_llm_package", fake_package)

    finalize_run_log(log_path)

    assert calls == [("summary", log_path), ("package", log_path)]


def test_summary_failure_prevents_package_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.logs import llm_package, summary

    monkeypatch.setattr(
        summary,
        "create_results_summary",
        lambda **_kwargs: {"summary": None, "failures": ["failed"]},
    )
    called = False

    def fake_package(_path: Path) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(llm_package, "create_llm_package", fake_package)

    result = finalize_run_log(tmp_path / "run.jsonl")

    assert result["package"] is None
    assert called is False


def test_finalizer_appends_summary_then_package(tmp_path: Path) -> None:
    log_path, _contract_path = _unfinalized_run(tmp_path)

    finalize_run_log(log_path)

    assert [record["record_type"] for record in _records(log_path)[-2:]] == [
        "RESULTS_SUMMARY",
        "LLM_PACKAGE",
    ]


def test_package_failure_preserves_summary_and_completed_log(tmp_path: Path) -> None:
    log_path, contract_path = _unfinalized_run(tmp_path)
    contract_path.unlink()

    result = finalize_run_log(log_path)

    record_types = [record["record_type"] for record in _records(log_path)]
    assert result["package"] is None
    assert "Configured log_analysis contract" in result["package_failures"][0]
    assert record_types[-1] == "RESULTS_SUMMARY"
    assert "RUN_COMPLETE" in record_types
    assert "LLM_PACKAGE" not in record_types


def test_repeated_finalization_does_not_duplicate_records(tmp_path: Path) -> None:
    log_path, _contract_path = _unfinalized_run(tmp_path)

    finalize_run_log(log_path)
    finalize_run_log(log_path)

    record_types = [record["record_type"] for record in _records(log_path)]
    assert record_types.count("RESULTS_SUMMARY") == 1
    assert record_types.count("LLM_PACKAGE") == 1


def test_package_uses_resolved_log_analysis_contract_and_canonical_summary(
    tmp_path: Path,
) -> None:
    log_path, _contract_path, summary = _prepared_run(tmp_path)

    package = create_llm_package(log_path)
    record = _records(log_path)[-1]

    assert package == {
        "instructions": {
            "name": "rey_log_interpreter",
            "version": 1,
            "rules": ["explain failures"],
        },
        "results": summary,
    }
    assert record["record_type"] == "LLM_PACKAGE"
    assert record["record_group"] == "results"
    assert record["instructions"] == package["instructions"]
    assert record["results"] == summary


def test_canonical_writer_supplies_existing_run_identity_and_metadata(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)

    create_llm_package(log_path)
    record = _records(log_path)[-1]

    assert record["run_id"] == "run-1"
    assert record["run_timestamp"] == "20260714_120000"
    assert record["app"] == "demo"
    assert record["pipeline_name"] == "demo_pipeline"
    assert record["timestamp"]
    assert record["record_schema_version"] == 1


def test_requested_analysis_name_selects_that_resolved_configuration(tmp_path: Path) -> None:
    log_path, _contract_path, summary = _prepared_run(tmp_path)

    package = create_llm_package(log_path, analysis_name="alternate")

    assert package == {
        "instructions": {"name": "alternate", "version": 2},
        "results": summary,
    }


def test_missing_analysis_fails_without_inference_and_preserves_log(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    before = log_path.read_bytes()

    with pytest.raises(ValueError, match="log_analysis configuration not found"):
        create_llm_package(log_path, analysis_name="missing")

    assert log_path.read_bytes() == before


def test_missing_configured_contract_preserves_existing_summary(tmp_path: Path) -> None:
    log_path, contract_path, summary = _prepared_run(tmp_path)
    contract_path.unlink()
    before = log_path.read_bytes()

    with pytest.raises(FileNotFoundError, match="Configured log_analysis contract"):
        create_llm_package(log_path)

    assert log_path.read_bytes() == before
    assert _records(log_path)[-1] == summary


def test_package_requires_load_order_zero_installation_reference(tmp_path: Path) -> None:
    config_path, _contract_path = _configuration(tmp_path)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    records = _completed_records(config_path)
    records[1]["load_order"] = 1
    _write_jsonl(log_path, records)
    create_results_summary(log_path=log_path)

    with pytest.raises(ValueError, match="load-order-zero installation"):
        create_llm_package(log_path)


def test_package_requires_existing_results_summary(tmp_path: Path) -> None:
    config_path, _contract_path = _configuration(tmp_path)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_jsonl(log_path, _completed_records(config_path))

    with pytest.raises(ValueError, match="canonical RESULTS_SUMMARY"):
        create_llm_package(log_path)

    assert not any(record["record_type"] == "LLM_PACKAGE" for record in _records(log_path))


def test_repeated_creation_with_unchanged_inputs_is_idempotent(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)

    create_llm_package(log_path)
    create_llm_package(log_path)

    packages = [
        record for record in _records(log_path)
        if record["record_type"] == "LLM_PACKAGE"
    ]
    assert len(packages) == 1
