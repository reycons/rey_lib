"""Focused tests for the installation-scoped governed file manifest."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import make_run_log, start_test_run

from rey_lib.logs import (
    FileManifestError,
    log_file_manifest_record,
    log_run_record,
    resolve_file_manifest_path,
)


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def _ctx(manifest_path: Path) -> SimpleNamespace:
    """Return a context whose path resolver yields the given manifest path."""
    return SimpleNamespace(
        paths=SimpleNamespace(resolve=lambda name: manifest_path),
        app_name="rey_lib",
        log_depth=0,
    )


def _manifest(tmp_path: Path) -> Path:
    return tmp_path / "file_manifest.jsonl"


def _record(**overrides: object) -> dict:
    record = {
        "record_type": "source_file_inventory",
        "file": {"path": "/x", "size_bytes": 1},
    }
    record.update(overrides)
    return record


def _rows(manifest_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_manifest_path_comes_from_configuration(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert resolve_file_manifest_path(_ctx(manifest)) == manifest


def test_unconfigured_path_is_rejected() -> None:
    with pytest.raises(FileManifestError):
        resolve_file_manifest_path(SimpleNamespace())


# ---------------------------------------------------------------------------
# Creation and append
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rejected input
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# log_run_record durable identity
# ---------------------------------------------------------------------------


def _run_ctx(tmp_path: Path) -> SimpleNamespace:
    """Return a context with a durable run-log directory."""
    run_dir = tmp_path / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        run_log_dir=str(run_dir),
        app_name="rey_lib",
        name="rey_lib",
        log_depth=0,
    )
    start_test_run(ctx)
    return ctx


def test_log_run_record_returns_the_committed_record_id(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    first = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")
    second = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/b")

    assert (first, second) == (1, 2)


def test_returned_record_id_matches_the_run_log_row(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    record_id = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")

    rows = [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[record_id - 1]["record_id"] == record_id
    assert rows[record_id - 1]["path"] == "/a"


def test_log_run_record_returns_none_when_it_cannot_append(tmp_path: Path) -> None:
    """The never-raise contract holds: an unusable run log returns None, not an error."""
    from rey_lib.logs.run_log import RunLog

    unusable = RunLog(app="rey_lib", run_id="R1", run_timestamp="20260822_000000")
    assert log_run_record(unusable, "SOURCE_FILE_INVENTORY", path="/a") is None


def test_source_file_inventory_is_grouped_with_file_records(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")

    record = json.loads(
        Path(run_log.path()).read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["record_group"] == "files"
    assert record["record_subgroup"] == "input_files"


def test_normal_run_log_writing_is_unaffected(tmp_path: Path) -> None:
    """Existing record types keep their group and still append normally."""
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_run_record(run_log, "STEP_START", step_name="one")
    log_run_record(run_log, "STEP_END", step_name="one", status="ok")

    rows = [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["record_type"] for row in rows] == ["STEP_START", "STEP_END"]
    assert all(row["record_group"] == "execution" for row in rows)


def test_an_unknown_root_field_is_refused(tmp_path: Path) -> None:
    """An append-only store must never make an unnameable field permanent."""
    manifest = tmp_path / "file_manifest.jsonl"
    with pytest.raises(FileManifestError, match="unknown root field\\(s\\): mutation_kind"):
        log_file_manifest_record(
            _ctx(manifest),
            {"record_type": "source_file_mutation", "mutation_kind": "excel_to_csv"},
        )
    assert not manifest.exists() or _rows(manifest) == []

