"""Tests for the shared, engine-independent git utility (rey_lib.git)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rey_lib.git import (
    GitError,
    changed_paths,
    commit,
    find_repo_root,
    get_head_commit,
    get_repo_status,
    require_clean,
    stage_paths,
    verify_paths_changed,
)


def _init_repo(tmp_path: Path) -> Path:
    """Initialise a throwaway git repo with identity configured."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


def test_status_require_clean_stage_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    obj_dir = repo / "database_ddl" / "postgres" / "control"
    obj_dir.mkdir(parents=True)
    (obj_dir / "v.sql").write_text("CREATE OR REPLACE VIEW v AS SELECT 1;\n")

    assert not get_repo_status(repo).clean
    with pytest.raises(GitError):
        require_clean(repo)

    stage_paths(repo, ["database_ddl/postgres/control"])
    result = commit(repo, "BEFORE\n\nContracts:\n- SGC_X")
    assert result.hash == get_head_commit(repo)
    require_clean(repo)  # clean after commit


def test_find_repo_root(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == repo.resolve()


def test_verify_paths_changed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    obj_dir = repo / "ddl"
    obj_dir.mkdir()
    (obj_dir / "v.sql").write_text("SELECT 1;\n")
    stage_paths(repo, ["ddl"])
    commit(repo, "init")

    (obj_dir / "v.sql").write_text("SELECT 1, 2;\n")
    ok, unexpected = verify_paths_changed(repo, ["ddl"], "HEAD")
    assert ok and unexpected == []
    assert changed_paths(repo, "HEAD") == ["ddl/v.sql"]

    (repo / "stray.sql").write_text("x\n")
    stage_paths(repo, ["stray.sql"])
    ok2, unexpected2 = verify_paths_changed(repo, ["ddl"], "HEAD")
    assert not ok2 and unexpected2 == ["stray.sql"]


def test_commit_empty_message_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(GitError):
        commit(repo, "   ")
