"""FileManifest.rollback(criteria): the request function's set, reversed (backlog 612).

    criteria -> the rollback request returns the mutations to reverse
    -> newest first, reverse each: undo its change, delete its record
    -> a file with no mutation left: delete its manifest

The request function decides the set; rollback reverses exactly what it returns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.files.manifest import FileManifest


class _Control:
    """The rollback request answers with ``answer``; deletes are recorded."""

    def __init__(self, answer: list[dict[str, Any]], remaining: dict[int, int]) -> None:
        self.answer = answer
        self.remaining = dict(remaining)  # file_manifest_id -> mutation count
        self.requests: list[dict[str, Any]] = []
        self.deleted_mutations: list[int] = []
        self.deleted_manifests: list[int] = []

    def request_file_rollback(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.requests.append(kwargs)
        return [dict(row) for row in self.answer]

    def delete_file_mutation(self, file_mutation_id: int) -> None:
        self.deleted_mutations.append(file_mutation_id)
        for row in self.answer:
            if row["file_mutation_id"] == file_mutation_id:
                self.remaining[row["file_manifest_id"]] -= 1

    def list_file_mutations(self, file_manifest_id: int | None = None,
                            file_mutation_id: int | None = None) -> list[dict[str, Any]]:
        if file_mutation_id is not None:
            return [{"file_mutation_id": file_mutation_id,
                     "record_type": "source_file_classification"}]
        return [{}] * self.remaining[file_manifest_id]

    def delete_file_manifest(self, file_manifest_id: int) -> None:
        self.deleted_manifests.append(file_manifest_id)


def _files(tmp_path: Path) -> dict[str, Path]:
    inbox = tmp_path / "source" / "inbox" / "a.csv"
    processing = tmp_path / "source" / "processing" / "a.csv"
    sanitized = tmp_path / "work" / "sanitized_csv" / "a.csv"
    processing.parent.mkdir(parents=True)
    processing.write_text("a\n1\n", encoding="utf-8")
    sanitized.parent.mkdir(parents=True)
    sanitized.write_text("a\n1\n", encoding="utf-8")
    return {"inbox": inbox, "processing": processing, "sanitized": sanitized}


def _after_classification(files: dict[str, Path]) -> list[dict[str, Any]]:
    """What the request returns for file 7 rolled back to its classification."""
    return [
        {"file_mutation_id": 4, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "create", "path": str(files["sanitized"]),
         "restore_to_path": str(files["processing"]), "command": "rm"},
        {"file_mutation_id": 3, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "move", "path": str(files["processing"]),
         "restore_to_path": str(files["inbox"]), "command": "mv"},
    ]


def test_the_criteria_go_to_the_request_function(tmp_path: Path) -> None:
    control = _Control([], {7: 2})

    FileManifest(control).rollback(scope="run", file_mutation_id=2,
                                   boundary_record_type="source_file_classification")

    (sent,) = control.requests
    assert sent == {"dry_run": True, "scope": "run", "anchor_file_manifest_id": None,
                    "rollback_to_mutation_id": 2,
                    "boundary_record_type": "source_file_classification"}


def test_a_dry_run_returns_the_requested_set_and_changes_nothing(tmp_path: Path) -> None:
    files = _files(tmp_path)
    control = _Control(_after_classification(files), {7: 4})

    result = FileManifest(control).rollback(file_mutation_id=2)

    assert [row["file_mutation_id"] for row in result["mutations"]] == [4, 3]
    assert result["boundary"] == {"file_mutation_id": 2,
                                  "record_type": "source_file_classification"}
    assert (control.deleted_mutations, control.deleted_manifests) == ([], [])
    assert files["sanitized"].exists() and files["processing"].exists()


def test_rollback_reverses_exactly_the_requested_set(tmp_path: Path) -> None:
    files = _files(tmp_path)
    control = _Control(_after_classification(files), {7: 4})

    result = FileManifest(control).rollback(file_mutation_id=2, dry_run=False)

    assert result["reversed"] == [4, 3]
    assert control.deleted_mutations == [4, 3]
    assert not files["sanitized"].exists()
    assert files["inbox"].exists() and not files["processing"].exists()
    assert control.deleted_manifests == []  # inventory and classification remain


def test_a_file_with_nothing_left_has_its_manifest_deleted(tmp_path: Path) -> None:
    files = _files(tmp_path)
    control = _Control(_after_classification(files), {7: 2})

    result = FileManifest(control).rollback(file_manifest_id=7, dry_run=False)

    assert control.deleted_manifests == [7]
    assert result["manifests_deleted"] == [7]


def test_a_failed_reversal_stops_and_keeps_its_record(tmp_path: Path) -> None:
    files = _files(tmp_path)
    files["processing"].unlink()  # in neither place: nothing to move back
    control = _Control(_after_classification(files), {7: 4})

    result = FileManifest(control).rollback(file_mutation_id=2, dry_run=False)

    assert result["reversed"] == [4]
    assert result["failed"]["file_mutation_id"] == 3
    assert control.deleted_mutations == [4]


def test_exactly_one_of_file_or_mutation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        FileManifest(_Control([], {})).rollback(file_manifest_id=7, file_mutation_id=2)
    with pytest.raises(ValueError, match="exactly one"):
        FileManifest(_Control([], {})).rollback()
