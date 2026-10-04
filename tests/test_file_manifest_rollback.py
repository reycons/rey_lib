"""FileManifest.rollback(criteria): governed history, reversed newest first (backlog 612).

    read the mutations after the boundary
    -> newest first, reverse each: undo its change, delete its record
    -> nothing left: delete the manifest
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.files.manifest import FileManifest


class _Control:
    """The three control calls rollback makes, over an in-memory history."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.deleted_mutations: list[int] = []
        self.deleted_manifests: list[int] = []
        self.requests: list[dict[str, Any]] = []

    def request_file_rollback(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.requests.append(kwargs)
        assert kwargs["dry_run"] is True  # rollback only ever reads through it
        return [dict(row) for row in self.rows
                if row["file_manifest_id"] == kwargs["file_manifest_id"]]

    def delete_file_mutation(self, file_mutation_id: int) -> None:
        self.deleted_mutations.append(file_mutation_id)

    def delete_file_manifest(self, file_manifest_id: int) -> None:
        self.deleted_manifests.append(file_manifest_id)


def _history(tmp_path: Path) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """inventory -> classify -> move to processing -> create a sanitized copy."""
    inbox = tmp_path / "source" / "inbox" / "a.csv"
    processing = tmp_path / "source" / "processing" / "a.csv"
    sanitized = tmp_path / "work" / "sanitized_csv" / "a.csv"
    processing.parent.mkdir(parents=True)
    processing.write_text("a\n1\n", encoding="utf-8")
    sanitized.parent.mkdir(parents=True)
    sanitized.write_text("a\n1\n", encoding="utf-8")
    rows = [
        {"file_mutation_id": 1, "file_manifest_id": 7, "record_type": "source_file_inventory",
         "action": "record_only", "status": "success", "path": str(inbox),
         "restore_to_path": None, "command": None, "rollback_action": "delete_record"},
        {"file_mutation_id": 2, "file_manifest_id": 7, "record_type": "source_file_classification",
         "action": "record_only", "status": "success", "path": str(inbox),
         "restore_to_path": str(inbox), "command": None, "rollback_action": "delete_record"},
        {"file_mutation_id": 3, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "move", "status": "success", "path": str(processing),
         "restore_to_path": str(inbox), "command": "mv", "rollback_action": "move_back"},
        {"file_mutation_id": 4, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "create", "status": "success", "path": str(sanitized),
         "restore_to_path": str(processing), "command": "rm", "rollback_action": "delete_file"},
        {"file_mutation_id": 9, "file_manifest_id": 8, "record_type": "source_file_inventory",
         "action": "record_only", "status": "success", "path": "/other",
         "restore_to_path": None, "command": None, "rollback_action": "delete_record"},
    ]
    return rows, {"inbox": inbox, "processing": processing, "sanitized": sanitized}


def test_a_dry_run_returns_the_mutations_and_changes_nothing(tmp_path: Path) -> None:
    rows, files = _history(tmp_path)
    control = _Control(rows)

    result = FileManifest(control).rollback(7, to_file_mutation_id=2, dry_run=True)

    assert [row["file_mutation_id"] for row in result["mutations"]] == [4, 3]
    assert (control.deleted_mutations, control.deleted_manifests) == ([], [])
    assert files["sanitized"].exists() and files["processing"].exists()


def test_rollback_to_a_mutation_keeps_it_and_reverses_what_came_after(tmp_path: Path) -> None:
    rows, files = _history(tmp_path)
    control = _Control(rows)

    result = FileManifest(control).rollback(7, to_file_mutation_id=2, dry_run=False)

    # Newest first: the sanitized copy is deleted, then the file moves back.
    assert result["reversed"] == [4, 3]
    assert control.deleted_mutations == [4, 3]
    assert not files["sanitized"].exists()
    assert files["inbox"].exists() and not files["processing"].exists()
    # The boundary and everything before it stay; so does the manifest.
    assert control.deleted_manifests == []
    assert result["manifest_deleted"] is False


def test_rolling_back_everything_deletes_the_manifest(tmp_path: Path) -> None:
    rows, files = _history(tmp_path)
    control = _Control(rows)

    result = FileManifest(control).rollback(7, dry_run=False)

    assert control.deleted_mutations == [4, 3, 2, 1]
    assert control.deleted_manifests == [7]
    assert result["manifest_deleted"] is True
    assert files["inbox"].exists()


def test_a_failed_reversal_stops_and_keeps_its_record(tmp_path: Path) -> None:
    rows, files = _history(tmp_path)
    files["processing"].unlink()  # the file is in neither place: nothing to move back
    control = _Control(rows)

    result = FileManifest(control).rollback(7, dry_run=False)

    assert result["reversed"] == [4]
    assert result["failed"]["file_mutation_id"] == 3
    assert control.deleted_mutations == [4]
    assert control.deleted_manifests == []


def test_another_files_mutation_is_not_a_boundary(tmp_path: Path) -> None:
    rows, _ = _history(tmp_path)

    with pytest.raises(ValueError, match="not one of file 7"):
        FileManifest(_Control(rows)).rollback(7, to_file_mutation_id=9, dry_run=True)


def test_other_files_are_never_touched(tmp_path: Path) -> None:
    rows, _ = _history(tmp_path)
    control = _Control(rows)

    FileManifest(control).rollback(7, dry_run=False)

    assert 9 not in control.deleted_mutations
    assert control.requests[0]["file_manifest_id"] == 7
