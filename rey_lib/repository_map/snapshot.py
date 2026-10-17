"""One scan of the estate, as a value.

The engine's whole boundary with anything that persists. ``scan`` reads source
and each repository's own scan rules and returns facts; it writes nothing, and
nothing here opens a connection -- a scan that needed a database could not be
reproduced from a bare checkout, which is the property that keeps it reviewable.

**The estate is read, never described (backlog 1029).** The repositories
scanned are the ones checked out under the apps root: every directory directly
under it that is its own git checkout. No list, registration or opt-in says
which they are, so a repository added to the estate is indexed and a retired
one disappears with its checkout.

**Facts only (backlog 639).** Authored architecture is not an input: no
architecture document is read, no declaration is resolved, and nothing declared
can stop the facts of the current code from being scanned. Any architecture
model is derived afterwards from the published index, never the reverse.

The apps root arrives as an argument. Where an estate is checked out is not
rey_lib's to know, and a default would be a guess that silently works on one
machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rey_lib.errors.error_utils import ConfigError
from rey_lib.git.repo import run_git
from rey_lib.logs.app_logger import AppLogger
from rey_lib.repository_map.writer import RepositoryMap, build_repository_map
from rey_lib.repository_map.inventory import load_scan_rules
from rey_lib.repository_map.records import ScanRules

__all__ = ["CodeIndexSnapshot", "estate_repositories", "scan"]

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


def estate_repositories(apps_root: Path) -> list[str]:
    """The repositories checked out under ``apps_root``, by directory name, sorted.

    A repository is a directory directly under the apps root that git reports as
    its own top level. A directory inside some other checkout (an installed
    package tree, for instance) answers that checkout's top level instead, and a
    plain directory answers none, so neither is a repository of the estate.
    Hidden directories are never read.

    Args:
        apps_root: Directory holding the repository checkouts.

    Returns:
        Every checkout's directory name.

    Raises:
        ConfigError: If the apps root is not a directory.
        GitError: If git cannot be run at all.
    """
    if not apps_root.is_dir():
        raise ConfigError(f"The apps root {apps_root} is not a directory.")
    found: list[str] = []
    for entry in sorted(apps_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # check=False: a directory that is no checkout answers non-zero, which
        # is an answer here; a missing git executable still raises.
        answer = run_git(entry, "rev-parse", "--show-toplevel", check=False)
        if answer.returncode == 0 and Path(answer.stdout.strip()).resolve() == entry.resolve():
            found.append(entry.name)
    return found


def scan(apps_root: Path) -> CodeIndexSnapshot:
    """Read every repository of the estate and return what they say, writing nothing.

    Args:
        apps_root: Directory holding the repository checkouts.

    Returns:
        One consistent snapshot of every repository ``estate_repositories``
        finds.

    Raises:
        ConfigError: If no repository is checked out under the apps root, which
            would publish an empty index as if it were the estate.
    """
    repositories = estate_repositories(apps_root)
    if not repositories:
        raise ConfigError(f"No repository is checked out under {apps_root}.")
    logger.info("Scanning %d repositories of the estate", len(repositories))

    # The publication's own checkouts: the only source roots a semantic
    # producer may resolve imports against (backlog 872).
    publication_roots = tuple(apps_root / repository for repository in repositories)
    maps: dict[str, RepositoryMap] = {}
    for repository in repositories:
        repo_root = apps_root / repository
        rules_path = repo_root / RULES_NAME
        # A repository declaring no scan rules is a supported state, not a
        # missing file: it still produces a file inventory.
        declared = rules_path if rules_path.is_file() else None
        rules = load_scan_rules(declared) if declared else ScanRules.unconfigured()
        maps[repository] = build_repository_map(
            repo_root, rules, declared, publication_roots=publication_roots
        )
    logger.info("Scanned %d repository maps", len(maps))

    return CodeIndexSnapshot(maps=maps)
