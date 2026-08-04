"""Focused tests for the shared Phase 1 File Manifest hierarchy."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import rey_lib.logs.file_hierarchy as hierarchy_module
from rey_lib.files.jsonl import write_jsonl_file
from rey_lib.logs.file_hierarchy import (
    FileHierarchyError,
    build_file_hierarchy,
    build_file_hierarchy_feed,
    build_file_hierarchy_feeds,
    build_file_hierarchy_stages,
)


def _ctx(path):
    return SimpleNamespace(paths=SimpleNamespace(resolve=lambda name: path))


def _inventory(record_id: int, file_id: str, feed: str, name: str, path: str) -> dict:
    return {
        "record_id": record_id,
        "record_type": "source_file_inventory",
        "file_id": file_id,
        "source_name": feed,
        "file": {"file_name": name, "path": path},
        "producer": {"application": "file_operator"},
    }


def _classification(
    record_id: int,
    file_id: str,
    feed: str,
    source_record_id: int | None = None,
) -> dict:
    """One governed classification record declaring the file's feed."""
    record = {
        "record_id": record_id,
        "record_type": "source_file_classification",
        "file_id": file_id,
        "status": "classified",
        "file": {"file_name": f"{file_id}.csv", "path": f"/in/{file_id}.csv"},
        "classification": {
            "type": "file_name_regex",
            "source_field": "file.file_name",
            "values": {"feed": feed},
        },
    }
    if source_record_id is not None:
        record["lineage"] = {"source_record_id": source_record_id}
    return record


def _mutation(
    record_id: int,
    file_id: str,
    action: str,
    *,
    path: str,
    original_path: str | None = None,
) -> dict:
    file_data = {"file_name": path.rsplit("/", 1)[-1], "path": path}
    if original_path is not None:
        file_data["original_path"] = original_path
    return {
        "record_id": record_id,
        "record_type": "source_file_mutation",
        "file_id": file_id,
        "action": action,
        "status": "success",
        "file": file_data,
        "evidence": {"run_log_record_id": record_id},
    }


def _write(tmp_path, records: list[dict]):
    path = tmp_path / "file_manifest.jsonl"
    write_jsonl_file(path, records)
    return path


def _files(page):
    return [file for feed in page.feeds for file in feed.files]


def test_library_has_only_canonical_shared_data_dependencies() -> None:
    source = Path(hierarchy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "rey_lib.files.jsonl" in imported
    assert "rey_lib.logs.file_manifest" in imported
    assert not imported.intersection(
        {
            "json",
            "os",
            "pathlib",
            "rey_lib.logs.log_utils",
            "rey_lib.logs.evidence_projection",
            "rey_lib.files.file_utils",
        }
    )


def test_reads_only_through_shared_jsonl_boundary(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path, [
        _inventory(1, "file-a", "feed-a", "a.csv", "/in/a.csv"),
        _classification(2, "file-a", "feed-a"),
    ])
    import rey_lib.files.jsonl as jsonl_boundary

    calls = []
    canonical = jsonl_boundary.read_jsonl_file

    def tracked(selected_path, **kwargs):
        calls.append((selected_path, kwargs))
        return canonical(selected_path, **kwargs)

    monkeypatch.setattr(jsonl_boundary, "read_jsonl_file", tracked)
    page = build_file_hierarchy(_ctx(path))

    assert len(_files(page)) == 1
    assert calls == [(path, {})]


def test_only_inventory_records_create_files_and_group_by_classified_feed(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"record_id": 7, "record_type": "source_file_classification", "file_id": "classified"},
            _inventory(4, "file-b", "feed_inbox", "b.csv", "/in/b.csv"),
            _inventory(2, "file-a", "feed_inbox", "a.csv", "/in/a.csv"),
            _mutation(8, "orphan", "move", path="/processing/a.csv", original_path="/in/a.csv"),
            _classification(10, "file-b", "Zulu"),
            _classification(11, "file-a", "alpha"),
        ],
    )

    page = build_file_hierarchy(_ctx(path))

    assert [feed.feed_identity for feed in page.feeds] == ["alpha", "Zulu"]
    assert [[file.file_id for file in feed.files] for feed in page.feeds] == [["file-a"], ["file-b"]]


def test_mutations_join_only_by_exact_file_id_not_name_or_path(tmp_path) -> None:
    inventory = _inventory(1, "governed-a", "feed", "same.csv", "/in/same.csv")
    exact = _mutation(5, "governed-a", "move", path="/processing/same.csv", original_path="/in/same.csv")
    lookalike = _mutation(6, "governed-b", "create", path="/processing/same.csv")
    page = build_file_hierarchy(_ctx(_write(
        tmp_path, [inventory, lookalike, exact, _classification(7, "governed-a", "feed")]
    )))

    file = _files(page)[0]
    assert [mutation.record_id for mutation in file.mutations] == [5]
    assert file.mutations[0].metadata["file_id"] == "governed-a"


def test_committed_record_id_controls_file_and_mutation_order(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            _mutation(12, "file-a", "create", path="/out/a.csv"),
            _inventory(9, "file-b", "feed", "b.csv", "/in/b.csv"),
            _mutation(10, "file-a", "move", path="/work/a.csv", original_path="/in/a.csv"),
            _inventory(3, "file-a", "feed", "a.csv", "/in/a.csv"),
            _classification(20, "file-a", "feed"),
            _classification(21, "file-b", "feed"),
        ],
    )

    page = build_file_hierarchy(_ctx(path))

    assert [file.inventory_record_id for file in _files(page)] == [3, 9]
    assert [mutation.record_id for mutation in _files(page)[0].mutations] == [10, 12]


def test_page_is_bounded_and_model_is_immutable(tmp_path) -> None:
    records = [
        _inventory(index, f"file-{index}", "feed", f"{index}.csv", f"/in/{index}.csv")
        for index in range(1, 302)
    ] + [
        _classification(1000 + index, f"file-{index}", "feed")
        for index in range(1, 302)
    ]
    ctx = _ctx(_write(tmp_path, records))

    first = build_file_hierarchy(ctx, limit=250)
    second = build_file_hierarchy(ctx, offset=250, limit=250)

    assert len(_files(first)) == 250
    assert first.next_offset == 250
    assert len(_files(second)) == 51
    assert second.next_offset is None
    assert first.total_files == 301
    with pytest.raises(FileHierarchyError, match="must not exceed 250"):
        build_file_hierarchy(ctx, limit=251)
    with pytest.raises(TypeError):
        _files(first)[0].metadata["changed"] = True  # type: ignore[index]


def test_payload_preserves_supplied_governed_values(tmp_path) -> None:
    inventory = _inventory(1, "file-a", "Feed A", "a.csv", "/in/a.csv")
    mutation = _mutation(2, "file-a", "create", path="/out/a.csv")
    payload = build_file_hierarchy(_ctx(_write(
        tmp_path, [inventory, mutation, _classification(3, "file-a", "Feed A")]
    ))).to_payload()

    assert payload["feeds"][0]["display_label"] == "Feed A"
    assert payload["feeds"][0]["files"][0]["metadata"] == inventory
    assert payload["feeds"][0]["files"][0]["mutations"][0]["metadata"] == mutation


def test_phase_two_queries_lazy_load_feed_files_and_exact_file_stages(tmp_path) -> None:
    inventory = _inventory(1, "file-a", "Feed A", "a.csv", "/in/a.csv")
    move = _mutation(2, "file-a", "move", path="/processing/a.csv", original_path="/in/a.csv")
    ctx = _ctx(_write(tmp_path, [inventory, move, _classification(3, "file-a", "Feed A")]))

    feeds = build_file_hierarchy_feeds(ctx).to_payload()
    assert feeds["feeds"] == [{
        "feed_identity": "Feed A", "display_label": "Feed A", "total_files": 1,
        "files": [], "files_loaded": False,
    }]
    files = build_file_hierarchy_feed(ctx, "Feed A").to_payload()
    assert [item["file_id"] for item in files["feeds"][0]["files"]] == ["file-a"]
    assert files["feeds"][0]["files"][0]["mutations"] == []
    stages = build_file_hierarchy_stages(ctx, 1).to_payload()
    assert stages["current_path"] == "/processing/a.csv"
    assert [stage["stage_type"] for stage in stages["stages"]] == ["inventory", "mutation"]


def test_create_never_replaces_primary_current_path_and_rollback_is_action_aware(tmp_path) -> None:
    inventory = _inventory(1, "file-a", "feed", "a.xlsx", "/in/a.xlsx")
    move = _mutation(2, "file-a", "move", path="/processing/a.xlsx", original_path="/in/a.xlsx")
    create = _mutation(3, "file-a", "create", path="/converted/a.csv")
    rollback = {
        "record_id": 4, "record_type": "source_file_rollback", "status": "success",
        "rollback": {"phase": "final", "original_record_id": 2, "attempt_record_id": 3},
        "evidence": {"run_log_record_id": 9},
    }
    page = build_file_hierarchy_stages(_ctx(_write(tmp_path, [inventory, move, create, rollback])), 1)

    assert page.current_path == "/in/a.xlsx"
    assert page.lifecycle_status == "active"
    assert [stage.stage_type for stage in page.stages] == ["inventory", "mutation", "mutation", "rollback"]


def test_moved_primary_does_not_mark_historical_inventory_stage_current(tmp_path) -> None:
    inventory = _inventory(1, "file-a", "feed", "a.csv", "/in/a.csv")
    move = _mutation(2, "file-a", "move", path="/processing/a.csv", original_path="/in/a.csv")

    page = build_file_hierarchy_stages(_ctx(_write(tmp_path, [inventory, move])), 1)

    assert page.current_path == "/processing/a.csv"
    assert all(stage.is_current_primary is False for stage in page.stages)


def test_rollback_is_reduced_in_committed_order_before_later_mutations(tmp_path) -> None:
    inventory = _inventory(1, "file-a", "feed", "a.csv", "/in/a.csv")
    first_move = _mutation(2, "file-a", "move", path="/work/a.csv", original_path="/in/a.csv")
    rollback = {
        "record_id": 3, "record_type": "source_file_rollback", "status": "success",
        "rollback": {"phase": "final", "original_record_id": 2, "attempt_record_id": 2},
        "evidence": {"run_log_record_id": 3},
    }
    second_move = _mutation(4, "file-a", "move", path="/processing/a.csv", original_path="/in/a.csv")

    page = build_file_hierarchy_stages(
        _ctx(_write(tmp_path, [inventory, first_move, rollback, second_move])), 1
    )
    assert page.current_path == "/processing/a.csv"
    assert page.lifecycle_status == "active"


def test_profiles_join_only_by_exact_governed_file_id(tmp_path) -> None:
    ctx = _ctx(_write(tmp_path, [_inventory(1, "file-a", "feed", "a.csv", "/in/a.csv")]))
    profile = {
        "record_id": 8, "record_type": "ARTIFACT_REFERENCE", "artifact_group": "profiles",
        "artifact_type": "row_shape_analysis", "source_file_id": "file-a",
        "path": "/profiles/a.profile.json", "__run_log_file": "/logs/run.jsonl",
    }
    page = build_file_hierarchy_stages(ctx, 1, profile_artifacts=(profile,))
    assert page.stages[-1].stage_type == "profile"
    assert page.stages[-1].path == "/profiles/a.profile.json"
    with pytest.raises(FileHierarchyError, match="does not match"):
        build_file_hierarchy_stages(ctx, 1, profile_artifacts=({**profile, "source_file_id": "other"},))


def test_configured_inventory_source_name_is_never_a_feed(tmp_path) -> None:
    """`feed_inbox` is discovery configuration, not governed feed identity."""
    ctx = _ctx(_write(tmp_path, [
        _inventory(1, "file-a", "feed_inbox", "CWATranMay26.xls", "/in/CWATranMay26.xls"),
        _classification(2, "file-a", "bny"),
    ]))

    identities = [feed.feed_identity for feed in build_file_hierarchy(ctx).feeds]
    summaries = [feed.feed_identity for feed in build_file_hierarchy_feeds(ctx).feeds]

    assert identities == ["bny"]
    assert summaries == ["bny"]
    assert "feed_inbox" not in identities
    assert "feed_inbox" not in summaries


def test_records_from_different_feeds_group_under_separate_feed_nodes(tmp_path) -> None:
    """One shared inventory source still yields one node per classified feed."""
    ctx = _ctx(_write(tmp_path, [
        _inventory(1, "file-a", "feed_inbox", "CWATranMay26.xls", "/in/a.xls"),
        _inventory(2, "file-b", "feed_inbox", "BmoHoldMay26.csv", "/in/b.csv"),
        _inventory(3, "file-c", "feed_inbox", "AmalTranMay26.csv", "/in/c.csv"),
        _classification(4, "file-a", "bny"),
        _classification(5, "file-b", "bmo"),
        _classification(6, "file-c", "amalgamated"),
    ]))

    page = build_file_hierarchy(ctx)

    assert [feed.feed_identity for feed in page.feeds] == ["amalgamated", "bmo", "bny"]
    assert [[file.file_id for file in feed.files] for feed in page.feeds] == [
        ["file-c"], ["file-b"], ["file-a"],
    ]
    assert [item["file_id"] for item in
            build_file_hierarchy_feed(ctx, "bny").to_payload()["feeds"][0]["files"]] == ["file-a"]


def test_file_lifecycle_children_are_unchanged_by_feed_grouping(tmp_path) -> None:
    """Grouping changed; the file's lifecycle stages are rendered as before."""
    ctx = _ctx(_write(tmp_path, [
        _inventory(1, "file-a", "feed_inbox", "CWATranMay26.xls", "/in/CWATranMay26.xls"),
        _mutation(2, "file-a", "move", path="/processing/CWATranMay26.xls",
                  original_path="/in/CWATranMay26.xls"),
        _classification(3, "file-a", "bny", source_record_id=1),
        _mutation(4, "file-a", "create", path="/converted/CWATranMay26.csv"),
    ]))

    stages = build_file_hierarchy_stages(ctx, 1).to_payload()

    assert [stage["stage_type"] for stage in stages["stages"]] == [
        "inventory", "mutation", "classification", "mutation",
    ]
    assert stages["current_path"] == "/processing/CWATranMay26.xls"


def test_a_file_without_a_classified_feed_is_not_grouped_under_a_feed(tmp_path) -> None:
    """No feed is invented for unclassified evidence; its lifecycle still reads."""
    ctx = _ctx(_write(tmp_path, [
        _inventory(1, "file-a", "feed_inbox", "a.csv", "/in/a.csv"),
        _mutation(2, "file-a", "move", path="/processing/a.csv", original_path="/in/a.csv"),
    ]))

    assert build_file_hierarchy(ctx).feeds == ()
    assert build_file_hierarchy_feeds(ctx).feeds == ()
    assert build_file_hierarchy_stages(ctx, 1).to_payload()["current_path"] == "/processing/a.csv"
