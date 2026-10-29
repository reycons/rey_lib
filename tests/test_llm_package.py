"""Focused tests for run finalization and the log interpreter's package.

The log interpretation itself was removed with the legacy AI (#1025); #1284
restores it on the live AI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import make_run_log

from rey_lib.logs import (
    create_llm_package,
    create_results_summary,
    finalize_run_log,
)
from rey_lib.logs.record_enrichment import log_run_record
from rey_lib.data.errors import DataStructureError


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_completed_run(path: Path, records: list[dict]) -> None:
    """Emit a completed fixture through the hierarchy-stamped run-log writer."""
    first = records[0]
    run_log = make_run_log(
        Path(path).parent,
        path=str(path),
        app=first.get("app", ""),
        run_id=first["run_id"],
        run_timestamp=first["run_timestamp"],
        pipeline=first.get("pipeline_name", ""),
    )
    run_log.set_nest_level("pipeline")
    for source in records:
        fields = dict(source)
        record_type = fields.pop("record_type")
        for envelope_field in (
            "record_group", "run_id", "run_timestamp", "app", "pipeline_name",
        ):
            fields.pop(envelope_field, None)
        log_run_record(run_log, record_type, **fields)


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _result_types(path: Path) -> list[str]:
    stages = {"RESULTS_SUMMARY", "LLM_PACKAGE", "LLM_INTERPRETATION"}
    return [r["record_type"] for r in _records(path) if r["record_type"] in stages]


def _log(log_path: Path):
    """The run log that owns the log at ``log_path``.

    Identity comes from the log's own records: finalization continues a run that
    already exists, so it writes under that run's identity, not a fresh one.
    """
    identity = {}
    path = Path(log_path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                identity = json.loads(line)
                break
    return make_run_log(
        path.parent,
        path=str(path),
        app=str(identity.get("app") or "data_engine"),
        run_id=str(identity.get("run_id")
                   or "00000000-0000-4000-8000-000000000001"),
        run_timestamp=str(identity.get("run_timestamp") or "20260822_000000"),
        pipeline=identity.get("pipeline_name"),
        workflow=identity.get("workflow_name"),
    )


def _pkg(
    log_path: Path,
    source_record_type: str = "RESULTS_SUMMARY",
    package_record_type: str = "LLM_PACKAGE",
) -> dict:
    return create_llm_package(
        _log(log_path),
        source_record_type=source_record_type,
        package_record_type=package_record_type,
    )


#: Stands in for the runtime's AI. The analysis stage is handed one and never
#: discovers it, so a test that patches _ask only needs something that is not
#: None. A test of the no-AI path passes ai=None explicitly.
_AN_AI = object()


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
    _write_completed_run(log_path, _completed_records(config_path))
    return log_path, contract_path


def _prepared_run(tmp_path: Path) -> tuple[Path, Path, dict]:
    log_path, contract_path = _unfinalized_run(tmp_path)
    result = create_results_summary(_log(log_path))
    assert result["action"] == "created"
    return log_path, contract_path, result["summary"]


def _envelope(content: dict) -> str:
    return json.dumps({"artifact_type": "json", "content": content, "notes": []})


# ---------------------------------------------------------------------------
# finalize_run_log lifecycle
# ---------------------------------------------------------------------------

def test_finalize_writes_the_summary_and_package_and_no_interpretation(tmp_path) -> None:
    """Finalization ends at the package: log interpretation was removed (#1025, #1284).

    No interpretation is attempted, so neither an interpretation nor a failure
    record is written, and the run's own records are left as they were.
    """
    log_path, _contract_path = _unfinalized_run(tmp_path)
    before = [record["record_type"] for record in _records(log_path)]

    result = finalize_run_log(_log(log_path))

    types = [record["record_type"] for record in _records(log_path)]
    assert types[:len(before)] == before
    assert types[len(before):] == ["RESULTS_SUMMARY", "LLM_PACKAGE"]
    assert types.count("RUN_COMPLETE") == before.count("RUN_COMPLETE") == 1
    assert "LLM_INTERPRETATION" not in types
    assert "LLM_ANALYSIS_FAILURE" not in types
    assert result["analysis"] is None
    assert result["package"] is not None


def test_finalize_produces_no_email_stage_records(tmp_path) -> None:
    """The duplicate email stage is gone: it leaves no records behind."""
    log_path, _contract_path = _unfinalized_run(tmp_path)

    result = finalize_run_log(_log(log_path))

    types = [record["record_type"] for record in _records(log_path)]
    assert "LLM_EMAIL_PACKAGE" not in types
    assert "LLM_EMAIL_RESULT" not in types
    # The removed stage leaves no vestigial keys on the result either.
    assert "email_package" not in result
    assert "email_analysis" not in result
    assert "email_failures" not in result



def test_summary_failure_prevents_package_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.logs import llm_package, summary

    monkeypatch.setattr(
        summary,
        "create_results_summary",
        lambda *_args, **_kwargs: {"summary": None, "failures": ["failed"]},
    )
    called = False

    def fake_package(*_args, **_kwargs) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(llm_package, "create_llm_package", fake_package)

    result = finalize_run_log(_log(tmp_path / "run.jsonl"))

    assert result["package"] is None
    assert called is False


# ---------------------------------------------------------------------------
# create_llm_package (parameterized)
# ---------------------------------------------------------------------------

def test_package_pairs_resolved_contract_with_canonical_source(tmp_path: Path) -> None:
    log_path, _contract_path, summary = _prepared_run(tmp_path)

    package = _pkg(log_path)
    record = _records(log_path)[-1]

    # The contract is not here: it is the instruction the task resolves through
    # the canonical AI settings object, so embedding it would send it twice.
    assert package == {
        "analysis_name": "log_interpreter",
        "source_record_type": "RESULTS_SUMMARY",
        "source": summary,
    }
    assert record["record_type"] == "LLM_PACKAGE"
    assert record["record_group"] == "results"
    assert record["analysis_name"] == "log_interpreter"
    assert record["source_record_type"] == "RESULTS_SUMMARY"
    assert "instructions" not in record
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


def test_missing_source_record_fails_with_requested_type(tmp_path: Path) -> None:
    config_path, _contract_path = _configuration(tmp_path)
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_jsonl(log_path, _completed_records(config_path))

    with pytest.raises(DataStructureError, match="source record: RESULTS_SUMMARY"):
        _pkg(log_path)

    assert not any(record["record_type"] == "LLM_PACKAGE" for record in _records(log_path))


def test_repeated_creation_of_the_package_is_idempotent(tmp_path: Path) -> None:
    """A finalization that runs twice describes one package, not two."""
    log_path, _contract_path, _summary = _prepared_run(tmp_path)

    _pkg(log_path)
    _pkg(log_path)

    types = [record["record_type"] for record in _records(log_path)]
    assert types.count("LLM_PACKAGE") == 1
