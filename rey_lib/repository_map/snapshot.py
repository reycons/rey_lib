"""One scan of the estate, as a value.

The engine's whole boundary with anything that persists. ``scan`` reads source
and each repository's own scan rules and returns facts; it writes nothing, and
nothing here opens a connection -- a scan that needed a database could not be
reproduced from a bare checkout, which is the property that keeps it reviewable.

**Facts only (backlog 639).** Which repositories are scanned is configuration,
handed in by the caller. Authored architecture is not an input: no architecture
document is read, no declaration is resolved, and nothing declared can stop the
facts of the current code from being scanned. Any architecture model is derived
afterwards from the published index, never the reverse.

The apps root arrives as an argument. Where an estate is checked out is not
rey_lib's to know, and a default would be a guess that silently works on one
machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs.app_logger import AppLogger
from rey_lib.repository_map.writer import RepositoryMap, build_repository_map
from rey_lib.repository_map.inventory import load_scan_rules
from rey_lib.repository_map.records import ScanRules

__all__ = ["CodeIndexSnapshot", "scan"]

logger = AppLogger(__name__)

RULES_NAME = "repository_map.rules.yaml"


@dataclass(frozen=True)
class CodeIndexSnapshot:
    """Everything one scan observed, before any of it is persisted.

    The canonical generated fact model. The relational code index is written
    from this and from nothing else, so it cannot drift from what was actually
    scanned.

    Attributes:
        maps: Repository name to the map this scan produced.
    """

    maps: dict[str, RepositoryMap]


def scan(apps_root: Path, repositories: Sequence[str]) -> CodeIndexSnapshot:
    """Read the configured repositories and return what they say, writing nothing.

    Args:
        apps_root: Directory holding the repository checkouts.
        repositories: The configured scope, one name per checkout under
            ``apps_root``.

    Returns:
        One consistent snapshot.

    Raises:
        ConfigError: If the scope is empty, or a configured repository is not
            checked out. Either would publish an incomplete index as if it
            were the estate.
    """
    if not repositories:
        raise ConfigError("No repositories are configured to scan.")
    logger.info("Scanning %d configured repositories", len(repositories))

    maps: dict[str, RepositoryMap] = {}
    for repository in repositories:
        repo_root = apps_root / repository
        if not repo_root.is_dir():
            raise ConfigError(
                f"Configured repository '{repository}' is not checked out at {repo_root}."
            )
        rules_path = repo_root / RULES_NAME
        # A repository declaring no scan rules is a supported state, not a
        # missing file: it still produces a file inventory.
        declared = rules_path if rules_path.is_file() else None
        rules = load_scan_rules(declared) if declared else ScanRules.unconfigured()
        maps[repository] = build_repository_map(repo_root, rules, declared)
    logger.info("Scanned %d repository maps", len(maps))

    return CodeIndexSnapshot(maps=maps)
