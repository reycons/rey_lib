"""The increment gate: what one bounded change did, and what an agent needs.

Contract: rey_architecture_enforcement_layer.sgc.yaml (REQ-227, REQ-320 to REQ-322).

Two capabilities the enforcement layer was missing, both about a single bounded
change rather than about a repository as a whole.

The first is telling a new violation from one that was already there. A total
count cannot: a patch that resolves one violation and introduces another leaves
the number unchanged, and a repository carrying known debt would otherwise be
unable to pass any gate until every pre-existing finding was fixed. Migrations
have to be able to improve a repository incrementally without that improvement
being read as permission to add new debt.

The second is the handoff. A coding agent should not have to reconstruct the
architecture from prose on every increment, so it is given the rules that apply
to the change, the manifest, and only the facts about the paths involved. When
the change retires a legacy path, the handoff states the forbidden recovery
moves outright — restoring the old owner, adding a shim, registering a fallback
— because those are the moves a failing migration invites.

Neither function scans anything. Both read generated facts that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rey_lib.repository_map.migration import MigrationManifest
from rey_lib.repository_map.records import matches_any_glob

__all__ = [
    "AgentHandoff",
    "ArchitectureDiff",
    "build_agent_handoff",
    "diff_architecture_violations",
]

_VIOLATION = "architecture_violation"

# The moves a failing migration invites, and which this architecture forbids.
# Stated in the handoff rather than left to memory, because every one of them
# turns a red gate green while leaving two owners behind.
FORBIDDEN_RECOVERIES = (
    "Do not restore the retired implementation. Roll the whole increment back instead.",
    "Do not add a compatibility shim or wrapper that keeps the old path callable.",
    "Do not register a fallback, or publish the old owner on a global surface.",
    "Do not add a second dispatcher or a second registry to preserve the old route.",
    "Do not weaken or delete an architecture rule in the same change as its violation.",
    "A compiler's unused warning is not proof that an input or capability was unnecessary.",
)


@dataclass(frozen=True)
class ArchitectureDiff:
    """Violations introduced, resolved and carried over by one change.

    Attributes:
        introduced: Violations present only after the change.
        resolved: Violations present only before it.
        pre_existing: Violations present on both sides.
    """

    introduced: tuple[dict[str, Any], ...] = ()
    resolved: tuple[dict[str, Any], ...] = ()
    pre_existing: tuple[dict[str, Any], ...] = ()

    @property
    def is_clean(self) -> bool:
        """Return whether the change introduced no new violation.

        Pre-existing findings do not make an increment fail. They were not
        caused by it, and requiring them to be fixed first would make every
        bounded change unbounded.
        """
        return not self.introduced

    def summary(self) -> str:
        """Return a one-line result for a report."""
        return (
            f"{len(self.introduced)} introduced, {len(self.resolved)} resolved, "
            f"{len(self.pre_existing)} pre-existing"
        )


def _violations_by_id(records: "list[dict[str, Any]]") -> dict[str, dict[str, Any]]:
    """Index the violation records of one map by their stable identity."""
    return {
        record["record_id"]: record
        for record in records
        if record.get("record_type") == _VIOLATION
    }


def diff_architecture_violations(
    before_records: "list[dict[str, Any]]",
    after_records: "list[dict[str, Any]]",
) -> ArchitectureDiff:
    """Return which violations one change introduced, resolved and carried over.

    Identity is the violation's own record_id, which already encodes rule, path
    and position. Counting alone would let a change that resolves one violation
    and introduces another look like no change at all.

    Args:
        before_records: Generated records from before the change.
        after_records: Generated records from after it.

    Returns:
        The categorised difference.
    """
    before = _violations_by_id(before_records)
    after = _violations_by_id(after_records)

    introduced = tuple(after[key] for key in sorted(set(after) - set(before)))
    resolved = tuple(before[key] for key in sorted(set(before) - set(after)))
    pre_existing = tuple(after[key] for key in sorted(set(after) & set(before)))
    return ArchitectureDiff(
        introduced=introduced, resolved=resolved, pre_existing=pre_existing
    )


@dataclass(frozen=True)
class AgentHandoff:
    """The bounded evidence and rules for one increment.

    Attributes:
        migration_id: The change this describes.
        capability: What ownership is moving.
        applicable_rules: rule_id to where it was declared.
        manifest_rows: Inputs and capabilities to account for, with dispositions.
        graph_slice: Facts about the paths this change touches, and no others.
        forbidden_recoveries: Moves that must not be used to make the gate pass.
    """

    migration_id: str
    capability: str
    applicable_rules: dict[str, str] = field(default_factory=dict)
    manifest_rows: tuple[dict[str, str], ...] = ()
    graph_slice: tuple[dict[str, Any], ...] = ()
    forbidden_recoveries: tuple[str, ...] = FORBIDDEN_RECOVERIES

    @property
    def unresolved_rows(self) -> tuple[dict[str, str], ...]:
        """Return manifest rows still lacking a disposition."""
        return tuple(row for row in self.manifest_rows if not row.get("disposition"))


def build_agent_handoff(
    manifest: MigrationManifest,
    records: "list[dict[str, Any]]",
    rule_sources: "dict[str, str]",
) -> AgentHandoff:
    """Return only what an agent needs to implement one bounded change.

    The graph slice is restricted to facts touching the old or new owner. An
    agent handed the whole map would be back to rediscovering the architecture,
    which is the cost this exists to remove.

    Args:
        manifest: The declared migration.
        records: Generated fact records for the repository.
        rule_sources: rule_id to where the rule was declared, from the
            effective policy.

    Returns:
        The handoff.
    """
    scope = tuple(manifest.old_owner_path_globs) + tuple(manifest.new_owner_path_globs)

    slice_records: list[dict[str, Any]] = []
    for record in records:
        if record.get("record_type") == _VIOLATION:
            # Every violation is relevant: the agent must not add to them.
            slice_records.append(record)
            continue
        path = str(record.get("source_path", ""))
        target = str(record.get("to", "") or record.get("target", ""))
        if matches_any_glob(path, scope) or matches_any_glob(target, scope):
            slice_records.append(record)

    return AgentHandoff(
        migration_id=manifest.migration_id,
        capability=manifest.capability,
        applicable_rules=dict(rule_sources),
        manifest_rows=tuple(
            {
                "name": row.name,
                "kind": row.kind,
                "disposition": row.disposition,
                "evidence": row.evidence,
                "new_source": row.new_source,
            }
            for row in manifest.rows
        ),
        graph_slice=tuple(slice_records),
    )
