"""Focused tests for parameterized LLM package creation and configured execution."""

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
    run_configured_record_analysis,
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


def _result_types(path: Path) -> list[str]:
    stages = {
        "RESULTS_SUMMARY", "LLM_PACKAGE", "LLM_INTERPRETATION",
        "LLM_EMAIL_PACKAGE", "LLM_EMAIL_RESULT",
    }
    return [r["record_type"] for r in _records(path) if r["record_type"] in stages]


def _pkg(
    log_path: Path,
    analysis_name: str = "log_interpreter",
    source_record_type: str = "RESULTS_SUMMARY",
    package_record_type: str = "LLM_PACKAGE",
) -> dict:
    return create_llm_package(
        log_path,
        analysis_name=analysis_name,
        source_record_type=source_record_type,
        package_record_type=package_record_type,
    )


def _run(
    log_path: Path,
    analysis_name: str = "log_interpreter",
    package_record_type: str = "LLM_PACKAGE",
) -> dict:
    return run_configured_log_analysis(
        log_path, analysis_name=analysis_name, package_record_type=package_record_type,
    )


def _configuration(
    tmp_path: Path,
    *,
    interpreter_enabled: bool = True,
    email_contract: bool = True,
) -> tuple[Path, Path]:
    """Installation with log_interpreter, email_results, and alternate analyses.

    Includes llm_profiles and artifact_type so the same fixture serves both package
    creation (contract read) and configured execution (LLM call) tests.
    """
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
        f"    enabled: {'true' if interpreter_enabled else 'false'}\n"
        "    engine: llm\n"
        "    artifact_type: json\n"
        "    contract: \"{llmcontracts}/rey_log_interpreter.yaml\"\n"
        "    llm_execution_profile: local_precision\n"
        "    fail_on_error: false\n"
        "    output:\n"
        "      destination: stdout\n"
        "      format: json\n"
        "      record_type: LLM_INTERPRETATION\n"
        "      record_group: results\n"
        "  email_results:\n"
        "    enabled: true\n"
        "    engine: llm\n"
        "    artifact_type: json\n"
        "    contract: \"{llmcontracts}/email_results.yaml\"\n"
        "    llm_execution_profile: local_precision\n"
        "    fail_on_error: false\n"
        "    output:\n"
        "      destination: stdout\n"
        "      format: json\n"
        "      record_type: LLM_EMAIL_RESULT\n"
        "      record_group: results\n"
        "  alternate:\n"
        "    enabled: true\n"
        "    engine: llm\n"
        "    artifact_type: json\n"
        "    contract: \"{llmcontracts}/alternate.yaml\"\n"
        "    llm_execution_profile: local_precision\n",
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
    (contracts / "rey_log_interpreter.yaml").write_text(
        "name: rey_log_interpreter\nversion: 1\nrules:\n  - explain failures\n",
        encoding="utf-8",
    )
    if email_contract:
        (contracts / "email_results.yaml").write_text(
            "name: email_results\nversion: 1\nrules:\n  - render email\n",
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


def _unfinalized_run(tmp_path: Path, **config_kwargs) -> tuple[Path, Path]:
    config_path, contract_path = _configuration(tmp_path, **config_kwargs)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_jsonl(log_path, _completed_records(config_path))
    return log_path, contract_path


def _prepared_run(tmp_path: Path) -> tuple[Path, Path, dict]:
    log_path, contract_path = _unfinalized_run(tmp_path)
    result = create_results_summary(log_path=log_path)
    assert result["action"] == "created"
    return log_path, contract_path, result["summary"]


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


def _stage_direct_ask(monkeypatch) -> None:
    """direct_ask that returns the interpretation or email envelope per stage."""
    def fake(prompt, **_kwargs):
        if '"analysis_name": "email_results"' in prompt:
            return _envelope({"subject": "Run report", "html": "<p>ok</p>", "text": "ok"})
        return _envelope({"verdict": "ok"})
    monkeypatch.setattr("rey_lib.llm.llm_utils.direct_ask", fake)


# ---------------------------------------------------------------------------
# finalize_run_log lifecycle
# ---------------------------------------------------------------------------

def test_finalize_runs_both_stages_in_required_order(tmp_path, monkeypatch) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    _stage_direct_ask(monkeypatch)

    finalize_run_log(log_path)

    assert _result_types(log_path) == [
        "RESULTS_SUMMARY",
        "LLM_PACKAGE",
        "LLM_INTERPRETATION",
        "LLM_EMAIL_PACKAGE",
        "LLM_EMAIL_RESULT",
    ]
    email_result = _records(log_path)[-1]
    assert email_result["record_type"] == "LLM_EMAIL_RESULT"
    assert email_result["subject"] == "Run report"
    assert email_result["html"] == "<p>ok</p>"
    assert email_result["text"] == "ok"


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

    def fake_package(*_args, **_kwargs) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(llm_package, "create_llm_package", fake_package)

    result = finalize_run_log(tmp_path / "run.jsonl")

    assert result["package"] is None
    assert called is False


def test_email_package_not_created_when_interpretation_absent(
    tmp_path, monkeypatch,
) -> None:
    log_path, _contract_path = _unfinalized_run(tmp_path, interpreter_enabled=False)
    create_results_summary(log_path=log_path)
    _stage_direct_ask(monkeypatch)

    finalize_run_log(log_path)

    types = [r["record_type"] for r in _records(log_path)]
    # Interpreter disabled -> no LLM_INTERPRETATION -> email stage never starts.
    assert "LLM_INTERPRETATION" not in types
    assert "LLM_EMAIL_PACKAGE" not in types
    assert "LLM_EMAIL_RESULT" not in types


def test_email_execution_not_attempted_when_email_package_absent(
    tmp_path, monkeypatch,
) -> None:
    log_path, _contract_path = _unfinalized_run(tmp_path, email_contract=False)
    create_results_summary(log_path=log_path)
    _stage_direct_ask(monkeypatch)

    result = finalize_run_log(log_path)

    types = [r["record_type"] for r in _records(log_path)]
    # Missing email contract -> email package creation fails and is captured; no result.
    assert result["email_failures"]
    assert "LLM_EMAIL_PACKAGE" not in types
    assert "LLM_EMAIL_RESULT" not in types
    # Completed run and earlier results survive the later-stage failure.
    assert "RUN_COMPLETE" in types
    assert "RESULTS_SUMMARY" in types
    assert "LLM_INTERPRETATION" in types


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


def test_repeated_finalization_does_not_duplicate_records(tmp_path, monkeypatch) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    _stage_direct_ask(monkeypatch)

    finalize_run_log(log_path)
    finalize_run_log(log_path)

    types = [record["record_type"] for record in _records(log_path)]
    for stage in (
        "RESULTS_SUMMARY", "LLM_PACKAGE", "LLM_INTERPRETATION",
        "LLM_EMAIL_PACKAGE", "LLM_EMAIL_RESULT",
    ):
        assert types.count(stage) == 1


# ---------------------------------------------------------------------------
# create_llm_package (parameterized)
# ---------------------------------------------------------------------------

def test_package_pairs_resolved_contract_with_canonical_source(tmp_path: Path) -> None:
    log_path, _contract_path, summary = _prepared_run(tmp_path)

    package = _pkg(log_path)
    record = _records(log_path)[-1]

    assert package == {
        "analysis_name": "log_interpreter",
        "source_record_type": "RESULTS_SUMMARY",
        "instructions": {
            "name": "rey_log_interpreter",
            "version": 1,
            "rules": ["explain failures"],
        },
        "source": summary,
    }
    assert record["record_type"] == "LLM_PACKAGE"
    assert record["record_group"] == "results"
    assert record["analysis_name"] == "log_interpreter"
    assert record["source_record_type"] == "RESULTS_SUMMARY"
    assert record["instructions"] == package["instructions"]
    assert record["source"] == summary


def test_canonical_writer_supplies_existing_run_identity_and_metadata(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)

    _pkg(log_path)
    record = _records(log_path)[-1]

    assert record["run_id"] == "run-1"
    assert record["run_timestamp"] == "20260714_120000"
    assert record["app"] == "demo"
    assert record["pipeline_name"] == "demo_pipeline"
    assert record["timestamp"]
    assert record["record_schema_version"] == 1


def test_requested_analysis_name_selects_that_resolved_configuration(tmp_path: Path) -> None:
    log_path, _contract_path, summary = _prepared_run(tmp_path)

    package = _pkg(log_path, analysis_name="alternate")

    assert package == {
        "analysis_name": "alternate",
        "source_record_type": "RESULTS_SUMMARY",
        "instructions": {"name": "alternate", "version": 2},
        "source": summary,
    }


def test_email_package_uses_interpretation_source_and_configured_type(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    interpretation = {
        "record_type": "LLM_INTERPRETATION", "record_group": "results",
        "run_id": "run-1", "verdict": "ok",
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(interpretation) + "\n")

    package = _pkg(
        log_path,
        analysis_name="email_results",
        source_record_type="LLM_INTERPRETATION",
        package_record_type="LLM_EMAIL_PACKAGE",
    )
    record = _records(log_path)[-1]

    assert record["record_type"] == "LLM_EMAIL_PACKAGE"
    assert package["analysis_name"] == "email_results"
    assert package["source_record_type"] == "LLM_INTERPRETATION"
    assert package["instructions"] == {"name": "email_results", "version": 1,
                                       "rules": ["render email"]}
    assert package["source"] == interpretation


def test_missing_analysis_fails_without_inference_and_preserves_log(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    before = log_path.read_bytes()

    with pytest.raises(ValueError, match="log_analysis configuration not found"):
        _pkg(log_path, analysis_name="missing")

    assert log_path.read_bytes() == before


def test_missing_configured_contract_preserves_existing_summary(tmp_path: Path) -> None:
    log_path, contract_path, summary = _prepared_run(tmp_path)
    contract_path.unlink()
    before = log_path.read_bytes()

    with pytest.raises(FileNotFoundError, match="Configured log_analysis contract"):
        _pkg(log_path)

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
        _pkg(log_path)


def test_missing_source_record_fails_with_requested_type(tmp_path: Path) -> None:
    config_path, _contract_path = _configuration(tmp_path)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_jsonl(log_path, _completed_records(config_path))

    with pytest.raises(ValueError, match="source record: RESULTS_SUMMARY"):
        _pkg(log_path)

    assert not any(record["record_type"] == "LLM_PACKAGE" for record in _records(log_path))


def test_missing_email_source_record_names_that_type(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)

    with pytest.raises(ValueError, match="source record: LLM_INTERPRETATION"):
        _pkg(
            log_path,
            analysis_name="email_results",
            source_record_type="LLM_INTERPRETATION",
            package_record_type="LLM_EMAIL_PACKAGE",
        )


def test_repeated_creation_of_each_package_type_is_idempotent(tmp_path: Path) -> None:
    log_path, _contract_path, _summary = _prepared_run(tmp_path)
    interpretation = {"record_type": "LLM_INTERPRETATION", "record_group": "results",
                      "verdict": "ok"}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(interpretation) + "\n")

    _pkg(log_path)
    _pkg(log_path)
    _pkg(log_path, analysis_name="email_results",
         source_record_type="LLM_INTERPRETATION", package_record_type="LLM_EMAIL_PACKAGE")
    _pkg(log_path, analysis_name="email_results",
         source_record_type="LLM_INTERPRETATION", package_record_type="LLM_EMAIL_PACKAGE")

    types = [record["record_type"] for record in _records(log_path)]
    assert types.count("LLM_PACKAGE") == 1
    assert types.count("LLM_EMAIL_PACKAGE") == 1


# ---------------------------------------------------------------------------
# run_configured_log_analysis (parameterized)
# ---------------------------------------------------------------------------

def _analysis_config(
    tmp_path: Path,
    *,
    name: str = "log_interpreter",
    record_type: str = "LLM_INTERPRETATION",
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
        f"      record_type: {record_type}\n"
        "      record_group: results\n"
    )
    if output_path is not None:
        output += f'      path: "{output_path}"\n'
    (shared / "log_analysis.yaml").write_text(
        "log_analysis:\n"
        f"  {name}:\n"
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


def _package_log(
    tmp_path: Path,
    config_path: Path,
    *,
    with_package: bool = True,
    record_type: str = "LLM_PACKAGE",
    package_fields: dict | None = None,
    extra_records: list[dict] | None = None,
) -> Path:
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
        fields = package_fields or {
            "analysis_name": "log_interpreter",
            "source_record_type": "RESULTS_SUMMARY",
            "instructions": {"name": "rey_log_interpreter"},
            "source": {"status": "success"},
        }
        records.append({
            "record_type": record_type, "record_group": "results", "run_id": "run-1",
            "run_timestamp": "20260714_120000", **fields,
        })
    if extra_records:
        records.extend(extra_records)
    _write_jsonl(log_path, records)
    return log_path


def test_configured_analysis_reads_existing_package(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    captured: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=captured)
    _run(log_path)
    assert '"instructions"' in captured["prompt"]
    assert '"source"' in captured["prompt"]


def test_configured_analysis_uses_resolved_log_analysis_entry(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    assert _run(log_path, analysis_name="log_interpreter")["action"] == "written_stdout"
    with pytest.raises(ValueError, match="log_analysis configuration not found"):
        _run(log_path, analysis_name="missing")


def test_configured_analysis_uses_execution_profile(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    captured: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=captured)
    _run(log_path)
    assert captured["provider"] == "mock"
    assert captured["model"] == "mock-model"


def test_configured_analysis_calls_existing_llm_executor(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    called = {"n": 0}

    def fake(prompt, **_kwargs):
        called["n"] += 1
        return _envelope({"ok": True})

    monkeypatch.setattr("rey_lib.llm.llm_utils.direct_ask", fake)
    _run(log_path)
    assert called["n"] == 1


def test_package_record_type_controls_which_package_is_consumed(tmp_path, monkeypatch) -> None:
    config = _analysis_config(tmp_path)
    email_fields = {
        "analysis_name": "email_results", "source_record_type": "LLM_INTERPRETATION",
        "instructions": {"name": "email_results"}, "source": {"marker": "EMAIL_SOURCE"},
    }
    log_path = _package_log(
        tmp_path, config,
        extra_records=[{
            "record_type": "LLM_EMAIL_PACKAGE", "record_group": "results",
            "run_id": "run-1", **email_fields,
        }],
    )
    captured: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=captured)

    _run(log_path, package_record_type="LLM_EMAIL_PACKAGE")

    assert "EMAIL_SOURCE" in captured["prompt"]
    assert '"status": "success"' not in captured["prompt"]


def test_stdout_result_uses_configured_record_type_and_group(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, destination="stdout"))
    _patch_direct_ask(monkeypatch, response=_envelope({"verdict": "ok", "reasons": ["r"]}))
    _run(log_path)
    record = _records(log_path)[-1]
    assert record["record_type"] == "LLM_INTERPRETATION"
    assert record["record_group"] == "results"
    assert record["verdict"] == "ok"
    assert record["reasons"] == ["r"]


def test_email_result_embeds_subject_html_and_text(tmp_path, monkeypatch) -> None:
    config = _analysis_config(tmp_path, name="email_results", record_type="LLM_EMAIL_RESULT")
    email_fields = {
        "analysis_name": "email_results", "source_record_type": "LLM_INTERPRETATION",
        "instructions": {"name": "email_results"}, "source": {"verdict": "ok"},
    }
    log_path = _package_log(
        tmp_path, config, record_type="LLM_EMAIL_PACKAGE", package_fields=email_fields,
    )
    _patch_direct_ask(
        monkeypatch,
        response=_envelope({"subject": "S", "html": "<p>b</p>", "text": "b"}),
    )

    _run(log_path, analysis_name="email_results", package_record_type="LLM_EMAIL_PACKAGE")

    record = _records(log_path)[-1]
    assert record["record_type"] == "LLM_EMAIL_RESULT"
    assert record["subject"] == "S"
    assert record["html"] == "<p>b</p>"
    assert record["text"] == "b"


def test_file_result_uses_existing_writer_and_configured_path(tmp_path, monkeypatch) -> None:
    out_file = tmp_path / "interpretation.json"
    log_path = _package_log(
        tmp_path, _analysis_config(tmp_path, destination="file", output_path=out_file)
    )
    _patch_direct_ask(monkeypatch, response=_envelope({"verdict": "ok"}))
    _run(log_path)
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
    out = _run(log_path)
    assert out["skipped"] == ["disabled"]
    assert called["n"] == 0
    assert not any(r["record_type"] == "LLM_INTERPRETATION" for r in _records(log_path))


def test_missing_package_record_fails_explicitly(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path), with_package=False)
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    with pytest.raises(ValueError, match="package record: LLM_PACKAGE"):
        _run(log_path)


def test_nonfatal_llm_failure_writes_failure_record(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, fail_on_error=False))
    before = _records(log_path)
    _patch_direct_ask(monkeypatch, raises=ProviderFailure("boom"))
    out = _run(log_path)
    assert out["result"] is None
    assert out["failures"]
    after = _records(log_path)
    # Prior records are preserved; one canonical failure record is appended, with the
    # full error scope shaped by error_utils (type, message, exception, traceback).
    assert after[:len(before)] == before
    failure = after[-1]
    assert failure["record_type"] == "LLM_INTERPRETATION"
    assert failure["record_group"] == "results"
    assert failure["status"] == "failed"
    assert failure["error_type"] == "ProviderFailure"
    assert failure["error_message"]
    assert failure["sanitized_traceback"]
    assert failure["analysis_name"] == "log_interpreter"


def test_nonfatal_parse_failure_writes_failure_record(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, fail_on_error=False))
    # A valid envelope whose json artifact is not decodable JSON triggers a parse failure.
    _patch_direct_ask(
        monkeypatch,
        response=json.dumps({"artifact_type": "json", "content": "not-json", "notes": []}),
    )
    out = _run(log_path)
    assert out["result"] is None
    assert out["failures"]
    assert _records(log_path)[-1]["status"] == "failed"


def test_fail_on_error_true_records_and_reraises(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path, fail_on_error=True))
    _patch_direct_ask(monkeypatch, raises=ProviderFailure("boom"))
    with pytest.raises(ProviderFailure):
        _run(log_path)


def test_repeated_execution_does_not_duplicate_result(tmp_path, monkeypatch) -> None:
    log_path = _package_log(tmp_path, _analysis_config(tmp_path))
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}))
    _run(log_path)
    _run(log_path)
    assert sum(1 for r in _records(log_path) if r["record_type"] == "LLM_INTERPRETATION") == 1


# ---------------------------------------------------------------------------
# run_configured_record_analysis — on-demand analysis of one supplied record
# (SGC_Rey_Console_JSON_Record_LLM_Explanation)
# ---------------------------------------------------------------------------

def _record_ctx(tmp_path: Path):
    """A resolved installation ctx carrying log_analysis and llm_profiles."""
    from rey_lib.config.config_utils import build_ctx_from_path

    config_path, _ = _configuration(tmp_path)
    return build_ctx_from_path(config_path, full_installation=True)


def test_record_analysis_runs_the_configured_analysis_over_a_supplied_record(
    tmp_path: Path, monkeypatch
) -> None:
    """The supplied record is packaged as the source and its result returned."""
    capture: dict = {}
    _patch_direct_ask(
        monkeypatch,
        response=_envelope({"subject": "Run report", "html": "<p>ok</p>", "text": "ok"}),
        capture=capture,
    )
    record = {"record_type": "LLM_INTERPRETATION", "verdict": "failed"}
    result = run_configured_record_analysis(_record_ctx(tmp_path), record, "email_results")

    assert result["action"] == "analysed"
    assert result["result"] == {"subject": "Run report", "html": "<p>ok</p>", "text": "ok"}
    # The configured analysis, its contract, and its profile are all reused. The
    # prompt is the serialized package followed by the shared envelope instruction.
    package, _ = json.JSONDecoder().raw_decode(capture["prompt"])
    assert package["analysis_name"] == "email_results"
    assert package["source"] == record                       # the exact record supplied
    assert package["source_record_type"] == "LLM_INTERPRETATION"
    assert package["instructions"]["name"] == "email_results"
    assert capture["provider"] == "mock" and capture["model"] == "mock-model"


def test_record_analysis_writes_nothing_and_reads_no_log(tmp_path: Path, monkeypatch) -> None:
    """The on-demand path touches no run log: nothing is read and nothing appended."""
    _patch_direct_ask(monkeypatch, response=_envelope({"subject": "s", "html": "<p>h</p>"}))
    ctx = _record_ctx(tmp_path)
    before = sorted(str(p) for p in tmp_path.rglob("*") if p.is_file())
    run_configured_record_analysis(ctx, {"any": "record"}, "email_results")
    assert sorted(str(p) for p in tmp_path.rglob("*") if p.is_file()) == before


def test_record_analysis_source_type_defaults_to_the_record_type(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicit source type wins; otherwise the record's own type is declared."""
    capture: dict = {}
    _patch_direct_ask(monkeypatch, response=_envelope({"ok": True}), capture=capture)
    ctx = _record_ctx(tmp_path)
    run_configured_record_analysis(ctx, {"record_type": "SOME_RESULT"}, "email_results")
    assert '"source_record_type": "SOME_RESULT"' in capture["prompt"]
    run_configured_record_analysis(
        ctx, {"record_type": "SOME_RESULT"}, "email_results", source_record_type="OVERRIDE"
    )
    assert '"source_record_type": "OVERRIDE"' in capture["prompt"]


def test_record_analysis_requires_a_json_object_record(tmp_path: Path, monkeypatch) -> None:
    """A non-object record is rejected before any provider contact."""
    _patch_direct_ask(monkeypatch, raises=AssertionError("provider must not be called"))
    ctx = _record_ctx(tmp_path)
    for value in ("a string", [1, 2], 42, None):
        with pytest.raises(ValueError, match="requires a JSON object record"):
            run_configured_record_analysis(ctx, value, "email_results")


def test_record_analysis_enforces_the_input_size_limit(tmp_path: Path, monkeypatch) -> None:
    """An oversized package is rejected before any provider contact."""
    _patch_direct_ask(monkeypatch, raises=AssertionError("provider must not be called"))
    with pytest.raises(ValueError, match="over the configured limit"):
        run_configured_record_analysis(
            _record_ctx(tmp_path), {"big": "x" * 500}, "email_results",
            max_input_characters=50,
        )


def test_record_analysis_skips_a_disabled_analysis(tmp_path: Path, monkeypatch) -> None:
    """A disabled analysis performs no provider call and reports why."""
    _patch_direct_ask(monkeypatch, raises=AssertionError("provider must not be called"))
    config_path, _ = _configuration(tmp_path, interpreter_enabled=False)
    from rey_lib.config.config_utils import build_ctx_from_path

    ctx = build_ctx_from_path(config_path, full_installation=True)
    result = run_configured_record_analysis(ctx, {"any": "record"}, "log_interpreter")
    assert result["action"] == "skipped"
    assert result["skipped"] == ["disabled"]
    assert result["result"] is None


def test_record_analysis_unconfigured_analysis_is_a_configuration_failure(
    tmp_path: Path,
) -> None:
    """An unknown analysis name fails closed as configuration."""
    from rey_lib.llm.exceptions import ConfigurationFailure

    with pytest.raises(ConfigurationFailure, match="log_analysis configuration not found"):
        run_configured_record_analysis(_record_ctx(tmp_path), {"a": 1}, "no_such_analysis")


def test_record_analysis_missing_contract_is_reported(tmp_path: Path) -> None:
    """A configured contract that is absent is reported, not silently skipped."""
    config_path, _ = _configuration(tmp_path, email_contract=False)
    from rey_lib.config.config_utils import build_ctx_from_path

    ctx = build_ctx_from_path(config_path, full_installation=True)
    with pytest.raises(FileNotFoundError, match="Configured log_analysis contract"):
        run_configured_record_analysis(ctx, {"a": 1}, "email_results")


def test_record_analysis_provider_failure_propagates(tmp_path: Path, monkeypatch) -> None:
    """Provider failures reach the caller, which owns presentation."""
    _patch_direct_ask(monkeypatch, raises=ProviderFailure("model unavailable"))
    with pytest.raises(ProviderFailure, match="model unavailable"):
        run_configured_record_analysis(_record_ctx(tmp_path), {"a": 1}, "email_results")
