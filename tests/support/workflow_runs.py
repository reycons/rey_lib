"""A Control double for the Runs a Workflow creates (backlog 950).

``Run.start`` records a run through ``Control.create_run_manifest`` and
``Run.finish`` closes it through ``Control.finish_run_manifest``. This double
answers both without a database: ids are minted in order, and every row and
terminal status is kept for a test to read.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.run import Run

__all__ = ["RecordingControl", "parent_run"]


class RecordingControl:
    """Mints run ids and keeps each recorded run's row and terminal status."""

    def __init__(self, first_id: int = 100) -> None:
        self._next = first_id
        self.rows: list[dict[str, Any]] = []
        self.finished: dict[int, str] = {}

    def create_run_manifest(self, **row: Any) -> int:
        """Record a starting run and return the id it was given."""
        run_id = self._next
        self._next += 1
        self.rows.append({"run_id": run_id, **row})
        return run_id

    def finish_run_manifest(self, run_id: int, status: str,
                            finished_at: Optional[str] = None,
                            required: bool = True) -> None:
        """Record a run's terminal status."""
        self.finished[int(run_id)] = status

    def children(self, parent: Run) -> list[dict[str, Any]]:
        """Every recorded run whose parent is ``parent``, in creation order,
        each with its terminal status (None while unfinished)."""
        return [
            {**row, "status": self.finished.get(row["run_id"])}
            for row in self.rows if row.get("parent_run_id") == parent.run_id
        ]


def parent_run(control: Any, run_id: int = 1, log: Any = None) -> Run:
    """The launch's parent Run, as bootstrap would hold it, with its run log."""
    run = Run(run_id=run_id, control=control, subject_type="workflow", subject_id="wf")
    run.log = log
    return run
