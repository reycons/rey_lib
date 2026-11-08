"""The installation's read-only source estate, hosted by rey_repository_map.

Discovery and scan policy are repository_map's existing policy. File owns safe
text; git owns revision/status/diff mechanics. Callers name repositories and
relative source paths, never commands, Git arguments or host paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import AppError
from rey_lib.files.file import File
from rey_lib.git.repo import get_head_commit, read_file_changes, read_working_tree
from rey_lib.repository_map.inventory import load_scan_rules, inventory_files
from rey_lib.repository_map.records import ScanRules, matches_any_glob
from rey_lib.repository_map.snapshot import estate_repositories

__all__ = ["RepositoryEstate"]


class RepositoryEstate:
    """Authorized checkouts beneath one explicitly resolved installation apps root.

    No mutation or generic command interface exists. Each read states its own
    observation, not a promise of an atomic multi-repository snapshot.
    """

    def __init__(self, apps_root: Path, *, max_source_bytes: int = 1_048_576) -> None:
        self.root = Path(apps_root).resolve()
        if not self.root.is_dir():
            raise AppError("A RepositoryEstate needs a configured apps directory.")
        self._repositories = tuple(key for key in estate_repositories(self.root)
                                   if not (self.root / key).is_symlink())
        self.max_source_bytes = self._integer(max_source_bytes, 1, 16_777_216)

    @staticmethod
    def _integer(value: Any, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise AppError(f"Expected an integer between {minimum} and {maximum}.")
        return value

    def _keys(self) -> list[str]:
        return [key for key in self._repositories if (self.root / key).is_dir()
                and not (self.root / key).is_symlink()]

    def _repository(self, key: str) -> Path:
        if not isinstance(key, str) or not key or key not in self._keys():
            raise AppError("Repository is not a checkout in this authorized estate.")
        return self.root / key

    @staticmethod
    def _rules(root: Path) -> ScanRules:
        path = root / 'repository_map.rules.yaml'
        if path.is_symlink():
            raise AppError('Repository scan policy must not be a symlink.')
        return load_scan_rules(path) if path.is_file() else ScanRules.unconfigured()

    def _path(self, key: str, relative_path: str) -> tuple[Path, Path]:
        root = self._repository(key)
        if not isinstance(relative_path, str) or not relative_path or '\\' in relative_path or '\0' in relative_path:
            raise AppError("Name a repository-relative source path.")
        parts = PurePosixPath(relative_path).parts
        if PurePosixPath(relative_path).is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in parts):
            raise AppError("Hidden, absolute and traversing paths are not source paths.")
        rules = self._rules(root)
        if any(p in rules.ignored_directory_names for p in parts[:-1]) or matches_any_glob(relative_path, rules.ignored_path_globs):
            raise AppError("This path is excluded by repository source policy.")
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise AppError("Source reads do not follow symlinks.")
        if not current.resolve().is_relative_to(root.resolve()):
            raise AppError("Source path escapes its repository.")
        refused = File.for_path(current).display_refusal()
        if refused:
            raise AppError(refused)
        return root, current

    def _text(self, path: Path) -> str:
        if not path.is_file():
            raise AppError("Source file does not exist.")
        try:
            content = File.for_path(path).content(self.max_source_bytes)
        except (OSError, UnicodeError) as error:
            raise AppError('Source could not be read as safe text.') from error
        if content.refusal:
            raise AppError(content.refusal)
        if content.truncated:
            raise AppError("Source exceeds the estate's bounded source-read size; no complete source was read.")
        return content.text

    @staticmethod
    def _evidence(key: str, root: Path) -> dict[str, Any]:
        return {"repository_key": key, "revision": get_head_commit(root),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "working_tree", "atomic_snapshot": False}

    def inspect(self) -> dict[str, Any]:
        repositories = []
        for key in self._keys():
            root = self._repository(key)
            state = read_working_tree(root)
            visible = []
            excluded = 0
            for change in state:
                try:
                    self._path(key, change['relative_path'])
                except AppError:
                    excluded += 1
                    continue
                visible.append(change)
            repositories.append({**self._evidence(key, root),
                                 'working_tree_clean': not state,
                                 'changes': visible[:200], 'changes_truncated': len(visible) > 200,
                                 'excluded_changes': excluded})
        return {"kind": "repository_estate", "access": "read", "proposal_only": True,
                "repositories": repositories}

    def search(self, text: str, *, repository_key: str = "", path_glob: str = "*",
               offset: int = 0, limit: int = 30) -> dict[str, Any]:
        """Literal text search across eligible checkouts, with resumable result bounds."""
        if not isinstance(text, str) or not text or len(text) > 1000:
            raise AppError("Search needs nonempty literal text of at most 1000 characters.")
        offset = self._integer(offset, 0, 1_000_000)
        limit = self._integer(limit, 1, 200)
        if not isinstance(path_glob, str) or len(path_glob) > 500:
            raise AppError("A source path glob must be text of at most 500 characters.")
        keys = [repository_key] if repository_key else self._keys()
        matches: list[dict[str, Any]] = []
        seen, skipped = 0, 0
        observations = []
        for key in keys:
            root = self._repository(key)
            observed = self._evidence(key, root)
            observations.append(observed)
            for entry in inventory_files(root, self._rules(root)):
                if not fnmatch(entry.path, path_glob):
                    continue
                try:
                    _, path = self._path(key, entry.path)
                    source = self._text(path)
                except (AppError, OSError, UnicodeError):
                    skipped += 1
                    continue
                for line, value in enumerate(source.splitlines(), 1):
                    if text not in value:
                        continue
                    seen += 1
                    if seen <= offset:
                        continue
                    if len(matches) == limit:
                        return {"matches": matches, "observations": observations, "has_more": True,
                                "next_offset": offset + len(matches), "skipped_files": skipped,
                                "coverage_complete": False}
                    at = value.index(text)
                    excerpt = value[max(0, at - 120):at + len(text) + 120]
                    matches.append({**observed, "relative_path": entry.path, "line": line,
                                    "text": excerpt, "line_truncated": excerpt != value,
                                    "content_hash": sha256_text(source),
                                    "content_hash_kind": "sha256_decoded_utf8_source"})
        return {"matches": matches, "observations": observations, "has_more": False,
                "next_offset": None, "skipped_files": skipped, "coverage_complete": skipped == 0}

    def read_source(self, repository_key: str, relative_path: str, *, start_line: int = 1,
                    line_count: int = 100) -> dict[str, Any]:
        """Read any bounded span, including later spans in another repository."""
        start_line = self._integer(start_line, 1, 10_000_000)
        line_count = self._integer(line_count, 1, 400)
        root, path = self._path(repository_key, relative_path)
        evidence = self._evidence(repository_key, root)
        source = self._text(path)
        lines = source.splitlines(keepends=True)
        selected = lines[start_line - 1:start_line - 1 + line_count]
        # A huge single source line must not create an unbounded tool response.
        text = ''.join(selected)
        if len(text.encode('utf-8')) > 65_536:
            raise AppError("Source span exceeds 65536 bytes; request fewer lines.")
        return {**evidence, "relative_path": relative_path, "start_line": start_line,
                "end_line": start_line + len(selected) - 1, "total_lines": len(lines),
                "text": text,
                "numbered_text": ''.join(f"{start_line + offset}: {line}" + ('' if line.endswith('\n') else '\n')
                                         for offset, line in enumerate(selected)),
                "content_hash": sha256_text(source),
                "content_hash_kind": "sha256_decoded_utf8_source",
                "revision_after_read": get_head_commit(root),
                "has_more": start_line - 1 + len(selected) < len(lines)}

    def diff(self, repository_key: str, relative_path: str, *, max_chars: int = 40000) -> dict[str, Any]:
        """Status and staged/unstaged diffs of one eligible path, never apply them."""
        max_chars = self._integer(max_chars, 1, 100000)
        root, path = self._path(repository_key, relative_path)
        source = self._text(path) if path.is_file() else None
        result = read_file_changes(root, relative_path)
        for name in ('staged', 'unstaged'):
            refusal = File.for_path(path).display_refusal(result[name].encode('utf-8'))
            if refusal:
                raise AppError(refusal)
        return {**self._evidence(repository_key, root), "relative_path": relative_path,
                "status": result['status'], "staged": result['staged'][:max_chars],
                "unstaged": result['unstaged'][:max_chars],
                "staged_truncated": len(result['staged']) > max_chars,
                "unstaged_truncated": len(result['unstaged']) > max_chars,
                "content_hash": sha256_text(source) if source is not None else None,
                "untracked": result['status'].startswith('??'),
                "untracked_note": "Untracked content has no Git diff; use read_source.",
                "changes_applied": False}
