"""
Tests for file_utils.file_reference_metadata (SGC_Rey_Run_Backend_Helper_API).

Cover safe, content-free metadata for a run-referenced file: approved-root
enforcement, present/absent files, and the absence of any content read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.file_utils import file_reference_metadata


def test_metadata_for_existing_file_under_approved_root(tmp_path: Path) -> None:
    """An approved, existing file yields size/modified metadata and file actions."""
    target = tmp_path / "report.json"
    target.write_text('{"ok": true}\n', encoding="utf-8")

    meta = file_reference_metadata(target, approved_roots=[tmp_path])

    assert meta["exists"] is True
    assert meta["name"] == "report.json"
    assert meta["size_bytes"] == len('{"ok": true}\n')
    assert meta["modified_at"]
    assert "view" in meta["actions"]
    # Metadata never carries file content.
    assert "content" not in meta


def test_metadata_for_missing_file_reports_absent(tmp_path: Path) -> None:
    """A missing (but approved-root) path reports exists=False without raising."""
    meta = file_reference_metadata(tmp_path / "gone.json", approved_roots=[tmp_path])

    assert meta["exists"] is False
    assert meta["size_bytes"] == 0
    assert meta["actions"] == ["copy_path"]


def test_metadata_rejects_path_outside_approved_roots(tmp_path: Path) -> None:
    """A path outside the approved roots is rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    approved = tmp_path / "approved"
    approved.mkdir()

    with pytest.raises(ValueError):
        file_reference_metadata(outside / "secret.txt", approved_roots=[approved])
