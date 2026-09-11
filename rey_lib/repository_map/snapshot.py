"""One scan of the estate, as a value.

The engine's whole boundary with anything that persists. ``scan`` reads source
and contracts and returns facts; it writes nothing, and nothing here opens a
connection -- a scan that needed a database could not be reproduced from a bare
checkout, which is the property that keeps it reviewable.

Two things come out of one scan:

    repository maps -> architecture projection

The projection joins authored architecture to those same maps, so building it
against a stale set would describe nothing that exists. It is **not persisted**:
its committed JSONL artifact was retired on 2026-09-11 along with the repository
maps, and the database code index is where structural facts are read from now.
It is still built and still validated, because that is the step which refuses an
estate whose authored architecture no longer resolves against its own code.

Which repositories participate is read once from
``system_membership.repositories`` and carried through, so this is not a second
membership authority. The maps handed to the projection are the objects this
scan just produced: one scan, one snapshot.

**Validation happens before the snapshot is returned.** A projection that does
not describe its own inputs means one moved mid-scan, and refusing while it is
still a value in memory is the whole point of doing it here.

Both roots arrive as arguments. Where an estate is checked out is not rey_lib's
to know, and a default would be a guess that silently works on one machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.architecture_projection import (
    ARCHITECTURE_SOURCE_NAME,
    ArchitectureProjection,
    build_architecture_projection,
    system_membership,
    validate_architecture_projection,
)
from rey_lib.repository_map.writer import (
    RepositoryMap,
    build_repository_map,
    effective_policy_for,
)
from rey_lib.repository_map.inventory import load_scan_rules
from rey_lib.repository_map.records import ScanRules

__all__ = ["CodeIndexSnapshot", "scan"]

logger = get_logger(__name__)

RULES_NAME = "repository_map.rules.yaml"


@dataclass(frozen=True)
class CodeIndexSnapshot:
    """Everything one scan observed, before any of it is persisted.

    The canonical generated fact model. The relational code index is written
    from this and from nothing else, so it cannot drift from what was actually
    scanned.

    Attributes:
        maps: Repository name to the map this scan produced.
        projection: The architecture projection joined against them. Not
            persisted anywhere: it exists so the scan can refuse an estate whose
            authored architecture no longer resolves against its own code.
    """

    maps: dict[str, RepositoryMap]
    projection: ArchitectureProjection


def scan(apps_root: Path, context_root: Path) -> CodeIndexSnapshot:
    """Read the estate and return what it says, writing nothing.

    Args:
        apps_root: Directory holding the repository checkouts, one per member.
        context_root: Directory holding the canonical context artifacts. Read
            for membership and authored architecture; nothing in it is written.

    Returns:
        One consistent snapshot.

    Raises:
        ArchitectureProjectionError: If the architecture context is absent or
            declares no membership, or if a statement cannot be resolved.
        FileNotFoundError: If a declared repository is not checked out.
        ValueError: If a derived artifact does not describe the inputs it was
            just built from, which means one moved during the scan.
    """
    architecture_path = context_root / ARCHITECTURE_SOURCE_NAME
    members = system_membership(architecture_path)
    logger.info("Scanning %d declared members", len(members))

    maps: dict[str, RepositoryMap] = {}
    for repository in members:
        repo_root = apps_root / repository
        if not repo_root.is_dir():
            raise FileNotFoundError(
                f"Declared system member '{repository}' is not checked out at {repo_root}."
            )
        rules_path = repo_root / RULES_NAME
        # A repository declaring no scan rules is a supported state, not a
        # missing file: it still produces a file inventory.
        declared = rules_path if rules_path.is_file() else None
        rules = load_scan_rules(declared) if declared else ScanRules.unconfigured()
        policy = effective_policy_for(repository, rules, architecture_path)
        maps[repository] = build_repository_map(repo_root, policy.rules, declared)
    logger.info("Scanned %d repository maps", len(maps))

    # Handed the map objects this scan produced rather than a set read back
    # from the context directory, so the whole snapshot is one observation.
    projection = build_architecture_projection(architecture_path, maps)
    _refuse(
        "architecture projection",
        validate_architecture_projection(projection, maps, architecture_path),
    )
    logger.info("Architecture projection carries %d nodes", len(projection.records))

    return CodeIndexSnapshot(maps=maps, projection=projection)


def _refuse(artifact: str, reasons: list[str]) -> None:
    """Fail when a derived artifact does not describe its own inputs.

    Args:
        artifact: What was validated, for the message.
        reasons: Why it is stale, empty when it is not.

    Raises:
        ValueError: If there are reasons. Immediately after building there can
            be none, so one means an input moved during the scan.
    """
    if reasons:
        raise ValueError(
            f"The {artifact} does not describe the inputs it was just built from: "
            + "; ".join(reasons)
        )
