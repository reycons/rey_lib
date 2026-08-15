"""The ownership-migration manifest, retirement gate and STOP state.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-006).

An ownership migration runs in one order and no other:

    establish the new owner -> repoint consumers -> prove zero old callers
    and readers -> delete the old owner

This module owns the third step, which is the one a coding agent cannot be
trusted to answer about its own patch. Retirement is proven from generated
facts: reference edges, registrations, global publications, entry points and
reachability. It is never inferred from a compiler's unused warning, which
reports what a build could not see rather than what a system cannot reach.

The gate has two outcomes and no third. Either the evidence proves the old
owner is unreachable, or the result is STOP. STOP is not a failure to be
worked around, and it does not mean the migration is wrong — it means the
current evidence does not prove retirement is safe. Nothing here edits code:
the gate reports, and a person decides.

Absence of evidence is never proof of absence. A registration whose id could
not be read, or a reachability verdict that is unknown, produces STOP rather
than silence, because a scanner that cannot see a caller has not shown there
is none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import read_text_file
from rey_lib.repository_map.records import matches_any_glob

__all__ = [
    "DISPOSITIONS",
    "MigrationManifest",
    "MigrationRow",
    "RetirementBlocker",
    "RetirementReport",
    "VERDICT_PROVEN",
    "VERDICT_STOP",
    "load_migration_manifest",
    "validate_migration_manifest",
    "verify_retirement_ready",
]

# What may become of one input or capability the old owner had. A row without
# one of these is unresolved, and an unresolved row is why REQ-248 refuses to
# let a migration enter implementation.
DISPOSITION_PRESERVED = "preserved"
DISPOSITION_MOVED = "moved_to_new_owner"
DISPOSITION_REPLACED = "replaced_by_canonical_capability"
DISPOSITION_OBSOLETE = "proven_obsolete"
DISPOSITIONS = frozenset(
    {
        DISPOSITION_PRESERVED,
        DISPOSITION_MOVED,
        DISPOSITION_REPLACED,
        DISPOSITION_OBSOLETE,
    }
)

VERDICT_PROVEN = "retirement_proven"
VERDICT_STOP = "stop"

# Why a retirement is not proven. The kind matters: a remaining caller is a
# migration that is not finished, while an unprovable reference is a migration
# whose safety is unknown. Collapsing them would let the second read as the
# first and be "fixed" by deleting something.
BLOCKER_OLD_CALLER = "old_caller"
BLOCKER_STALE_REGISTRATION = "stale_registration"
BLOCKER_STALE_PUBLICATION = "stale_publication"
BLOCKER_STALE_ENTRY_POINT = "stale_entry_point"
BLOCKER_STILL_REACHABLE = "still_reachable"
BLOCKER_UNPROVABLE = "unprovable_reference"


@dataclass(frozen=True)
class MigrationRow:
    """One input or capability the old owner had, and what became of it.

    Attributes:
        name: What the old call site resolved or performed.
        kind: input or capability.
        disposition: One of DISPOSITIONS, or empty when undecided.
        evidence: Why that disposition is true.
        new_source: Where a preserved value now comes from.
    """

    name: str
    kind: str = "input"
    disposition: str = ""
    evidence: str = ""
    new_source: str = ""

    @property
    def is_resolved(self) -> bool:
        """Return whether this row has a disposition backed by evidence."""
        return self.disposition in DISPOSITIONS and bool(self.evidence)


@dataclass(frozen=True)
class MigrationManifest:
    """One bounded ownership transfer, declared before code changes.

    This records what is being migrated and the ownership state expected on
    each side. It deliberately restates none of the repository map: the facts
    stay machine-owned, and this names which of them to ask about.

    Attributes:
        migration_id: Stable identity of this migration.
        capability: What ownership is moving.
        old_owner_path_globs: Paths that constitute the retiring owner.
        new_owner_path_globs: Paths that constitute the canonical owner.
        old_symbol_globs: Symbols the old owner exposed, for edges that name a
            symbol rather than a path.
        rows: Inputs and capabilities the old call site had.
    """

    migration_id: str
    capability: str
    old_owner_path_globs: tuple[str, ...]
    new_owner_path_globs: tuple[str, ...]
    old_symbol_globs: tuple[str, ...] = ()
    rows: tuple[MigrationRow, ...] = ()

    @property
    def unresolved_rows(self) -> tuple[MigrationRow, ...]:
        """Return rows carrying no disposition and evidence."""
        return tuple(row for row in self.rows if not row.is_resolved)


@dataclass(frozen=True)
class RetirementBlocker:
    """One reason a retirement is not proven.

    Attributes:
        kind: One of the BLOCKER_* constants.
        source_path: Where the blocking fact lives.
        source_line: Line of the blocking fact, 0 when it has none.
        detail: What was found, in the fact's own words.
        evidence_record_id: The generated record this came from.
    """

    kind: str
    source_path: str
    source_line: int
    detail: str
    evidence_record_id: str


@dataclass(frozen=True)
class RetirementReport:
    """Whether the evidence proves an old owner can be deleted.

    Attributes:
        migration_id: The migration this answers for.
        verdict: VERDICT_PROVEN or VERDICT_STOP.
        blockers: Every reason retirement is not proven.
        examined: How many records of each type were considered, so a clean
            result is distinguishable from a scan that examined nothing.
    """

    migration_id: str
    verdict: str
    blockers: tuple[RetirementBlocker, ...] = ()
    examined: dict[str, int] = field(default_factory=dict)

    @property
    def is_proven(self) -> bool:
        """Return whether the old owner may be deleted."""
        return self.verdict == VERDICT_PROVEN

    def blockers_of(self, kind: str) -> tuple[RetirementBlocker, ...]:
        """Return the blockers of one kind."""
        return tuple(blocker for blocker in self.blockers if blocker.kind == kind)


def load_migration_manifest(path: Path) -> MigrationManifest:
    """Load a migration manifest from its declaration.

    Args:
        path: Path to the manifest.

    Returns:
        The typed manifest.

    Raises:
        FileNotFoundError: If the manifest is absent. The path is configuration
            and is never guessed.
        ValueError: If the manifest is not valid YAML or omits a required field.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Migration manifest not found: {path}")
    try:
        parsed = parse_yaml(read_text_file(path))
    except Exception as exc:  # Surface the offending file, not a bare parse error.
        raise ValueError(f"Migration manifest is not valid YAML: {path}") from exc

    data = (parsed or {}).get("migration", parsed) or {}
    for required in ("migration_id", "capability", "old_owner_path_globs", "new_owner_path_globs"):
        if not data.get(required):
            raise ValueError(f"Migration manifest {path} is missing '{required}'.")

    return MigrationManifest(
        migration_id=data["migration_id"],
        capability=data["capability"],
        old_owner_path_globs=tuple(data["old_owner_path_globs"]),
        new_owner_path_globs=tuple(data["new_owner_path_globs"]),
        old_symbol_globs=tuple(data.get("old_symbol_globs", ())),
        rows=tuple(
            MigrationRow(
                name=row.get("name", ""),
                kind=row.get("kind", "input"),
                disposition=row.get("disposition", ""),
                evidence=row.get("evidence", ""),
                new_source=row.get("new_source", ""),
            )
            for row in data.get("rows", ())
        ),
    )


def validate_migration_manifest(manifest: MigrationManifest) -> list[str]:
    """Return why a manifest may not enter implementation.

    Every input and capability the old call site had must have a disposition
    and evidence. A row left undecided is the mechanism by which a value
    disappears during a move and nobody notices until it is needed.

    Args:
        manifest: The manifest to check.

    Returns:
        Problems, empty when the manifest is complete.
    """
    problems: list[str] = []
    for row in manifest.unresolved_rows:
        if row.disposition and row.disposition not in DISPOSITIONS:
            problems.append(
                f"Row {row.name!r} has unknown disposition {row.disposition!r}; "
                f"expected one of {sorted(DISPOSITIONS)}."
            )
        elif not row.disposition:
            problems.append(f"Row {row.name!r} has no disposition.")
        else:
            problems.append(f"Row {row.name!r} has disposition but no evidence.")
    for row in manifest.rows:
        if row.disposition == DISPOSITION_PRESERVED and not row.new_source:
            problems.append(
                f"Row {row.name!r} is preserved but names no new source. A preserved value "
                "has to come from somewhere."
            )
    return problems


def _is_old_owner(manifest: MigrationManifest, path: str) -> bool:
    """Return whether a path belongs to the retiring owner."""
    return matches_any_glob(path, manifest.old_owner_path_globs)


def _names_old_owner(manifest: MigrationManifest, target: str) -> bool:
    """Return whether a written target names the retiring owner."""
    return matches_any_glob(target, manifest.old_owner_path_globs) or matches_any_glob(
        target, manifest.old_symbol_globs
    )


def verify_retirement_ready(
    manifest: MigrationManifest,
    records: "list[dict[str, Any]]",
) -> RetirementReport:
    """Return whether generated evidence proves the old owner can be deleted.

    Every reachability kind the graph distinguishes is asked separately, because
    a caller removed from source can survive as a registration id, a global
    publication or a template entry point. Deleting on the strength of the
    direct-call check alone is how a retirement passes and the system breaks.

    Args:
        manifest: The declared migration.
        records: Generated fact records for the repository, as read from its
            map through the canonical reader.

    Returns:
        The report. VERDICT_PROVEN only when nothing blocks and nothing is
        unprovable.
    """
    blockers: list[RetirementBlocker] = []
    examined = {
        "dependency_edge": 0,
        "registration": 0,
        "global_publication": 0,
        "entry_point": 0,
        "reachability": 0,
    }

    for record in records:
        kind = record.get("record_type")
        if kind not in examined:
            continue
        examined[kind] += 1

        if kind == "dependency_edge":
            target = str(record.get("to", ""))
            source = str(record.get("source_path", ""))
            # A reference from inside the old owner to itself is not a caller.
            if _names_old_owner(manifest, target) and not _is_old_owner(manifest, source):
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_OLD_CALLER,
                        source_path=source,
                        source_line=int(record.get("source_line", 0)),
                        detail=f"{record.get('edge_kind', 'reference')} -> {target}",
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )

        elif kind == "registration":
            if _is_old_owner(manifest, str(record.get("source_path", ""))) or _names_old_owner(
                manifest, str(record.get("implementation", ""))
            ):
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_STALE_REGISTRATION,
                        source_path=str(record.get("source_path", "")),
                        source_line=int(record.get("source_line", 0)),
                        detail=f"{record.get('registry')}:{record.get('registered_id')}",
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )
            elif not record.get("registered_id_resolved", True) and not str(
                record.get("implementation", "")
            ).strip():
                # An unreadable id is only unprovable when nothing else identifies
                # the registration. Where the implementation is readable and names
                # something other than the retiring owner, the registration is
                # ruled out on evidence rather than assumed dangerous. Blocking on
                # every dynamic id anywhere would make retirement permanently
                # unprovable in any repository that has one, and a gate that can
                # never pass protects nothing.
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_UNPROVABLE,
                        source_path=str(record.get("source_path", "")),
                        source_line=int(record.get("source_line", 0)),
                        detail=(
                            f"registration id {record.get('registered_id')!r} is not a literal "
                            "and no implementation identifies it, so it cannot be shown not to "
                            "name the old owner"
                        ),
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )

        elif kind == "global_publication":
            if _is_old_owner(manifest, str(record.get("source_path", ""))) or _names_old_owner(
                manifest, str(record.get("implementation", ""))
            ):
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_STALE_PUBLICATION,
                        source_path=str(record.get("source_path", "")),
                        source_line=int(record.get("source_line", 0)),
                        detail=str(record.get("global", "")),
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )

        elif kind == "entry_point":
            if _names_old_owner(manifest, str(record.get("target", ""))):
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_STALE_ENTRY_POINT,
                        source_path=str(record.get("source_path", "")),
                        source_line=int(record.get("source_line", 0)),
                        detail=str(record.get("target", "")),
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )

        elif kind == "reachability":
            target = str(record.get("target", ""))
            if not _is_old_owner(manifest, target):
                continue
            status = str(record.get("status", ""))
            if status == "definitely_reachable":
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_STILL_REACHABLE,
                        source_path=target,
                        source_line=0,
                        detail=f"reachable from {record.get('root')}",
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )
            elif status not in ("unreferenced_candidate",):
                # potentially_reachable or reachability_unknown: not a proof.
                blockers.append(
                    RetirementBlocker(
                        kind=BLOCKER_UNPROVABLE,
                        source_path=target,
                        source_line=0,
                        detail=f"reachability is {status}, which does not prove retirement",
                        evidence_record_id=str(record.get("record_id", "")),
                    )
                )

    verdict = VERDICT_PROVEN if not blockers else VERDICT_STOP
    return RetirementReport(
        migration_id=manifest.migration_id,
        verdict=verdict,
        blockers=tuple(blockers),
        examined=examined,
    )
