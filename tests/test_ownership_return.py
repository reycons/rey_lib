"""App-completion ownership-return regression tests
(SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction).

When an app descends into deeper scopes (workflow, workflow step, analysis) the
shared hierarchy state is left deep. run_app_operation must reassert the app base
before RUN_COMPLETE so completion records are emitted at the app level (3), not at
whatever deeper level the app body left behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import set_nest_level
from rey_lib.run_lifecycle import run_app_operation


def _ctx(tmp_path: Path) -> Namespace:
    return Namespace(
        {
            "run_log_dir": str(tmp_path),
            "app_name": "demo",
            "run_id": "run-demo",
            "run_timestamp": "20260101_000000",
        }
    )


def _run_complete_records(tmp_path: Path) -> list[dict]:
    log = next(tmp_path.glob("*.jsonl"))
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("record_type") == "RUN_COMPLETE"]


# Deeper app scope -> app RUN_COMPLETE returns to app level (success path).
def test_success_completion_returns_to_app_level(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    def func() -> int:
        set_nest_level(ctx, "workflow")       # 4
        set_nest_level(ctx, "workflow_step")  # 5 — left deep, no return
        return 0

    run_app_operation(ctx, "op", func)
    completes = _run_complete_records(tmp_path)
    assert completes and completes[-1]["nest_level"] == 3


# Deeper app scope + raising body -> failed RUN_COMPLETE still returns to app level.
def test_failed_completion_returns_to_app_level(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    def func() -> int:
        set_nest_level(ctx, "workflow")
        set_nest_level(ctx, "workflow_step")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_app_operation(ctx, "op", func)
    completes = _run_complete_records(tmp_path)
    assert completes and completes[-1]["nest_level"] == 3
