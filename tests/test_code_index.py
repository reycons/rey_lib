"""Turning one snapshot into the rows a destination receives.

The grouping is the whole of it: a destination must never re-derive which file
declares a symbol, because two destinations that derive it separately can
disagree. These assert what the shape says, not how any provider stores it.
"""

from __future__ import annotations

import pytest

from rey_lib.repository_map.code_index import CodeIndexWriter, IndexedRepository, index
from rey_lib.repository_map.records import dotted_identity
from rey_lib.repository_map.snapshot import CodeIndexSnapshot
from rey_lib.repository_map.writer import RepositoryMap


class _Recorder:
    """A destination that remembers instead of storing."""

    def __init__(self) -> None:
        self.calls: list[list[IndexedRepository]] = []

    def replace_index(self, indexed: list[IndexedRepository]) -> None:
        self.calls.append(indexed)

    @property
    def replaced(self) -> list[IndexedRepository]:
        """The one replacement's repositories."""
        assert len(self.calls) == 1, "the index is replaced in exactly one call"
        return self.calls[0]


def _header(repository: str) -> dict[str, str]:
    return {
        "repository": repository,
        "head_commit": "abc123",
        "branch": "main",
        "working_tree_status": "clean",
        "content_hash": "c" * 64,
        "generator_version": "1.0.0",
        "rules_hash": "r" * 64,
    }


def _file(path: str, language: str = "Python") -> dict[str, object]:
    return {
        "record_type": "file",
        "path": path,
        "language": language,
        "content_hash": "f" * 64,
        "classification": {"generated": False, "vendor": False, "test": False},
    }


def _symbol(path: str, name: str, owner: str = "") -> dict[str, object]:
    qualified = f"{owner}.{name}" if owner else name
    return {
        "record_type": "symbol",
        "source_path": path,
        "source_line": 10,
        "source_column": 0,
        "name": name,
        "symbol_kind": "function",
        "exported": True,
        "owner": owner,
        "qualified_name": qualified,
        "end_line": 20,
        "end_column": 0,
        "returns_annotation": None,
        "is_generator": False,
        "dotted_identity": dotted_identity(path, qualified),
    }


def _edge(path: str, target: str, kind: str = "call") -> dict[str, object]:
    return {
        "record_type": "dependency_edge",
        "source_path": path,
        "source_line": 12,
        "source_column": 4,
        "from": f"file:{path}",
        "to": target,
        "edge_kind": kind,
        "evidence": "ast.Call",
    }


def _snapshot(records: list[dict[str, object]]) -> CodeIndexSnapshot:
    return CodeIndexSnapshot(
        maps={"rey_loader": RepositoryMap(header=_header("rey_loader"), records=records)},
        index=None,
        projection=None,
    )


class TestTheRowsADestinationReceives:
    """What indexing hands over, independently of where it goes."""

    def test_a_symbol_arrives_under_the_file_that_declares_it(self) -> None:
        """Grouping happens once, here, so no destination repeats it."""
        recorder = _Recorder()
        index(
            _snapshot(
                [_file("app.py"), _file("db.py"), _symbol("db.py", "connect")]
            ),
            recorder,
        )

        files = {entry["relative_path"]: entry for entry in recorder.replaced[0].files}
        assert [s["name"] for s in files["db.py"]["symbols"]] == ["connect"]
        assert files["app.py"]["symbols"] == []

    def test_the_header_becomes_the_observed_state(self) -> None:
        """Revision, branch and status travel together or not at all."""
        recorder = _Recorder()
        index(_snapshot([_file("app.py")]), recorder)

        assert recorder.replaced[0].header == {
            "repository_key": "rey_loader",
            "revision": "abc123",
            "branch": "main",
            "working_tree_status": "clean",
            "content_hash": "c" * 64,
            "generator_version": "1.0.0",
            "rules_hash": "r" * 64,
        }

    def test_a_symbol_naming_an_uninventoried_file_is_dropped(self) -> None:
        """A generator defect is reported, never written under an invented file."""
        recorder = _Recorder()
        index(_snapshot([_file("app.py"), _symbol("ghost.py", "vanished")]), recorder)

        paths = [entry["relative_path"] for entry in recorder.replaced[0].files]
        assert paths == ["app.py"]
        assert recorder.replaced[0].files[0]["symbols"] == []

    def test_a_recorder_satisfies_the_writer_contract(self) -> None:
        """The protocol is one method, so a destination is easy to substitute."""
        assert isinstance(_Recorder(), CodeIndexWriter)

    def test_the_whole_index_is_handed_over_in_one_call(self) -> None:
        """Not per repository.

        A destination that received repositories one at a time could be
        interrupted between two of them, and the index would then hold half of
        one scan and half of the last. What is absent from this call is absent
        from the index.
        """
        recorder = _Recorder()
        index(_snapshot([_file("app.py")]), recorder)

        assert len(recorder.calls) == 1
        assert [entry.repository for entry in recorder.calls[0]] == ["rey_loader"]

    def test_position_is_carried_but_is_not_identity(self) -> None:
        """Line and column describe a symbol; nothing addresses one by them."""
        recorder = _Recorder()
        index(_snapshot([_file("db.py"), _symbol("db.py", "connect")]), recorder)

        symbol = recorder.replaced[0].files[0]["symbols"][0]
        assert symbol["start_line"] == 10
        assert symbol["start_column"] == 0
        assert "record_id" not in symbol
