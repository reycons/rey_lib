"""Tests for shared per-run hierarchy state
(SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction).

The complete hierarchy state lives in one companion file derived deterministically
from the run log path, so processes writing the same run log — the pipeline and the
app subprocesses it invokes — share one authoritative state. A separate ctx that
carries the same ``run_log_path`` models a subprocess: it resolves the same
companion file and continues the existing state rather than restarting.
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import set_nest_level
from rey_lib.logs import record_parenting as rp
from rey_lib.logs import run_state


def _ctx(run_log_path: Path) -> Namespace:
    """A ctx carrying only run_log_path — the sole channel a subprocess inherits."""
    return Namespace({"run_log_path": str(run_log_path)})


def _write(ctx, nest_level, *, fail=False) -> dict:
    """Stamp a record; commit only on a successful append (fail=False)."""
    record: dict = {}
    record_id = rp.stamp_record(ctx, record, nest_level)
    if not fail:
        rp.commit_record(ctx, record_id, nest_level)
    return record


# TEST-001 — standalone run creates a companion state file and starts at id 1.
def test_standalone_run_creates_companion_and_starts_at_one(tmp_path: Path) -> None:
    log = tmp_path / "app.20260101_000000.jsonl"
    ctx = _ctx(log)
    set_nest_level(ctx, "app")
    rec = _write(ctx, 3)
    assert rec["record_id"] == 1
    assert run_state.companion_path(str(log)).exists()


# TEST-002 — one physical run log has exactly one companion state file.
def test_one_companion_file_per_run_log(tmp_path: Path) -> None:
    log = tmp_path / "pipe.20260101_000000.jsonl"
    ctx = _ctx(log)
    set_nest_level(ctx, "pipeline")
    _write(ctx, 1)
    set_nest_level(ctx, "pipeline_step")
    _write(ctx, 2)
    assert len(list(tmp_path.glob("*.hstate.json"))) == 1


# TEST-003 — a subprocess derives the same companion path from the inherited run log.
def test_subprocess_derives_same_companion_path(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    pipe, app = _ctx(log), _ctx(log)
    assert run_state.companion_path(str(pipe.run_log_path)) == run_state.companion_path(
        str(app.run_log_path)
    )


# TEST-004 / TEST-005 — cross-process record-id and parent continuation.
def test_app_continues_sequence_and_parent_across_processes(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    pipe = _ctx(log)
    set_nest_level(pipe, "pipeline")       # 1
    _write(pipe, 1)                        # id 1
    set_nest_level(pipe, "pipeline_step")  # 2, parent = pipeline record
    step = _write(pipe, 2)                 # id 2 -> persisted last=2

    app = _ctx(log)                        # new process, same run log
    set_nest_level(app, "app")             # inherits nest 2 -> descent to 3
    first = _write(app, 3)
    assert first["record_id"] == 3         # N+1, not restarting at 1
    assert first["parent_record_id"] == step["record_id"]  # under the pipeline step


# TEST-006 — the pipeline's next write continues from the app's final state.
def test_pipeline_continues_after_app(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    pipe = _ctx(log)
    set_nest_level(pipe, "pipeline")
    _write(pipe, 1)
    set_nest_level(pipe, "pipeline_step")
    _write(pipe, 2)

    app = _ctx(log)
    set_nest_level(app, "app")
    _write(app, 3)                         # id 3, app advances shared last to 3

    set_nest_level(pipe, "pipeline_step")  # pipeline resumes, reads app's state
    nxt = _write(pipe, 2)
    assert nxt["record_id"] == 4           # continues after the app


# TEST-007 — state comes from the companion file, never from JSONL reconstruction.
def test_state_from_companion_not_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    ctx = _ctx(log)
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    # No JSONL was written in this unit test; sequencing still works from the file.
    assert not log.exists()
    assert run_state.companion_path(str(log)).exists()


# TEST-008 — no authoritative hierarchy state is retained on a file-backed ctx.
def test_no_authoritative_state_on_ctx_when_file_backed(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    ctx = _ctx(log)
    set_nest_level(ctx, "app")
    _write(ctx, 3)
    assert getattr(ctx, "_rey_nest_level", None) is None
    assert getattr(ctx, "_rey_last_record_id", None) is None
    assert getattr(ctx, "_rey_level_parents", None) is None
    assert getattr(ctx, "_rey_run_state", None) is None  # file-backed, not in-memory


# Successful-append rule survives the process boundary: a failed append does not
# advance the shared last_record_id.
def test_failed_append_does_not_advance_shared_state(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    ctx = _ctx(log)
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    _write(ctx, 3, fail=True)              # stamped, not committed

    app = _ctx(log)                        # another process sees last still 1
    set_nest_level(app, "app")
    assert _write(app, 3)["record_id"] == 2


# Create-if-absent only: resolution never overwrites an existing state file.
def test_existing_state_file_is_not_overwritten(tmp_path: Path) -> None:
    log = tmp_path / "pipe.ts.jsonl"
    companion = run_state.companion_path(str(log))
    companion.write_text(
        json.dumps(
            {
                "last_record_id": 7,
                "current_nest_level": 3,
                "current_parent_record_id": 5,
                "level_parents": {"0": 0, "3": 5},
            }
        ),
        encoding="utf-8",
    )
    ctx = _ctx(log)
    rec = _write(ctx, 3)                    # continues from the existing file
    assert rec["record_id"] == 8
    assert rec["parent_record_id"] == 5
