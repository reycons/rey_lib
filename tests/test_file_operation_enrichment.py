"""Ambient FILE_OPERATION records get standard enrichment.

File operations recorded through the bound run (the ambient file_utils path) must
receive the same execution-context enrichment — app identity, pipeline_name,
workflow_name — as any other typed run-log record, rather than being written
through an identity-less bound run.
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.config.config_utils import Namespace
from rey_lib.logs.log_utils import bind_run, clear_run
from rey_lib.logs.file_records import record_file_operation


def _file_ops(log: Path) -> list[dict]:
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("record_type") == "FILE_OPERATION"]


def test_ambient_file_operation_receives_app_and_context(tmp_path: Path) -> None:
    log = tmp_path / "app.ts.jsonl"
    ctx = Namespace(
        {
            "run_log_path": str(log),
            "run_id": "r",
            "run_timestamp": "ts",
            "app_name": "rey_analyzer",
            "pipeline_name": "trade_analyzer_generate_apply_ddl",
        }
    )
    bind_run(ctx)
    try:
        record_file_operation(
            "read", source_path=str(tmp_path / "x"), target_path=str(tmp_path / "y")
        )
    finally:
        clear_run()

    ops = _file_ops(log)
    assert ops, "an ambient FILE_OPERATION should have been recorded"
    assert ops[-1]["app"] == "rey_analyzer"
    assert ops[-1]["pipeline_name"] == "trade_analyzer_generate_apply_ddl"


def test_ambient_file_operation_omits_absent_identity(tmp_path: Path) -> None:
    # No app identity on the ctx -> app is simply absent, exactly as before.
    log = tmp_path / "app.ts.jsonl"
    ctx = Namespace({"run_log_path": str(log), "run_id": "r", "run_timestamp": "ts"})
    bind_run(ctx)
    try:
        record_file_operation("read", source_path=str(tmp_path / "x"))
    finally:
        clear_run()

    ops = _file_ops(log)
    assert ops and "app" not in ops[-1]
