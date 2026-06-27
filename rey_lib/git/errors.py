"""
Errors and result objects for the shared git utility.

These are framework-level, engine-independent git types. ``rey_db_admin`` (and
any other consumer) owns *when* to commit and *what* a commit means; this
package owns *how* git is executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rey_lib.errors.error_utils import AppError

__all__ = ["GitError", "RepoStatus", "CommitResult"]


class GitError(AppError):
    """A git operation failed (non-zero exit, missing repo, or bad input)."""


@dataclass
class RepoStatus:
    """Working-tree status for a repository.

    Attributes
    ----------
    repo_root : Path
        Repository root.
    clean : bool
        True when the working tree has no staged or unstaged changes.
    changed_paths : list[str]
        Paths reported by ``git status --porcelain`` (empty when clean).
    """

    repo_root:     Path
    clean:         bool
    changed_paths: list[str] = field(default_factory=list)


@dataclass
class CommitResult:
    """Result of a commit: the resolved HEAD hash and the message used."""

    hash:    str
    message: str
