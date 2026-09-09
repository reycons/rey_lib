"""The one command that regenerates the context artifacts, in dependency order.

Three artifacts, and the later two are derived from the earlier ones:

    repository maps -> system index -> architecture projection

That order is not a preference. A system index binds the maps by content hash,
and the projection joins authored architecture to those same maps, so building
either against a stale predecessor produces an artifact that describes nothing
that exists. Documented steps a person pastes in cannot enforce an order; a
command can, which is why this exists.

It is orchestration and nothing else. Which repositories participate is read
once from ``system_membership.repositories`` and carried through every step, so
this command is not a second membership authority -- and the maps handed to the
index and the projection are the objects this run just produced. One run, one
snapshot.

Both roots arrive as arguments. Where an estate is checked out is not
rey_lib's to know, and a default would be a guess that silently works on one
machine.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.architecture_projection import (
    ARCHITECTURE_ARTIFACT_NAME,
    ARCHITECTURE_SOURCE_NAME,
    ArchitectureProjection,
    ArchitectureProjectionError,
    build_architecture_projection,
    system_membership,
    validate_architecture_projection,
    write_architecture_projection,
)
from rey_lib.repository_map.system_index import (
    SystemIndex,
    build_system_index,
    validate_system_index,
    write_system_index,
)
from rey_lib.repository_map.writer import RepositoryMap, generate_repository_map

__all__ = ["RegenerationResult", "main", "regenerate_context_maps"]

logger = get_logger(__name__)

SYSTEM_INDEX_NAME = "03_repository_map.system.generated.jsonl"
RULES_NAME = "repository_map.rules.yaml"


@dataclass(frozen=True)
class RegenerationResult:
    """What one complete regeneration produced.

    Attributes:
        maps: Repository name to the map this run generated.
        index: The system index built from those maps.
        projection: The architecture projection joined against them.
    """

    maps: dict[str, RepositoryMap]
    index: SystemIndex
    projection: ArchitectureProjection


def regenerate_context_maps(apps_root: Path, context_root: Path) -> RegenerationResult:
    """Regenerate every context artifact from one generation snapshot.

    Args:
        apps_root: Directory holding the repository checkouts, one per member.
        context_root: Directory holding the canonical context artifacts.

    Returns:
        Everything this run produced.

    Raises:
        ArchitectureProjectionError: If the architecture context is absent or
            declares no membership, or if a statement cannot be resolved.
        FileNotFoundError: If a declared repository is not checked out.
        ValueError: If a derived artifact does not describe the inputs it was
            just built from, which means one moved during the run.
    """
    architecture_path = context_root / ARCHITECTURE_SOURCE_NAME
    members = system_membership(architecture_path)
    logger.info("Regenerating context artifacts for %d declared members", len(members))

    maps: dict[str, RepositoryMap] = {}
    for repository in members:
        repo_root = apps_root / repository
        if not repo_root.is_dir():
            raise FileNotFoundError(
                f"Declared system member '{repository}' is not checked out at {repo_root}."
            )
        rules_path = repo_root / RULES_NAME
        maps[repository] = generate_repository_map(
            repo_root=repo_root,
            output_path=context_root / f"03_repository_map.{repository}.generated.jsonl",
            # A repository declaring no scan rules is a supported state, not a
            # missing file: it still produces a file inventory.
            rules_path=rules_path if rules_path.is_file() else None,
            architecture_path=architecture_path,
        )
    logger.info("Regenerated %d repository maps", len(maps))

    # Both steps below are handed the map objects this run produced rather than
    # a set read back from the context directory, so the whole command works on
    # one snapshot. The index does read each repository's review artifact from
    # disk, which is authored rather than generated and is not part of it.
    index = build_system_index(context_root, maps)
    write_system_index(index, context_root / SYSTEM_INDEX_NAME)
    _refuse("system index", validate_system_index(index, maps))

    projection = build_architecture_projection(architecture_path, maps)
    write_architecture_projection(projection, context_root / ARCHITECTURE_ARTIFACT_NAME)
    _refuse(
        "architecture projection",
        validate_architecture_projection(projection, maps, architecture_path),
    )
    logger.info("Architecture projection carries %d nodes", len(projection.records))

    return RegenerationResult(maps=maps, index=index, projection=projection)


def _refuse(artifact: str, reasons: list[str]) -> None:
    """Fail when a freshly written artifact does not describe its own inputs.

    Args:
        artifact: What was validated, for the message.
        reasons: Why it is stale, empty when it is not.

    Raises:
        ValueError: If there are reasons. Immediately after writing there can
            be none, so one means an input moved during the run.
    """
    if reasons:
        raise ValueError(
            f"The {artifact} does not describe the inputs it was just built from: "
            + "; ".join(reasons)
        )


def main(argv: list[str] | None = None) -> int:
    """Run the regeneration from the command line.

    Args:
        argv: Arguments to parse. None reads them from the process.

    Returns:
        Process exit status: 0 when every artifact was regenerated.
    """
    parser = argparse.ArgumentParser(
        prog="rey-regenerate-context-maps",
        description=(
            "Regenerate the repository maps, the system index and the architecture "
            "projection, in that order, from one generation snapshot."
        ),
    )
    parser.add_argument(
        "--apps",
        required=True,
        type=Path,
        help="Directory holding the repository checkouts, one per declared member.",
    )
    parser.add_argument(
        "--context",
        required=True,
        type=Path,
        help="Directory holding the canonical context artifacts.",
    )
    args = parser.parse_args(argv)

    try:
        result = regenerate_context_maps(args.apps, args.context)
    except (ArchitectureProjectionError, FileNotFoundError, ValueError) as exc:
        # Contract and input failures are reported: a person ran this, and the
        # contradiction that stopped it is a better answer than a traceback. A
        # programming defect is not caught here and still raises.
        logger.error("Regeneration stopped: %s", exc)
        return 1

    logger.info(
        "Regenerated %d repository maps, the system index, and %d architecture nodes",
        len(result.maps),
        len(result.projection.records),
    )
    return 0
