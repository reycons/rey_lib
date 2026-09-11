"""One scan of the estate, as a value, and the serializer that writes it.

The engine's whole boundary with anything that persists. ``scan`` reads source
and contracts and returns facts; ``serialize`` turns those facts into the
committed artifacts. Neither knows an installation exists, and nothing here
opens a connection -- a scan that needed a database would make the committed
artifact impossible to reproduce from a bare checkout, which is the property
that keeps it reviewable.

Three artifacts, and the later two are derived from the earlier ones:

    repository maps -> system index -> architecture projection

That order is not a preference. A system index binds the maps by content hash,
and the projection joins authored architecture to those same maps, so building
either against a stale predecessor produces an artifact that describes nothing
that exists.

Which repositories participate is read once from
``system_membership.repositories`` and carried through every step, so this is
not a second membership authority. The maps handed to the index and the
projection are the objects this scan just produced: one scan, one snapshot.

**Validation happens before anything is written.** A derived artifact that does
not describe its own inputs means one moved mid-scan, and refusing while it is
still a value in memory leaves nothing half-written on disk.

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
from rey_lib.repository_map.system_index import (
    SystemIndex,
    build_system_index,
    validate_system_index,
    write_system_index,
)
from rey_lib.repository_map.writer import (
    RepositoryMap,
    build_repository_map,
    effective_policy_for,
    write_repository_map,
)
from rey_lib.repository_map.inventory import load_scan_rules
from rey_lib.repository_map.records import ScanRules

__all__ = ["CodeIndexSnapshot", "SYSTEM_INDEX_NAME", "scan", "serialize"]

logger = get_logger(__name__)

SYSTEM_INDEX_NAME = "03_repository_map.system.generated.jsonl"
RULES_NAME = "repository_map.rules.yaml"


@dataclass(frozen=True)
class CodeIndexSnapshot:
    """Everything one scan observed, before any of it is persisted.

    The canonical generated fact model. Both destinations -- the committed
    JSONL and the relational index -- are written from this and from nothing
    else, so neither can drift from what was actually scanned.

    Attributes:
        maps: Repository name to the map this scan produced.
        index: The system index built from those maps.
        projection: The architecture projection joined against them.
    """

    maps: dict[str, RepositoryMap]
    index: SystemIndex
    projection: ArchitectureProjection


def scan(apps_root: Path, context_root: Path) -> CodeIndexSnapshot:
    """Read the estate and return what it says, writing nothing.

    Args:
        apps_root: Directory holding the repository checkouts, one per member.
        context_root: Directory holding the canonical context artifacts. Read
            for membership, authored architecture and each repository's review
            artifact; nothing in it is written.

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

    # Both steps below are handed the map objects this scan produced rather
    # than a set read back from the context directory, so the whole snapshot is
    # one observation. The index does read each repository's review artifact
    # from disk, which is authored rather than generated and is not part of it.
    index = build_system_index(context_root, maps)
    _refuse("system index", validate_system_index(index, maps))

    projection = build_architecture_projection(architecture_path, maps)
    _refuse(
        "architecture projection",
        validate_architecture_projection(projection, maps, architecture_path),
    )
    logger.info("Architecture projection carries %d nodes", len(projection.records))

    return CodeIndexSnapshot(maps=maps, index=index, projection=projection)


def serialize(snapshot: CodeIndexSnapshot, context_root: Path) -> list[Path]:
    """Write one snapshot as the committed artifacts.

    The one canonical serializer. A snapshot read back from the relational
    index is written by this same function, so the two paths to JSONL can
    differ only where the round trip differs -- never in formatting.

    **The architecture projection is not among them.** It is still built and
    still validated -- ``scan`` refuses a snapshot whose projection does not
    describe its own inputs -- but it is no longer written to disk. Its only
    reader was the JSONL-backed Architecture tree, retired in
    tree_retire_jsonl_architecture_root.applied.sql, and the database code
    index is where that reading lives now.

    Args:
        snapshot: What to write.
        context_root: Directory the artifacts belong in.

    Returns:
        Every path written, in the order written.
    """
    written: list[Path] = []
    for repository, repository_map in snapshot.maps.items():
        path = context_root / f"03_repository_map.{repository}.generated.jsonl"
        write_repository_map(repository_map, path)
        written.append(path)

    index_path = context_root / SYSTEM_INDEX_NAME
    write_system_index(snapshot.index, index_path)
    written.append(index_path)

    logger.info("Wrote %d artifacts to %s", len(written), context_root)
    return written


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
