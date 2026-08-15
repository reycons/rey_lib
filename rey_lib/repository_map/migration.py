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

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import read_text_file
from rey_lib.repository_map.records import matches_any_glob

__all__ = [
    "BLOCKER_PROBES",
    "BlockerProbe",
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

    @property
    def retires_a_file(self) -> bool:
        """Return whether the thing being deleted is a file rather than a symbol.

        The distinction changes what counts as a caller. Deleting a file also
        deletes every reference written inside it, so its internal references
        are not callers. Deleting a symbol from a file that survives does not:
        a reference from elsewhere in the same file is a real caller, and
        excluding it would prove a retirement that breaks the moment the symbol
        is gone.
        """
        return bool(self.old_owner_path_globs)


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
    for required in ("migration_id", "capability", "new_owner_path_globs"):
        if not data.get(required):
            raise ValueError(f"Migration manifest {path} is missing '{required}'.")
    if not data.get("old_owner_path_globs") and not data.get("old_symbol_globs"):
        raise ValueError(
            f"Migration manifest {path} names neither old_owner_path_globs nor "
            "old_symbol_globs, so there is nothing it retires."
        )

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


def _dotted_forms(path_globs: "tuple[str, ...]") -> tuple[str, ...]:
    """Return the dotted-module spellings of filesystem path globs.

    A dependency edge names its target the way the source wrote it, which for
    Python is a dotted module path — rey_lib.files.file_utils.read_text_file,
    never rey_lib/files/file_utils.py. Comparing a path glob against those
    matches nothing, so a manifest written in paths would prove retirement
    while every caller was still there. Both spellings are checked.
    """
    forms: list[str] = []
    for glob in path_globs:
        stem = glob[:-3] if glob.endswith(".py") else glob
        dotted = stem.replace("/", ".")
        forms.append(dotted)
        # Anything reached inside the module is also the module.
        forms.append(f"{dotted}.*")
    return tuple(forms)


def _strip_record_prefix(target: str) -> str:
    """Return a reachability target without its record-kind prefix.

    Reachability names its subject as file:<path>, so a bare path glob never
    matches one and a definitely_reachable verdict is skipped in silence.
    """
    _, separator, remainder = target.partition(":")
    return remainder if separator else target


def _is_old_owner(manifest: MigrationManifest, path: str) -> bool:
    """Return whether a path belongs to the retiring owner."""
    return matches_any_glob(_strip_record_prefix(path), manifest.old_owner_path_globs)


def _names_old_owner(manifest: MigrationManifest, target: str) -> bool:
    """Return whether a written target names the retiring owner.

    Checked in every spelling the fact layer uses: the filesystem path, the
    dotted module path an edge actually carries, and any symbol the manifest
    declares.
    """
    return (
        matches_any_glob(target, manifest.old_owner_path_globs)
        or matches_any_glob(target, _dotted_forms(manifest.old_owner_path_globs))
        or matches_any_glob(target, manifest.old_symbol_globs)
    )


@dataclass(frozen=True)
class BlockerProbe:
    """One record type, and what about it can block a retirement.

    Adding a way an old owner stays reachable is one probe plus one entry in
    ``BLOCKER_PROBES``. The gate itself names no record type, so a new kind of
    evidence does not edit it.

    Attributes:
        record_type: The generated record type this probe reads.
        probe: Returns the blockers one record contributes, if any.
    """

    record_type: str
    probe: "Callable[[MigrationManifest, dict[str, Any]], list[RetirementBlocker]]"


def _blocker(record: "dict[str, Any]", kind: str, detail: str, path: str = "") -> RetirementBlocker:
    """Return one blocker built from a generated record."""
    return RetirementBlocker(
        kind=kind,
        source_path=path or str(record.get("source_path", "")),
        source_line=int(record.get("source_line", 0) or 0),
        detail=detail,
        evidence_record_id=str(record.get("record_id", "")),
    )


def _probe_dependency_edge(
    manifest: MigrationManifest, record: "dict[str, Any]"
) -> list[RetirementBlocker]:
    """Return a blocker when something outside the old owner still references it."""
    target = str(record.get("to", ""))
    source = str(record.get("source_path", ""))
    # A reference from inside a retiring file is not a caller, because deleting
    # the file deletes the reference too. When a symbol is retiring from a file
    # that survives, that reasoning does not hold and every reference counts.
    excluded = manifest.retires_a_file and _is_old_owner(manifest, source)
    if _names_old_owner(manifest, target) and not excluded:
        return [
            _blocker(
                record,
                BLOCKER_OLD_CALLER,
                f"{record.get('edge_kind', 'reference')} -> {target}",
            )
        ]
    return []


def _probe_registration(
    manifest: MigrationManifest, record: "dict[str, Any]"
) -> list[RetirementBlocker]:
    """Return a blocker for a registration that keeps the old owner reachable."""
    if _is_old_owner(manifest, str(record.get("source_path", ""))) or _names_old_owner(
        manifest, str(record.get("implementation", ""))
    ):
        return [
            _blocker(
                record,
                BLOCKER_STALE_REGISTRATION,
                f"{record.get('registry')}:{record.get('registered_id')}",
            )
        ]
    if not record.get("registered_id_resolved", True) and not str(
        record.get("implementation", "")
    ).strip():
        # An unreadable id is only unprovable when nothing else identifies the
        # registration. Where the implementation is readable and names something
        # other than the retiring owner, it is ruled out on evidence rather than
        # assumed dangerous. Blocking on every dynamic id anywhere would make
        # retirement permanently unprovable, and a gate that can never pass
        # protects nothing.
        return [
            _blocker(
                record,
                BLOCKER_UNPROVABLE,
                f"registration id {record.get('registered_id')!r} is not a literal and no "
                "implementation identifies it, so it cannot be shown not to name the old owner",
            )
        ]
    return []


def _probe_global_publication(
    manifest: MigrationManifest, record: "dict[str, Any]"
) -> list[RetirementBlocker]:
    """Return a blocker for a global surface that keeps the old owner callable."""
    if _is_old_owner(manifest, str(record.get("source_path", ""))) or _names_old_owner(
        manifest, str(record.get("implementation", ""))
    ):
        return [_blocker(record, BLOCKER_STALE_PUBLICATION, str(record.get("global", "")))]
    return []


def _probe_entry_point(
    manifest: MigrationManifest, record: "dict[str, Any]"
) -> list[RetirementBlocker]:
    """Return a blocker for a runtime root that still loads the old owner."""
    if _names_old_owner(manifest, str(record.get("target", ""))):
        return [_blocker(record, BLOCKER_STALE_ENTRY_POINT, str(record.get("target", "")))]
    return []


def _probe_reachability(
    manifest: MigrationManifest, record: "dict[str, Any]"
) -> list[RetirementBlocker]:
    """Return a blocker when the graph still reaches the old owner, or cannot say."""
    target = str(record.get("target", ""))
    if not _is_old_owner(manifest, target):
        return []
    status = str(record.get("status", ""))
    if status == "definitely_reachable":
        return [
            _blocker(
                record,
                BLOCKER_STILL_REACHABLE,
                f"reachable from {record.get('root')}",
                path=target,
            )
        ]
    if status != "unreferenced_candidate":
        # potentially_reachable or reachability_unknown: not a proof.
        return [
            _blocker(
                record,
                BLOCKER_UNPROVABLE,
                f"reachability is {status}, which does not prove retirement",
                path=target,
            )
        ]
    return []


# The registry. Adding a way an old owner survives is one probe plus one entry.
BLOCKER_PROBES: tuple[BlockerProbe, ...] = (
    BlockerProbe("dependency_edge", _probe_dependency_edge),
    BlockerProbe("registration", _probe_registration),
    BlockerProbe("global_publication", _probe_global_publication),
    BlockerProbe("entry_point", _probe_entry_point),
    BlockerProbe("reachability", _probe_reachability),
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

    Which kinds exist is the registry's business: this function names no record
    type, so a new kind of evidence is one probe and one registration.

    Args:
        manifest: The declared migration.
        records: Generated fact records for the repository, as read from its
            map through the canonical reader.

    Returns:
        The report. VERDICT_PROVEN only when nothing blocks and nothing is
        unprovable.
    """
    probes = {probe.record_type: probe for probe in BLOCKER_PROBES}
    blockers: list[RetirementBlocker] = []
    examined = {probe.record_type: 0 for probe in BLOCKER_PROBES}

    for record in records:
        probe = probes.get(record.get("record_type", ""))
        if probe is None:
            continue
        examined[probe.record_type] += 1
        blockers.extend(probe.probe(manifest, record))

    verdict = VERDICT_PROVEN if not blockers else VERDICT_STOP
    return RetirementReport(
        migration_id=manifest.migration_id,
        verdict=verdict,
        blockers=tuple(blockers),
        examined=examined,
    )
