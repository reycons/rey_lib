"""Run finalization ends at the summary; the package is AI's own evidence (#1305).

Finalization appends the deterministic RESULTS_SUMMARY and nothing else: no
LLM_PACKAGE, no interpretation, no AI. The package the finalization stage used to
write is now built by the AI at the run it is asked for -- the task's contract
beside the record it was sent, in the same structure and field names -- and
``rey_lib.ai.evidence.package_of`` is what builds it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import make_run_log

from rey_lib.ai.evidence import package_of
from rey_lib.logs import create_results_summary, finalize_run_log
from rey_lib.logs.record_enrichment import log_run_record


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


def _log(log_path: Path):
    """The run log that owns the log at ``log_path``: finalization continues the
    run the log's own records name."""
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


def _completed_records() -> list[dict]:
    base = {"run_id": "run-1", "run_timestamp": "20260714_120000", "app": "demo",
            "pipeline_name": "demo_pipeline"}
    return [
        {**base, "record_type": "RUN_START", "record_group": "execution"},
        {**base, "record_type": "STEP_START", "record_group": "execution",
         "step_id": "s1", "step_name": "Step one"},
        {**base, "record_type": "STEP_END", "record_group": "execution",
         "step_id": "s1", "step_name": "Step one", "status": "succeeded"},
        {**base, "record_type": "RUN_COMPLETE", "record_group": "execution",
         "status": "succeeded"},
    ]


def _unfinalized_run(tmp_path: Path) -> Path:
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_completed_run(log_path, _completed_records())
    return log_path


# ---------------------------------------------------------------------------
# finalize_run_log lifecycle (#1305): the summary, and nothing else
# ---------------------------------------------------------------------------

def test_finalize_writes_the_summary_and_nothing_else(tmp_path: Path) -> None:
    log_path = _unfinalized_run(tmp_path)
    before = [record["record_type"] for record in _records(log_path)]

    result = finalize_run_log(_log(log_path))

    types = [record["record_type"] for record in _records(log_path)]
    assert types[:len(before)] == before
    assert types[len(before):] == ["RESULTS_SUMMARY"]
    assert types.count("RUN_COMPLETE") == before.count("RUN_COMPLETE") == 1
    assert "LLM_PACKAGE" not in types
    assert "LLM_INTERPRETATION" not in types
    assert "LLM_ANALYSIS_FAILURE" not in types
    assert result["summary"] is not None
    assert result["package"] is None
    assert result["analysis"] is None


def test_finalizing_twice_returns_the_one_summary(tmp_path: Path) -> None:
    log_path = _unfinalized_run(tmp_path)

    first = finalize_run_log(_log(log_path))
    second = finalize_run_log(_log(log_path))

    types = [record["record_type"] for record in _records(log_path)]
    assert types.count("RESULTS_SUMMARY") == 1
    assert second["summary"] == first["summary"]
    assert "already_summarized" in second["skipped"]


def test_a_run_with_no_terminal_record_is_not_summarized(tmp_path: Path) -> None:
    log_path = tmp_path / "demo.20260714_120000.jsonl"
    _write_completed_run(log_path, _completed_records()[:-1])

    result = finalize_run_log(_log(log_path))

    assert result["summary"] is None and result["package"] is None
    assert "no_terminal_record" in result["skipped"]
    assert not any(r["record_type"] == "RESULTS_SUMMARY" for r in _records(log_path))


def test_finalization_calls_no_ai_and_builds_no_package() -> None:
    """The finalizer reaches nothing of the AI subsystem and no package builder."""
    import inspect

    from rey_lib.logs import summary

    source = inspect.getsource(summary.finalize_run_log)
    assert "llm_package" not in source
    assert "ApplicationAI" not in source
    assert "LLM_PACKAGE" not in source
    assert not hasattr(summary, "create_llm_package")


# ---------------------------------------------------------------------------
# The package AI records (#1305): the original structure, built from the task
# ---------------------------------------------------------------------------

CONTRACT = ("contract:\n  name: rey_log_interpreter\n  version: 1.3.1\n"
            "purpose: Interpret a run.\nreferences: []\n")


def _task(contract_body: str = CONTRACT) -> SimpleNamespace:
    return SimpleNamespace(task_key="log_interpretation", instruction_key="log_interpreter",
                           contract_key="rey_log_interpreter", contract_version="1.3.1",
                           contract_body=contract_body)


def test_the_package_pairs_the_tasks_contract_with_the_source_record(tmp_path: Path) -> None:
    log_path = _unfinalized_run(tmp_path)
    summary = create_results_summary(_log(log_path))["summary"]

    package = package_of(_task(), summary, "RESULTS_SUMMARY", runtime=None)

    assert list(package) == ["analysis_name", "source_record_type", "instructions", "source",
                             "task_key"]
    assert package["task_key"] == "log_interpretation"
    assert package["analysis_name"] == "log_interpreter"
    assert package["source_record_type"] == "RESULTS_SUMMARY"
    assert package["instructions"] == {
        "contract_key": "rey_log_interpreter", "contract_version": "1.3.1",
        "contract": {"name": "rey_log_interpreter", "version": "1.3.1"},
        "purpose": "Interpret a run.", "references": [],
    }
    assert package["source"] == summary


def test_a_contract_that_is_not_yaml_travels_as_its_text() -> None:
    package = package_of(_task("Explain it: {not: [yaml"), None, "", runtime=None)

    assert package["instructions"] == {"contract_key": "rey_log_interpreter",
                                       "contract_version": "1.3.1",
                                       "body": "Explain it: {not: [yaml"}
    assert package["source"] is None
    assert package["source_record_type"] == ""


def test_declared_references_are_loaded_through_the_one_loader(tmp_path: Path) -> None:
    reference = tmp_path / "guide.md"
    reference.write_text("Read this first.", encoding="utf-8")
    body = ("contract:\n  name: rey_log_interpreter\n  version: 1.3.1\n"
            f"references:\n  - name: guide\n    role: guidance\n    path: {reference}\n")
    runtime = SimpleNamespace(paths=None)

    package = package_of(_task(body), {"a": 1}, "RESULTS_SUMMARY", runtime=runtime)

    assert package["references"] == [{"name": "guide", "role": "guidance",
                                      "content": "Read this first."}]
    assert list(package) == ["analysis_name", "source_record_type", "instructions",
                             "references", "source", "task_key"]
