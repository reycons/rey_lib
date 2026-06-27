"""
Shared git repository mechanics — the single, centralized place git is executed.

No application should shell out to git directly; consumers call these helpers so
git behavior is consistent, testable, and engine-independent. Consumers own the
policy (when/what to commit); this module owns the mechanics.

Public API
----------
find_repo_root      Resolve the repository root containing a path.
get_repo_status     Working-tree status (clean + changed paths).
require_clean       Raise unless the working tree is clean.
stage_paths         Stage one or more paths.
commit              Commit staged changes; return the commit hash.
get_head_commit     Resolve the current HEAD commit hash.
run_git             Low-level centralized git invocation (advanced use).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Union

from rey_lib.git.errors import CommitResult, GitError, RepoStatus
from rey_lib.logs import get_logger

__all__ = [
    "find_repo_root",
    "get_repo_status",
    "require_clean",
    "stage_paths",
    "commit",
    "get_head_commit",
    "run_git",
]

_logger = get_logger(__name__)

_PathLike = Union[str, Path]


def run_git(
    repo_root: _PathLike,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command in ``repo_root`` and return the completed process.

    Centralized so every git call shares the same execution and error handling.

    Parameters
    ----------
    repo_root : str | Path
        Directory passed to ``git -C``.
    *args : str
        git arguments (e.g. ``"status", "--porcelain"``).
    check : bool
        When True (default) a non-zero exit raises :class:`GitError`.

    Returns
    -------
    subprocess.CompletedProcess
        The completed process (stdout/stderr captured as text).

    Raises
    ------
    GitError
        When git is missing or (with ``check``) the command fails.
    """
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH.") from exc
    if check and result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no output")
        raise GitError(f"git {' '.join(args)} failed in '{repo_root}': {detail}")
    return result


def find_repo_root(start: _PathLike) -> Path:
    """Return the repository root containing ``start``."""
    result = run_git(start, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip())


def get_repo_status(repo_root: _PathLike) -> RepoStatus:
    """Return the working-tree status of ``repo_root``."""
    result = run_git(repo_root, "status", "--porcelain")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    changed = [line[3:].strip() for line in lines]
    return RepoStatus(repo_root=Path(repo_root), clean=not lines, changed_paths=changed)


def require_clean(repo_root: _PathLike) -> None:
    """Raise :class:`GitError` unless ``repo_root`` has a clean working tree."""
    status = get_repo_status(repo_root)
    if not status.clean:
        raise GitError(
            f"working tree is not clean in '{repo_root}': {status.changed_paths}"
        )


def stage_paths(repo_root: _PathLike, paths: Iterable[_PathLike]) -> None:
    """Stage ``paths`` in ``repo_root``."""
    path_args = [str(p) for p in paths]
    if not path_args:
        raise GitError("stage_paths: no paths supplied.")
    run_git(repo_root, "add", "--", *path_args)


def commit(repo_root: _PathLike, message: str, *, allow_empty: bool = False) -> CommitResult:
    """Commit staged changes in ``repo_root`` and return the commit result.

    Parameters
    ----------
    repo_root : str | Path
        Repository root.
    message : str
        Commit message (may be multi-line).
    allow_empty : bool
        When True, permit a commit with no staged changes.

    Returns
    -------
    CommitResult
        The resolved HEAD hash and the message used.

    Raises
    ------
    GitError
        On empty message or a failed commit (e.g. nothing staged).
    """
    if not message.strip():
        raise GitError("commit: empty commit message.")
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    run_git(repo_root, *args)
    return CommitResult(hash=get_head_commit(repo_root), message=message)


def get_head_commit(repo_root: _PathLike) -> str:
    """Return the current HEAD commit hash of ``repo_root``."""
    return run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
