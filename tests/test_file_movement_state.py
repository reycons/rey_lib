"""Tests for movement state helpers in rey_lib.files.file_utils."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    config_root = tmp_path / "test" / "installations" / "ccc" / "configs" / "v01"
    config_root.mkdir(parents=True)
    return SimpleNamespace(
        config_root=config_root,
        environment_root=tmp_path / "test",
        installation="ccc",
        state=SimpleNamespace(
            file_operations_path="state/{config_root}/file_operations.jsonl"
        ),
    )


def test_configured_log_path_resolves_under_state_file_operations(tmp_path: Path) -> None:
    """File-operation state path must come from ctx.state.file_operations_path."""
    ctx = _ctx(tmp_path)
    assert file_operation_log_path(ctx) == (
        tmp_path
        / "test"
        / "installations"
        / "ccc"
        / "state"
        / "v01"
        / "file_operations.jsonl"
    )


def test_configured_log_path_is_relative_to_installation_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.state = SimpleNamespace(
        file_operations_path="state/app/{config_root}/operations.jsonl"
    )

    assert file_operation_log_path(ctx) == (
        tmp_path
        / "test"
        / "installations"
        / "ccc"
        / "state"
        / "app"
        / "v01"
        / "operations.jsonl"
    )


def test_legacy_movement_path_still_resolves(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.state = SimpleNamespace(
        file_movements_path="state/legacy/{config_root}/moves.jsonl"
    )

    assert file_movement_log_path(ctx) == (
        tmp_path
        / "test"
        / "installations"
        / "ccc"
        / "state"
        / "legacy"
        / "v01"
        / "moves.jsonl"
    )


def test_missing_state_path_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    delattr(ctx, "state")

    try:
        file_movement_log_path(ctx)
    except ValueError as exc:
        assert "state.file_operations_path" in str(exc)
    else:
        raise AssertionError("Expected missing state path to raise.")


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
