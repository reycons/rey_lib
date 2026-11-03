"""AppExecute owns the active run's stdout and projects the live run (#1311).

User rulings, 2026-11-03: AppExecute is the traffic cop for an active run and owns
that run's stdout. Each run owns its own stdout, a child run's included, and a
child's output is never copied into its parent's. Its projection is the durable
facts of ``state()``, its detail, error and artifacts, and its stdout after a
cursor -- an offset into that stdout -- with the cursor that follows it. It does
not interpret log records or reconstruct historical runs: a run answered by
``open`` carries its manifest facts and no stdout.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.run import AppExecute

from tests.support.workflow_runs import RecordingControl


def _started(control: Any = None, parent: AppExecute | None = None) -> AppExecute:
    return AppExecute.start(control or RecordingControl(), subject_type="app",
                            subject_id="workflow_app", parent=parent)


def test_the_projection_is_the_state_with_the_runs_outcome_and_its_stdout() -> None:
    run = _started()
    run.detail = "two files"
    run.artifacts = ["a.csv"]

    projection = run.projection()

    assert {key: projection[key] for key in run.state()} == run.state()
    assert projection["detail"] == "two files"
    assert projection["error"] is None
    assert projection["artifacts"] == ["a.csv"]
    assert projection["stdout"] == ""
    assert projection["stdout_cursor"] == 0


def test_written_output_is_projected_in_order_and_the_cursor_is_its_end() -> None:
    run = _started()
    run.write("entering state RUNNING\n")
    run.write("step one completed\n")

    projection = run.projection()

    assert projection["stdout"] == "entering state RUNNING\nstep one completed\n"
    assert projection["stdout_cursor"] == len(projection["stdout"])


def test_a_cursor_returns_only_the_output_written_after_it() -> None:
    run = _started()
    run.write("first\n")
    first = run.projection()
    run.write("second\n")

    after = run.projection(first["stdout_cursor"])

    assert after["stdout"] == "second\n"
    assert after["stdout_cursor"] == len("first\nsecond\n")
    # Nothing new: the delta is empty and the cursor stays where it was.
    assert run.projection(after["stdout_cursor"])["stdout"] == ""
    assert run.projection(after["stdout_cursor"])["stdout_cursor"] == after["stdout_cursor"]


def test_empty_writes_change_nothing() -> None:
    run = _started()
    run.write("")

    assert run.projection()["stdout_cursor"] == 0


@pytest.mark.parametrize("cursor", [-1, 1])
def test_a_cursor_the_run_never_handed_out_is_refused(cursor: int) -> None:
    run = _started()

    with pytest.raises(AppError):
        run.projection(cursor)


def test_each_run_owns_its_stdout_and_a_childs_is_not_its_parents() -> None:
    control = RecordingControl()
    workflow = _started(control)
    step = _started(control, parent=workflow)
    workflow.write("workflow narration\n")
    step.write("step output\n")

    assert workflow.projection()["stdout"] == "workflow narration\n"
    assert step.projection()["stdout"] == "step output\n"
    assert step.projection()["parent_run_id"] == workflow.run_id


def test_a_finished_run_still_held_keeps_its_final_stdout() -> None:
    run = _started()
    run.write("done\n")
    run.finish("SUCCEEDED")

    projection = run.projection()

    assert projection["status"] == "SUCCEEDED"
    assert projection["stdout"] == "done\n"


def test_a_run_answered_by_open_has_its_manifest_facts_and_no_stdout() -> None:
    class _Manifest:
        def get_run_manifest(self, run_id: int) -> dict[str, Any]:
            return {"run_manifest_id": run_id, "subject_type": "app",
                    "subject_id": "workflow_app", "status": "SUCCEEDED"}

    opened = AppExecute.open(_Manifest(), 7)
    assert opened is not None

    projection = opened.projection()

    assert projection["run_id"] == 7
    assert projection["status"] == "SUCCEEDED"
    assert projection["stdout"] == ""
    assert projection["stdout_cursor"] == 0


def test_concurrent_writers_lose_no_output() -> None:
    run = _started()

    def write_many(tag: str) -> None:
        for _ in range(200):
            run.write(tag)

    writers = [threading.Thread(target=write_many, args=(tag,)) for tag in "abcd"]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    stdout = run.projection()["stdout"]
    assert len(stdout) == 800
    assert all(stdout.count(tag) == 200 for tag in "abcd")
