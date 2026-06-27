"""
Shared, engine-independent git mechanics for Rey Apps.

The single place git is executed: consumers (e.g. rey_db_admin DDL versioning)
own commit *policy*; this package owns *how* git runs. No app should shell out
to git directly.
"""

from rey_lib.git.diff import changed_paths, diff, verify_paths_changed
from rey_lib.git.errors import CommitResult, GitError, RepoStatus
from rey_lib.git.repo import (
    commit,
    find_repo_root,
    get_head_commit,
    get_repo_status,
    require_clean,
    run_git,
    stage_paths,
)

__all__ = [
    "GitError",
    "RepoStatus",
    "CommitResult",
    "find_repo_root",
    "get_repo_status",
    "require_clean",
    "stage_paths",
    "commit",
    "get_head_commit",
    "run_git",
    "diff",
    "changed_paths",
    "verify_paths_changed",
]
