"""
Tests for reading several runs out of one run log.

A run log is one top-level execution artifact, not one run: a pipeline and every
step it spawns share a log, each minting its own ``run_id`` and naming its
``parent_run_id``.

Two readers answer two different questions, and neither replaces the other.
``run_summary`` answers "what is this artifact" -- one row, the top-level run,
counts aggregated across the whole tree. ``runs_in_run_log`` answers "what ran in
here" -- one row per run, with lineage and that run's own counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rey_lib.logs import run_summary, runs_in_run_log
from rey_lib.logs.evidence_projection import RunLogIdentityError


def _log(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    """Write records as a run log and return its path."""
    path = tmp_path / "amalgamate.20260821_101500.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def _tree(tmp_path: Path) -> Path:
    """A pipeline, a workflow beneath it, an app beneath that, and a direct app."""
    return _log(tmp_path, [
        {"record_type": "RUN_START", "run_id": "R100", "subject_type": "pipeline",
         "subject_id": "amalgamate", "pipeline_name": "amalgamate", "timestamp": "t1"},
        {"record_type": "RUN_START", "run_id": "R101", "parent_run_id": "R100",
         "subject_type": "workflow", "subject_id": "load_only", "timestamp": "t2"},
        {"record_type": "RUN_START", "run_id": "R102", "parent_run_id": "R101",
         "subject_type": "app", "subject_id": "rey_loader", "timestamp": "t3"},
        {"record_type": "ERROR", "run_id": "R102", "timestamp": "t4"},
        {"record_type": "RUN_COMPLETE", "run_id": "R102", "status": "failed",
         "timestamp": "t5"},
        {"record_type": "RUN_COMPLETE", "run_id": "R101", "status": "failed",
         "timestamp": "t6"},
        {"record_type": "RUN_START", "run_id": "R104", "parent_run_id": "R100",
         "subject_type": "app", "subject_id": "ftp_sync", "timestamp": "t7"},
        {"record_type": "WARNING", "run_id": "R104", "timestamp": "t8"},
        {"record_type": "RUN_COMPLETE", "run_id": "R104", "status": "completed",
         "timestamp": "t9"},
        {"record_type": "RUN_COMPLETE", "run_id": "R100", "status": "failed",
         "timestamp": "t10"},
    ])


class TestTheExecutionTreeIsReadable:
    """Every run in the log, linked by parent, rebuilt from records alone."""

    def test_each_run_appears_once_with_its_lineage(self, tmp_path: Path) -> None:
        runs = {run["run_id"]: run for run in runs_in_run_log(_tree(tmp_path))}

        assert set(runs) == {"R100", "R101", "R102", "R104"}
        assert runs["R100"]["parent_run_id"] == ""
        assert runs["R101"]["parent_run_id"] == "R100"
        assert runs["R102"]["parent_run_id"] == "R101"
        assert runs["R104"]["parent_run_id"] == "R100"

    def test_the_tree_rebuilds_by_walking_parents(self, tmp_path: Path) -> None:
        runs = {run["run_id"]: run for run in runs_in_run_log(_tree(tmp_path))}

        def root_of(run_id: str) -> str:
            while runs[run_id]["parent_run_id"]:
                run_id = runs[run_id]["parent_run_id"]
            return run_id

        assert root_of("R102") == "R100"
        assert root_of("R104") == "R100"
        assert root_of("R100") == "R100"

    def test_a_run_carries_its_own_subject(self, tmp_path: Path) -> None:
        runs = {run["run_id"]: run for run in runs_in_run_log(_tree(tmp_path))}

        assert runs["R100"]["subject_type"] == "pipeline"
        assert runs["R101"]["subject_type"] == "workflow"
        assert runs["R102"]["subject_id"] == "rey_loader"
        assert runs["R104"]["subject_id"] == "ftp_sync"

    def test_counts_are_each_run_s_own(self, tmp_path: Path) -> None:
        runs = {run["run_id"]: run for run in runs_in_run_log(_tree(tmp_path))}

        assert runs["R102"]["error_count"] == 1
        assert runs["R104"]["warning_count"] == 1
        # The parent's own counts, not the tree's.
        assert runs["R100"]["error_count"] == 0
        assert runs["R100"]["warning_count"] == 0

    def test_each_run_reports_its_own_completion(self, tmp_path: Path) -> None:
        runs = {run["run_id"]: run for run in runs_in_run_log(_tree(tmp_path))}

        assert runs["R102"]["status"] == "failed"
        assert runs["R104"]["status"] == "completed"
        assert runs["R100"]["completed_at"] == "t10"

    def test_a_record_with_no_identity_is_reported_not_skipped(
        self, tmp_path: Path,
    ) -> None:
        # Passing over it would hide a malformed writer behind a tree that merely
        # looks smaller than it is.
        path = _log(tmp_path, [
            {"record_type": "RUN_START", "run_id": "R100", "timestamp": "t1"},
            {"record_type": "STEP_START", "timestamp": "t2"},
        ])

        with pytest.raises(RunLogIdentityError, match="carries no run_id"):
            runs_in_run_log(path)


class TestTheArtifactSummaryStaysOnePerLog:
    """run_summary answers about the artifact, and is not overloaded per run."""

    def test_the_summary_names_the_top_level_run(self, tmp_path: Path) -> None:
        summary = run_summary(_tree(tmp_path))

        assert summary["run_id"] == "R100"

    def test_summary_counts_are_tree_wide(self, tmp_path: Path) -> None:
        summary = run_summary(_tree(tmp_path))

        # One error from R102 and one warning from R104: the artifact's totals,
        # which is what a reader of a run artifact is asking for.
        assert summary["error_count"] == 1
        assert summary["warning_count"] == 1

    def test_status_comes_from_the_top_level_run_not_the_last_written(
        self, tmp_path: Path,
    ) -> None:
        # The parent completes, then a late child finishes after it. Selecting
        # the last RUN_COMPLETE would report the child's status as the run's.
        path = _log(tmp_path, [
            {"record_type": "RUN_START", "run_id": "R100", "timestamp": "t1"},
            {"record_type": "RUN_COMPLETE", "run_id": "R100", "status": "completed",
             "timestamp": "t2"},
            {"record_type": "RUN_START", "run_id": "R101", "parent_run_id": "R100",
             "timestamp": "t3"},
            {"record_type": "RUN_COMPLETE", "run_id": "R101", "status": "failed",
             "timestamp": "t4"},
        ])

        summary = run_summary(path)

        assert summary["run_id"] == "R100"
        assert summary["status"] == "completed"
        assert summary["completed_at"] == "t2"

    def test_a_log_whose_first_record_has_no_identity_is_refused(
        self, tmp_path: Path,
    ) -> None:
        # There is no legacy history to tolerate, so an unidentified record is
        # malformed. Reading it as legacy would let a broken writer pass silently
        # for as long as nobody noticed runs missing from the tree.
        path = _log(tmp_path, [
            {"record_type": "RUN_START", "timestamp": "t1"},
            {"record_type": "RUN_COMPLETE", "status": "completed", "timestamp": "t2"},
        ])

        with pytest.raises(RunLogIdentityError, match="first record carries no run_id"):
            run_summary(path)

    def test_an_empty_log_is_refused(self, tmp_path: Path) -> None:
        path = _log(tmp_path, [])

        with pytest.raises(RunLogIdentityError, match="holds no records"):
            run_summary(path)
