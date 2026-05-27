"""Tests for movement state helpers in rey_lib.files.file_utils."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.config.config_utils import PathResolver
from rey_lib.files.file_utils import (
    file_sha256,
    file_operation_log_path,
    file_movement_log_path,
    find_original_relative_path,
    iter_file_operations,
    iter_file_movements,
    move_file,
)


def _ctx(tmp_path: Path) -> SimpleNamespace:
    root   = tmp_path / "test"
    state  = root / "state"
    paths  = PathResolver({
        "root":                 root.resolve(),
        "state":                state.resolve(),
        "file_operations_state": (state / "v01" / "file_operations.jsonl").resolve(),
    })
    return SimpleNamespace(paths=paths)


def test_configured_log_path_resolves_under_state_file_operations(tmp_path: Path) -> None:
    """file_operation_log_path must return the path resolver's file_operations_state."""
    ctx = _ctx(tmp_path)
    expected = (tmp_path / "test" / "state" / "v01" / "file_operations.jsonl").resolve()
    assert file_operation_log_path(ctx) == expected


def test_file_movement_log_path_is_alias(tmp_path: Path) -> None:
    """file_movement_log_path is a compatibility alias for file_operation_log_path."""
    ctx = _ctx(tmp_path)
    assert file_movement_log_path(ctx) == file_operation_log_path(ctx)


def test_missing_paths_raises(tmp_path: Path) -> None:
    ctx = SimpleNamespace()
    with pytest.raises(ValueError, match="ctx.paths is required"):
        file_operation_log_path(ctx)


def test_move_file_writes_jsonl_after_successful_move(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = tmp_path / "test" / "data" / "pipelines" / "daily" / "inbox" / "client_a" / "feed.csv"
    dest_dir = tmp_path / "test" / "data" / "pipelines" / "daily" / "processed" / "client_a"
    src.parent.mkdir(parents=True)
    src.write_text("a,b\n", encoding="utf-8")

    move_file(
        src,
        dest_dir,
        state_ctx=ctx,
        app="file_redactor",
        pipeline="daily",
        reason="processed",
    )

    record = list(iter_file_operations(ctx))[0]
    assert record["app"] == "file_redactor"
    assert record["operation_id"]
    assert record["operation"] == "move"
    assert record["action"] == "move"
    assert record["source"] == "data/pipelines/daily/inbox/client_a/feed.csv"
    assert record["destination"] == "data/pipelines/daily/processed/client_a/feed.csv"
    assert record["file_fingerprint"]["name"] == "feed.csv"
    assert record["file_fingerprint"]["exists"] is True
    assert record["file_fingerprint"]["size_bytes"] == 4
    assert record["file_fingerprint"]["sha256"] == file_sha256(dest_dir / "feed.csv")

    assert list(iter_file_movements(ctx)) == [record]


def test_find_original_relative_path_uses_latest_nested_inbox_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = tmp_path / "test" / "data" / "pipelines" / "daily" / "inbox" / "client_a" / "feed.csv"
    dest_dir = tmp_path / "test" / "data" / "pipelines" / "daily" / "processed"
    src.parent.mkdir(parents=True)
    src.write_text("a,b\n", encoding="utf-8")

    move_file(src, dest_dir, state_ctx=ctx, app="file_redactor", pipeline="daily")

    assert find_original_relative_path(ctx, pipeline="daily", file_name="feed.csv") == Path("client_a/feed.csv")
