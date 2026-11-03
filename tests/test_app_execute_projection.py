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


def test_a_parent_projects_the_child_runs_it_owns_each_with_its_own_output() -> None:
    control = RecordingControl()
    workflow = _started(control)
    first = _started(control, parent=workflow)
    second = _started(control, parent=workflow)
    first.write("one\n")
    second.write("two\n")

    children = workflow.projection()["children"]

    assert [child["run_id"] for child in children] == [first.run_id, second.run_id]
    assert [child["stdout"] for child in children] == ["one\n", "two\n"]
    assert workflow.projection()["stdout"] == ""


def test_observers_are_told_of_writes_to_the_run_and_to_the_runs_it_owns() -> None:
    control = RecordingControl()
    workflow = _started(control)
    step = _started(control, parent=workflow)
    told: list[int] = []
    workflow.observe(lambda written: told.append(written.run_id))

    workflow.write("narration\n")
    step.write("step output\n")

    assert told == [workflow.run_id, step.run_id]


def test_an_observer_that_raises_is_detached_and_writing_goes_on() -> None:
    run = _started()
    told: list[str] = []

    def broken(_written: AppExecute) -> None:
        raise RuntimeError("observer broke")

    run.observe(broken)
    run.observe(lambda _written: told.append("told"))
    run.write("first\n")
    run.write("second\n")

    assert run.projection()["stdout"] == "first\nsecond\n"
    assert told == ["told", "told"]


def test_the_process_stdout_and_stderr_are_the_owning_runs_and_still_reach_their_streams(
    capfd: pytest.CaptureFixture[str],
) -> None:
    import os

    run = _started()

    with run.capture_process_output():
        os.write(1, b"Scanning 14 repositories\n")
        os.write(2, "WARNING été\n".encode("utf-8"))

    captured = capfd.readouterr()
    assert captured.out == "Scanning 14 repositories\n"
    assert captured.err == "WARNING été\n"
    stdout = run.projection()["stdout"]
    assert "Scanning 14 repositories\n" in stdout
    assert "WARNING été\n" in stdout
    # Restored: written after the block, it is no longer the run's.
    os.write(1, b"after\n")
    assert "after" not in run.projection()["stdout"]


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
