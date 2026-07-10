"""Tests for the canonical pipeline run-state path helpers
(SGC_Rey_Lib_Pipeline_Run_State_Flat_Step_Context_Files)."""

from __future__ import annotations

from pathlib import Path

from rey_lib.run_lifecycle import pipeline_run_ctx_path, pipeline_run_dir


def test_pipeline_run_dir_is_run_scoped() -> None:
    """The run directory is namespaced by run id and pipeline name for isolation."""
    state_dir = Path("/state")
    run_dir = pipeline_run_dir(state_dir, "20260710_120000", "trade_pipeline")
    assert run_dir == state_dir / "pipeline_runs" / "20260710_120000" / "trade_pipeline"


def test_pipeline_run_ctx_path_is_flat_step_named_file() -> None:
    """A step's context is a flat ``<step_id>.ctx.json`` file directly under the run
    directory — never a per-step directory containing ``ctx.json``."""
    state_dir = Path("/state")
    path = pipeline_run_ctx_path(
        state_dir, "20260710_120000", "trade_pipeline", "prepare_trade_files"
    )
    assert path == (
        state_dir / "pipeline_runs" / "20260710_120000" / "trade_pipeline"
        / "prepare_trade_files.ctx.json"
    )
    # The context file sits directly in the run directory (its parent is the run dir).
    assert path.parent == pipeline_run_dir(state_dir, "20260710_120000", "trade_pipeline")
    # The filename identifies the step; there is no separate step directory.
    assert path.name == "prepare_trade_files.ctx.json"


def test_multiple_steps_map_to_multiple_flat_files_in_one_run_dir() -> None:
    """Multiple steps produce multiple flat context files sharing one run directory."""
    state_dir = Path("/state")
    steps = ["redact_trade_inbox", "prepare_trade_files", "generate_trade_staging_views"]
    paths = [
        pipeline_run_ctx_path(state_dir, "run1", "trade_pipeline", step)
        for step in steps
    ]
    run_dir = pipeline_run_dir(state_dir, "run1", "trade_pipeline")
    assert {p.parent for p in paths} == {run_dir}
    assert [p.name for p in paths] == [f"{step}.ctx.json" for step in steps]
    assert len({str(p) for p in paths}) == len(steps)
