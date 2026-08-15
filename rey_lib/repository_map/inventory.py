"""Deterministic source-file inventory for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-001, REQ-010 to REQ-012).

The inventory reads no file contents and mutates no source. It records only
filesystem facts, and orders its output so two runs from the same commit
produce identical records.
"""

from __future__ import annotations

import os
from pathlib import Path

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import is_hidden_path, read_text_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.rule_families import RULE_FAMILIES
from rey_lib.repository_map.records import (
    ENTRY_POINT_LOAD_UNKNOWN,
    LANGUAGE_UNKNOWN,
    FileRecord,
    ScanRules,
    matches_any_glob,
)

__all__ = ["inventory_files", "load_scan_rules"]

logger = get_logger(__name__)


def load_scan_rules(rules_path: Path) -> ScanRules:
    """Load a repository's scan rules from its rules file.

    Args:
        rules_path: Path to the repository's repository_map.rules.yaml.

    Returns:
        The typed scan rules.

    Raises:
        FileNotFoundError: If the rules file does not exist. The path is
            configuration and is never guessed or substituted.
        ValueError: If the rules file is not valid rules content.
    """
    if not rules_path.is_file():
        raise FileNotFoundError(f"Scan rules file not found: {rules_path}")

    text = read_text_file(rules_path)
    try:
        parsed = parse_yaml(text)
    except Exception as exc:  # Surface the offending file, not a bare parse error.
        raise ValueError(f"Scan rules file is not valid YAML: {rules_path}") from exc

    try:
        return ScanRules.from_mapping(parsed, RULE_FAMILIES)
    except ValueError as exc:
        raise ValueError(f"Invalid scan rules in {rules_path}: {exc}") from exc


def inventory_files(repo_root: Path, rules: ScanRules) -> list[FileRecord]:
    """Inventory every scannable source file under a repository root.

    Hidden entries are excluded before stat and are never opened, listed or
    descended into. That is repository policy, not a configurable ignore rule:
    a path segment beginning with '.' is out of scope for repository-map
    discovery and must not be made expressible in ScanRules. Symlinks are
    skipped so a scan cannot escape the repository or vary with link targets.

    Args:
        repo_root: Repository root to scan.
        rules: The scanned repository's own scan rules.

    Returns:
        File records sorted by repository-relative path.

    Raises:
        NotADirectoryError: If repo_root is not an existing directory.
    """
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository root is not a directory: {repo_root}")

    records: list[FileRecord] = []
    # os.walk is used over Path.rglob so ignored trees are pruned before they
    # are descended into; Path.walk is unavailable on the supported 3.11 floor.
    for dir_path, dir_names, file_names in os.walk(repo_root, followlinks=False):
        current_dir = Path(dir_path)
        # Prune in place. Assigning to the slice is what stops the descent.
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not is_hidden_path(current_dir / name, repo_root)
            and name not in rules.ignored_directory_names
        )
        for file_name in sorted(file_names):
            if is_hidden_path(current_dir / file_name, repo_root):
                continue
            record = _build_file_record(current_dir / file_name, repo_root, rules)
            if record is not None:
                records.append(record)

    records.sort(key=lambda record: record.path)
    logger.debug("Inventoried %d files under %s", len(records), repo_root)
    return records


def _build_file_record(
    file_path: Path,
    repo_root: Path,
    rules: ScanRules,
) -> FileRecord | None:
    """Build one file record, or None when the file is out of scope.

    Args:
        file_path: Absolute path to the candidate file.
        repo_root: Repository root the path is recorded relative to.
        rules: The scanned repository's scan rules.

    Returns:
        The record, or None when the file is a symlink, is unreadable, or
        matches a configured ignore glob.
    """
    if file_path.is_symlink():
        return None

    relative_path = file_path.relative_to(repo_root).as_posix()
    if matches_any_glob(relative_path, rules.ignored_path_globs):
        return None

    try:
        size_bytes = file_path.stat().st_size
    except OSError as exc:
        # A file that cannot be stat'ed is reported and omitted rather than
        # recorded with an invented size.
        logger.warning("Skipping unreadable file %s: %s", relative_path, exc)
        return None

    return FileRecord(
        path=relative_path,
        language=rules.language_by_extension.get(file_path.suffix.lower(), LANGUAGE_UNKNOWN),
        size_bytes=size_bytes,
        is_generated=matches_any_glob(relative_path, rules.generated_path_globs),
        is_vendor=matches_any_glob(relative_path, rules.vendor_path_globs),
        is_test=matches_any_glob(relative_path, rules.test_path_globs),
        # Resolved by entry-point discovery in INC-003.
        entry_point_load_state=ENTRY_POINT_LOAD_UNKNOWN,
    )
