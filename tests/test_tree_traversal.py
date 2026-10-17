"""The tree's shared traversal mechanics (backlog 1054).

TreeTraversal routes a record to the source that owns its children, reads it
through the state the projection holds, applies the consumer's presentation
step, caps before admission, and admits through the projection. These pin that
behaviour -- moved unchanged from the Console's NodeDataObject -- plus the one
rule the move adds: a declared source this process cannot read is refused,
never read as persisted. Stub sources only; no database.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import (
    EXPANSION_LIMIT,
    INSPECTED_FIELDS,
    TreeProjection,
    TreeTraversal,
    inspected,
)
from rey_lib.tree.sources import DATABASE_SOURCE, FOLDER_SOURCE


class _Persisted:
    """The persisted source: children arrive with their own keys."""

    def __init__(self, count: int = 3) -> None:
        self.asked: list[str] = []
        self._count = count

    def children(self, ctx: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
        parent = str(record["node_key"])
        self.asked.append(parent)
        return [{"node_key": f"{parent}:{n}", "label": f"row {n}", "has_children": False}
                for n in range(self._count)]


class _Folder:
    """A declared source holding state per level, as the database source does."""

    def __init__(self, count: int = 3) -> None:
        self.reads = 0
        self._count = count

    def state_key(self, record: dict[str, Any]) -> str:
        return f"folder:{record['node_key']}"

    def state(self, ctx: Any, record: dict[str, Any]) -> list[str]:
        self.reads += 1
        return [f"entry {n}" for n in range(self._count)]

    def children(self, ctx: Any, record: dict[str, Any], state: Any) -> list[dict[str, Any]]:
        return [{"label": name, "tree_object_key": FOLDER_SOURCE, "secret": "x"}
                for name in state]


class TestRouting:
    def test_a_record_naming_nothing_is_persisted(self) -> None:
        persisted = _Persisted()
        traversal = TreeTraversal(persisted, {})

        children = traversal.children(object(), {"node_key": "1:1"})

        assert persisted.asked == ["1:1"]
        assert [c["node_key"] for c in children] == ["1:1:0", "1:1:1", "1:1:2"]

    @pytest.mark.parametrize("key", ["file_manifest", "run_history"])
    def test_an_undeclared_name_is_persisted_as_it_always_was(self, key: str) -> None:
        persisted = _Persisted()

        TreeTraversal(persisted, {}).children(object(), {"node_key": "9:9",
                                                        "tree_object_key": key})

        assert persisted.asked == ["9:9"]

    def test_a_declared_source_reads_through_its_implementation(self) -> None:
        folder = _Folder()
        projection = TreeProjection()

        children = TreeTraversal(_Persisted(), {FOLDER_SOURCE: folder}).children(
            object(), {"node_key": "2:2", "tree_object_key": FOLDER_SOURCE}, projection,
        )

        assert [c["label"] for c in children] == ["entry 0", "entry 1", "entry 2"]
        assert [c["node_key"] for c in children] == ["2:2:1", "2:2:2", "2:2:3"]
        assert all(c["parent_node_key"] == "2:2" for c in children)

    def test_a_declared_source_this_process_cannot_read_is_refused(self) -> None:
        persisted = _Persisted()

        with pytest.raises(ConfigError, match="'database' cannot be read here"):
            TreeTraversal(persisted, {}).children(
                object(), {"node_key": "3:3", "tree_object_key": DATABASE_SOURCE},
            )
        assert persisted.asked == []


class TestCapAndAdmission:
    def test_assemble_runs_before_the_cap_and_admission(self) -> None:
        seen: list[int] = []

        def assemble(ctx: Any, records: list[dict[str, Any]], projection: Any) -> list:
            seen.append(len(records))
            for record in records:
                record["buttons"] = ["open"]
            return records

        projection = TreeProjection()
        children, truncated = TreeTraversal(_Persisted(count=5), {}).bounded_children(
            object(), {"node_key": "1:1"}, projection, 2, assemble=assemble,
        )

        assert seen == [5]
        assert truncated is True
        assert [c["node_key"] for c in children] == ["1:1:0", "1:1:1"]
        assert projection.record("1:1:0")["buttons"] == ["open"]

    def test_only_what_was_returned_is_admitted(self) -> None:
        projection = TreeProjection()

        TreeTraversal(_Persisted(count=5), {}).bounded_children(
            object(), {"node_key": "1:1"}, projection, 2,
        )

        assert projection.record("1:1:1") is not None
        assert projection.record("1:1:2") is None

    def test_a_branch_within_the_limit_is_not_truncated(self) -> None:
        children, truncated = TreeTraversal(_Persisted(count=2), {}).bounded_children(
            object(), {"node_key": "1:1"}, TreeProjection(), EXPANSION_LIMIT,
        )

        assert (len(children), truncated) == (2, False)

    def test_without_a_projection_nothing_is_admitted_or_rekeyed(self) -> None:
        children = TreeTraversal(_Persisted(), {FOLDER_SOURCE: _Folder()}).children(
            object(), {"node_key": "2:2", "tree_object_key": FOLDER_SOURCE},
        )

        assert all("node_key" not in c for c in children)


class TestHeldState:
    def test_a_sources_read_is_held_and_reused(self) -> None:
        folder = _Folder()
        projection = TreeProjection()
        traversal = TreeTraversal(_Persisted(), {FOLDER_SOURCE: folder})
        record = {"node_key": "2:2", "tree_object_key": FOLDER_SOURCE}

        traversal.children(object(), record, projection)
        traversal.children(object(), record, projection)

        assert folder.reads == 1

    def test_fresh_reads_the_source_again(self) -> None:
        folder = _Folder()
        projection = TreeProjection()
        traversal = TreeTraversal(_Persisted(), {FOLDER_SOURCE: folder})
        record = {"node_key": "2:2", "tree_object_key": FOLDER_SOURCE}

        traversal.children(object(), record, projection)
        traversal.children(object(), record, projection, fresh=True)

        assert folder.reads == 2


class TestInspected:
    def test_exactly_the_declared_fields_and_nothing_else(self) -> None:
        record = {name: f"v-{name}" for name in INSPECTED_FIELDS}
        record.update({"secret": "x", "buttons": ["open"], "tree_object_key": "folder"})

        assert inspected(record) == {name: f"v-{name}" for name in INSPECTED_FIELDS}

    def test_the_ceiling_is_the_admitted_initial_one(self) -> None:
        assert EXPANSION_LIMIT == 50
