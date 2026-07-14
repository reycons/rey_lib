"""Focused tests for configured LLM_PACKAGE run records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.llm.exceptions import ProviderFailure
from rey_lib.logs import (
    create_llm_package,
    create_results_summary,
    finalize_run_log,
    run_configured_log_analysis,
)


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


def test_finalize_run_log_order_is_summary_package_result(
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

    def fake_analysis(path: Path) -> dict:
        calls.append(("analysis", path))
        return {"result": None, "action": None}

    monkeypatch.setattr(summary, "create_results_summary", fake_summary)
    monkeypatch.setattr(llm_package, "create_llm_package", fake_package)
    monkeypatch.setattr(llm_package, "run_configured_log_analysis", fake_analysis)

    finalize_run_log(log_path)

    assert calls == [
        ("summary", log_path),
        ("package", log_path),
        ("analysis", log_path),
    ]


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


# ---------------------------------------------------------------------------
# run_configured_log_analysis (SGC_Rey_Lib_Run_Configured_Log_Analysis)
# ---------------------------------------------------------------------------

def _analysis_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    fail_on_error: bool = False,
    destination: str = "stdout",
    output_path: Path | None = None,
) -> Path:
    installation = tmp_path / "installation"
    shared = installation / "shared"
    shared.mkdir(parents=True)
    config_path = installation / "installation.yaml"
    config_path.write_text("installation:\n  name: demo\n", encoding="utf-8")
    output = (
        f"      destination: {destination}\n"
        "      format: JSON\n"
        "      record_type: LLM_INTERPRETATION\n"
        "      record_group: results\n"
    )
    if output_path is not None:
        output += f'      path: "{output_path}"\n'
    (shared / "log_analysis.yaml").write_text(
        "log_analysis:\n"
        "  log_interpreter:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    engine: llm\n"
        "    artifact_type: json\n"
        "    llm_execution_profile: local_precision\n"
        f"    fail_on_error: {'true' if fail_on_error else 'false'}\n"
        "    output:\n" + output,
        encoding="utf-8",
    )
    (shared / "llm_profiles.yaml").write_text(
        "llm_profiles:\n"
        "  - name: local_precision\n"
        "    provider: mock\n"
        "    model: mock-model\n"
        '    api_key: ""\n',
        encoding="utf-8",
    )
    return config_path


def _package_log(tmp_path: Path, config_path: Path, *, with_package: bool = True) -> Path:
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    records = [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "run-1",
         "run_timestamp": "20260714_120000", "app": "demo", "pipeline_name": "demo_pipeline"},
        {"record_type": "CONFIG_FILE_REFERENCE", "record_group": "files", "run_id": "run-1",
         "run_timestamp": "20260714_120000", "path": str(config_path),
         "configuration_layer": "installation", "config_type": "installation", "load_order": 0},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "run-1",
         "run_timestamp": "20260714_120000", "status": "success"},
    ]
    if with_package:
        records.append({
            "record_type": "LLM_PACKAGE", "record_group": "results", "run_id": "run-1",
            "run_timestamp": "20260714_120000",
            "instructions": {"name": "rey_log_interpreter"}, "results": {"status": "success"},
        })
    _write_jsonl(log_path, records)
    return log_path


def _envelope(content: dict) -> str:
    return json.dumps({"artifact_type": "json", "content": content, "notes": []})


def _patch_direct_ask(monkeypatch, *, response=None, capture=None, raises=None) -> None:
    def fake(prompt, *, model, provider, api_key, **_kwargs):
        if capture is not None:
            capture.update({"prompt": prompt, "model": model, "provider": provider})
        if raises is not None:
            raise raises
        return response
    monkeypatch.setattr("rey_lib.llm.llm_utils.direct_ask", fake)


def test_configured_analysis_reads_existing_llm_package(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    captured: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=captured)
    run_configured_log_analysis(log_path)
    assert '"instructions"' in captured["prompt"]
    assert '"results"' in captured["prompt"]


def test_configured_analysis_uses_resolved_log_analysis_entry(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    assert run_configured_log_analysis(log_path, analysis_name="log_interpreter")["action"] == "written_stdout"
    with pytest.raises(ValueError, match="log_analysis configuration not found"):
        run_configured_log_analysis(log_path, analysis_name="missing")


def test_configured_analysis_uses_execution_profile(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    captured: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=captured)
    run_configured_log_analysis(log_path)
    assert captured["provider"] == "mock"
    assert captured["model"] == "mock-model"


def test_configured_analysis_calls_existing_llm_executor(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    called = {"n": 0}

    def fake(prompt, **_kwargs):
        called["n"] += 1
        return _envelope({"ok": True})

    monkeypatch.setattr("rey_lib.llm.llm_utils.direct_ask", fake)
    run_configured_log_analysis(log_path)
    assert called["n"] == 1


def test_stdout_result_uses_configured_record_type_and_group(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, destination="stdout"))
    _patch_direct_ask(monkeypatch, response=_envelope({"verdict": "ok", "reasons": ["r"]}))
    run_configured_log_analysis(log_path)
    record = _records(log_path)[-1]
    assert record["record_type"] == "LLM_INTERPRETATION"
    assert record["record_group"] == "results"
    assert record["verdict"] == "ok"
    assert record["reasons"] == ["r"]


def test_file_result_uses_existing_writer_and_configured_path(tmp_path, monkeypatch) -> None:
    out_file = tmp_path / "interpretation.json"
    log_path = _package_log(
        tmp_path, _analysis_config(tmp_path, destination="file", output_path=out_file)
    )
    _patch_direct_ask(monkeypatch, response=_envelope({"verdict": "ok"}))
    run_configured_log_analysis(log_path)
    assert out_file.is_file()
    assert json.loads(out_file.read_text(encoding="utf-8")) == {"verdict": "ok"}
    assert not any(r["record_type"] == "LLM_INTERPRETATION" for r in _records(log_path))


def test_disabled_analysis_is_skipped(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, enabled=False))
    called = {"n": 0}
    monkeypatch.setattr(
        "rey_lib.llm.llm_utils.direct_ask",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    out = run_configured_log_analysis(log_path)
    assert out["skipped"] == ["disabled"]
    assert called["n"] == 0
    assert not any(r["record_type"] == "LLM_INTERPRETATION" for r in _records(log_path))


def test_missing_llm_package_fails_explicitly(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path), with_package=False)
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    with pytest.raises(ValueError, match="LLM_PACKAGE"):
        run_configured_log_analysis(log_path)


def test_nonfatal_llm_failure_preserves_existing_records(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, fail_on_error=False))
    before = log_path.read_bytes()
    _patch_direct_ask(monkeypatch, raises=ProviderFailure("boom"))
    out = run_configured_log_analysis(log_path)
    assert out["result"] is None
    assert out["failures"]
    assert log_path.read_bytes() == before


def test_fail_on_error_true_propagates(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, fail_on_error=True))
    _patch_direct_ask(monkeypatch, raises=ProviderFailure("boom"))
    with pytest.raises(ProviderFailure):
        run_configured_log_analysis(log_path)


def test_repeated_finalization_does_not_duplicate_result(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    run_configured_log_analysis(log_path)
    run_configured_log_analysis(log_path)
    assert sum(1 for r in _records(log_path) if r["record_type"] == "LLM_INTERPRETATION") == 1
