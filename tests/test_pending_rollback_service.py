"""The rollback service: reverse a pending mutation, then mark it reversed.

The pending rows are the queue. Each is reversed on its own and completed the
moment its inverse succeeds, so a run that dies partway leaves exactly the
unfinished rows pending and the next run continues them.

These exercise the service against fakes rather than a live database: what is
under test is the loop's contract -- one row at a time, complete only on
success, never touch the filesystem to decide a target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.files.log_run_rollback import (
    LogRunRollbackError,
    _pending_as_compensation_record,
    run_pending_file_rollbacks,
)


class _Manifest:
    """A FileManifest that serves one fixed queue and records completions."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.completed: list[int] = []

    def pending_rollbacks(self) -> list[dict[str, Any]]:
        return [row for row in self._rows
                if row["file_mutation_id"] not in self.completed]

    def complete_rollback(self, file_mutation_id: int) -> None:
        self.completed.append(file_mutation_id)


@pytest.fixture()
def ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A context exposing a shared Control, with FileManifest stubbed."""
    holder: dict[str, Any] = {}

    class _Ctx:
        shared_control = object()

    def _factory(_control: Any) -> Any:
        return holder["manifest"]

    monkeypatch.setattr("rey_lib.files.manifest.FileManifest", _factory)
    context = _Ctx()
    context.install = holder            # type: ignore[attr-defined]
    return context


def _mutation(mutation_id: int, action: str, path: str,
              restore_to: str | None) -> dict[str, Any]:
    return {
        "file_mutation_id": mutation_id,
        "record_type": "source_file_mutation",
        "action": action,
        "path": path,
        "restore_to_path": restore_to,
        "status": "succeeded",
        "rollback": None,
    }


class TestTheInverseIsPerformed:
    """Each action's own inverse, on the target the history resolved."""

    def test_move_restores_the_original_location(self, tmp_path: Path,
                                                 ctx: Any) -> None:
        source, destination = tmp_path / "A.csv", tmp_path / "B.csv"
        destination.write_text("data")
        ctx.install["manifest"] = _Manifest(
            [_mutation(1, "move", str(destination), str(source))])

        result = run_pending_file_rollbacks(ctx)

        assert source.is_file() and not destination.exists()
        assert result["reversed"] == 1 and result["failed"] == 0
        assert ctx.install["manifest"].completed == [1]

    def test_create_removes_the_created_file(self, tmp_path: Path,
                                             ctx: Any) -> None:
        created = tmp_path / "made.csv"
        created.write_text("data")
        ctx.install["manifest"] = _Manifest(
            [_mutation(2, "create", str(created), None)])

        result = run_pending_file_rollbacks(ctx)

        assert not created.exists()
        assert result["reversed"] == 1
        assert ctx.install["manifest"].completed == [2]


class TestRetrySafety:
    """A failure leaves work for the next run, and never half-marks a row."""

    def test_a_failed_inverse_leaves_the_row_pending(self, tmp_path: Path,
                                                     ctx: Any) -> None:
        """The file is in neither place: nothing can be restored."""
        missing, gone = tmp_path / "here.csv", tmp_path / "there.csv"
        ctx.install["manifest"] = _Manifest(
            [_mutation(3, "move", str(gone), str(missing))])

        result = run_pending_file_rollbacks(ctx)

        assert result["reversed"] == 0 and result["failed"] == 1
        assert ctx.install["manifest"].completed == []
        assert ctx.install["manifest"].pending_rollbacks(), "row must remain queued"

    def test_the_next_run_retries_what_stayed_pending(self, tmp_path: Path,
                                                      ctx: Any) -> None:
        source, destination = tmp_path / "A.csv", tmp_path / "B.csv"
        ctx.install["manifest"] = _Manifest(
            [_mutation(4, "move", str(destination), str(source))])

        first = run_pending_file_rollbacks(ctx)
        assert first["failed"] == 1

        destination.write_text("data")          # the obstruction clears
        second = run_pending_file_rollbacks(ctx)

        assert second["reversed"] == 1
        assert source.is_file()
        assert ctx.install["manifest"].completed == [4]

    def test_an_already_satisfied_inverse_completes(self, tmp_path: Path,
                                                    ctx: Any) -> None:
        """The move-back landed before the process died.

        Reversal is judged by the end state. Refusing here would leave the row
        pending forever, describing a state that no longer exists.
        """
        source, destination = tmp_path / "A.csv", tmp_path / "B.csv"
        source.write_text("data")               # already back
        ctx.install["manifest"] = _Manifest(
            [_mutation(5, "move", str(destination), str(source))])

        result = run_pending_file_rollbacks(ctx)

        assert result["reversed"] == 1
        assert ctx.install["manifest"].completed == [5]

    def test_one_failure_does_not_abandon_the_rest(self, tmp_path: Path,
                                                   ctx: Any) -> None:
        doomed = tmp_path / "missing.csv"
        source, destination = tmp_path / "A.csv", tmp_path / "B.csv"
        destination.write_text("data")
        ctx.install["manifest"] = _Manifest([
            _mutation(6, "move", str(doomed), str(tmp_path / "nowhere.csv")),
            _mutation(7, "move", str(destination), str(source)),
        ])

        result = run_pending_file_rollbacks(ctx)

        assert result["reversed"] == 1 and result["failed"] == 1
        assert ctx.install["manifest"].completed == [7]


class TestTargetsComeFromTheHistory:
    """The request froze the set; the filesystem is never consulted for a target."""

    def test_the_restore_target_is_the_resolved_path(self) -> None:
        record = _pending_as_compensation_record(
            _mutation(8, "move", "/B/a.csv", "/A/a.csv"))

        assert record["file"]["path"] == "/B/a.csv"
        assert record["file"]["original_path"] == "/A/a.csv"

    def test_no_surviving_predecessor_leaves_no_target(self) -> None:
        record = _pending_as_compensation_record(
            _mutation(9, "create", "/A/a.csv", None))

        assert record["file"]["original_path"] is None


class TestControlIsRequired:
    """Rollback is recorded in the control database or not at all."""

    def test_a_context_without_control_is_refused(self) -> None:
        class _Bare:
            pass

        with pytest.raises(LogRunRollbackError, match="shared Control"):
            run_pending_file_rollbacks(_Bare())
