"""
Git diff helpers for DDL verification.

Engine-independent: used to confirm a re-export matches a committed state and
that only expected paths changed. Mechanics only — callers decide what a diff
means.

Public API
----------
diff                 Return the diff text (optionally scoped to refs/paths).
changed_paths        Return the list of changed paths (name-only).
verify_paths_changed Check that changes stay within an expected path set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

from rey_lib.git.repo import run_git

__all__ = ["diff", "changed_paths", "verify_paths_changed"]

_PathLike = Union[str, Path]


def _diff_args(
    from_ref: Optional[str],
    to_ref: Optional[str],
    paths: Optional[Iterable[_PathLike]],
    extra: Optional[list[str]] = None,
) -> list[str]:
    """Assemble ``git diff`` arguments from optional refs and paths."""
    args = ["diff", *(extra or [])]
    if from_ref:
        args.append(from_ref)
    if to_ref:
        args.append(to_ref)
    if paths:
        args.append("--")
        args.extend(str(p) for p in paths)
    return args


def diff(
    repo_root: _PathLike,
    from_ref: Optional[str] = None,
    to_ref: Optional[str] = None,
    paths: Optional[Iterable[_PathLike]] = None,
) -> str:
    """Return the diff text.

    With no refs this is the working-tree diff. ``from_ref`` alone (e.g.
    ``"HEAD"``) diffs the working tree against that ref. ``paths`` scopes the
    diff. Used to report executable-SQL differences during verification.
    """
    return run_git(repo_root, *_diff_args(from_ref, to_ref, paths)).stdout


def changed_paths(
    repo_root: _PathLike,
    from_ref: Optional[str] = None,
    to_ref: Optional[str] = None,
    paths: Optional[Iterable[_PathLike]] = None,
) -> list[str]:
    """Return the changed paths for the given diff scope (name-only)."""
    out = run_git(
        repo_root, *_diff_args(from_ref, to_ref, paths, extra=["--name-only"])
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def verify_paths_changed(
    repo_root: _PathLike,
    expected_paths: Iterable[_PathLike],
    from_ref: Optional[str] = None,
    to_ref: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """Return ``(ok, unexpected_paths)`` for a diff scope.

    ``ok`` is True when every changed path lies within ``expected_paths`` (a
    file or a directory prefix). Used to fail closed when unexpected objects
    changed.
    """
    expected = [str(p).rstrip("/") for p in expected_paths]
    changes = changed_paths(repo_root, from_ref, to_ref)
    unexpected = [
        c for c in changes
        if not any(c == e or c.startswith(e + "/") for e in expected)
    ]
    return (not unexpected, unexpected)
