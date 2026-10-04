"""FileManifest.rollback: the scope's records, cut per manifest, reversed (backlog 612).

    selected mutation + scope -> the request returns every record of the scope
    -> per manifest, cut at the selected mutation's record type
    -> newest first, reverse the boundary and what follows: undo, delete its record
    -> a file with no mutation left: delete its manifest
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


def _scope(files: dict[str, Path]) -> list[dict[str, Any]]:
    """What the request returns for a run touching files 7, 8 and 9.

    File 7: inventory 1, classification 2, move 3, sanitize 4.
    File 8: classification 5, a later record 6.
    File 9: no classification, one record 10.
    """
    def record(mid: int, manifest: int, record_type: str, **more: Any) -> dict[str, Any]:
        return {"file_mutation_id": mid, "file_manifest_id": manifest,
                "record_type": record_type, "action": "record_only", "command": None,
                **more}
    return [
        record(10, 9, "source_file_profile"),
        record(6, 8, "source_file_profile"),
        record(5, 8, "source_file_classification"),
        {"file_mutation_id": 4, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "create", "path": str(files["sanitized"]),
         "restore_to_path": str(files["processing"]), "command": "rm"},
        {"file_mutation_id": 3, "file_manifest_id": 7, "record_type": "source_file_mutation",
         "action": "move", "path": str(files["processing"]),
         "restore_to_path": str(files["inbox"]), "command": "mv"},
        record(2, 7, "source_file_classification"),
        record(1, 7, "source_file_inventory"),
    ]


def test_the_mutation_and_scope_go_to_the_request_function(tmp_path: Path) -> None:
    control = _Control(_scope(_files(tmp_path)), {7: 4})

    FileManifest(control).rollback(scope="run", file_mutation_id=2)

    (sent,) = control.requests
    assert sent == {"scope": "run", "anchor_file_manifest_id": None,
                    "rollback_to_mutation_id": 2}


def test_each_manifest_is_cut_at_the_selected_record_type(tmp_path: Path) -> None:
    files = _files(tmp_path)
    control = _Control(_scope(files), {7: 4, 8: 2, 9: 1})

    result = FileManifest(control).rollback(scope="run", file_mutation_id=2)

    # File 7 from 2, file 8 from its classification 5; file 9 has none, so
    # nothing of it. The boundaries are reversed too.
    assert [row["file_mutation_id"] for row in result["mutations"]] == [6, 5, 4, 3, 2]
    assert result["boundary"] == {"file_mutation_id": 2,
                                  "record_type": "source_file_classification"}
    assert (control.deleted_mutations, control.deleted_manifests) == ([], [])
    assert files["sanitized"].exists() and files["processing"].exists()


def test_the_anchor_manifest_is_cut_at_the_selected_mutation(tmp_path: Path) -> None:
    control = _Control(_scope(_files(tmp_path)), {7: 4})

    result = FileManifest(control).rollback(scope="file", file_mutation_id=3)

    assert [row["file_mutation_id"] for row in result["mutations"]] == [4, 3]


def test_rollback_reverses_the_selected_records(tmp_path: Path) -> None:
    files = _files(tmp_path)
    control = _Control(_scope(files), {7: 4, 8: 2, 9: 1})

    result = FileManifest(control).rollback(scope="run", file_mutation_id=2, dry_run=False)

    assert result["reversed"] == [6, 5, 4, 3, 2]
    assert control.deleted_mutations == [6, 5, 4, 3, 2]
    assert not files["sanitized"].exists()
    assert files["inbox"].exists() and not files["processing"].exists()
    assert control.deleted_manifests == [8]  # 7 keeps its inventory; 8 has nothing left


def test_a_selected_file_is_reversed_whole(tmp_path: Path) -> None:
    files = _files(tmp_path)
    rows = [row for row in _scope(files) if row["file_manifest_id"] == 7]
    control = _Control(rows, {7: 4})

    result = FileManifest(control).rollback(file_manifest_id=7, dry_run=False)

    assert result["reversed"] == [4, 3, 2, 1]
    assert control.deleted_manifests == [7]
    assert result["manifests_deleted"] == [7]


def test_a_failed_reversal_stops_and_keeps_its_record(tmp_path: Path) -> None:
    files = _files(tmp_path)
    files["processing"].unlink()  # in neither place: nothing to move back
    control = _Control(_scope(files), {7: 4, 8: 2, 9: 1})

    result = FileManifest(control).rollback(scope="run", file_mutation_id=2, dry_run=False)

    assert result["reversed"] == [6, 5, 4]
    assert result["failed"]["file_mutation_id"] == 3
    assert control.deleted_mutations == [6, 5, 4]


def test_a_run_reads_its_mutations_and_reverses_them_all(tmp_path: Path) -> None:
    files = _files(tmp_path)
    rows = [row for row in _scope(files) if row["file_mutation_id"] in (3, 4)]
    control = _Control(rows, {7: 4})

    preview = FileManifest(control).rollback(run_id=330)

    (sent,) = control.requests
    assert sent == {"run_id": 330}
    assert preview["scope"] == "run" and preview["boundary"] is None
    assert [row["file_mutation_id"] for row in preview["mutations"]] == [4, 3]

    result = FileManifest(control).rollback(run_id=330, dry_run=False)

    assert result["reversed"] == [4, 3]
    assert files["inbox"].exists() and not files["sanitized"].exists()
    assert control.deleted_manifests == []  # inventory and classification remain


def test_a_run_is_rolled_back_with_scope_run_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scope run"):
        FileManifest(_Control([], {})).rollback(run_id=330, scope="file")


def test_a_mutation_outside_the_scope_records_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not among the scope's records"):
        FileManifest(_Control([], {})).rollback(file_mutation_id=2)


def test_exactly_one_of_file_or_mutation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        FileManifest(_Control([], {})).rollback(file_manifest_id=7, file_mutation_id=2)
    with pytest.raises(ValueError, match="exactly one"):
        FileManifest(_Control([], {})).rollback(file_mutation_id=2, run_id=330)
    with pytest.raises(ValueError, match="exactly one"):
        FileManifest(_Control([], {})).rollback()
