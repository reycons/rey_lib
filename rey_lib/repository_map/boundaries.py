"""Deterministic architecture-boundary guards over generated evidence.

Contract: rey_repository_map_generator.sgc.yaml (INC-005, REQ-090 to REQ-093).

Ownership is split deliberately. This module owns no policy: it evaluates
rules supplied as data by the repository being scanned, against reference
facts the extractors already proved. The repository owns which boundaries
exist; the generator owns whether they hold.

Guards run over parse-tree reference edges rather than file text, so a call
written inside a comment or a string cannot be reported as an offender, and a
call spread across lines cannot hide from a single-line pattern.

Scope is explicit per rule, so a guard covers every surviving root a rule
applies to rather than only the tree its test framework happens to reach
(REQ-093).
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    EDGE_KIND_GLOBAL_REFERENCE,
    BoundaryRule,
    EntryPointRecord,
    FileRecord,
    GlobalPublicationRecord,
    PresenceRule,
    PublicationRule,
    ReferenceEdge,
    ScanRules,
    ViolationRecord,
)

__all__ = ["check_architecture_boundaries"]

logger = get_logger(__name__)


def check_architecture_boundaries(
    rules: ScanRules,
    *,
    references: Sequence[ReferenceEdge] = (),
    publications: Sequence[GlobalPublicationRecord] = (),
    files: Sequence[FileRecord] = (),
    entry_points: Sequence[EntryPointRecord] = (),
) -> list[ViolationRecord]:
    """Evaluate every declared architectural boundary against generated facts.

    The one entry point. Three rule families are evaluated here so a consumer
    asks a single question and no subsystem interprets a boundary of its own:

    - reference rules, over who may reach what
    - publication rules, over what may reach a global surface
    - presence rules, over what must not exist or be loaded at all

    A boundary that cannot be expressed as one of these does not belong here.
    Coding-style rules are not architectural boundaries and stay outside.

    Args:
        rules: The scanned repository's own scan rules, carrying its policy.
        references: Executable reference edges from extraction.
        publications: Global publications from root discovery.
        files: The inventoried files.
        entry_points: Runtime entry points from root discovery.

    Returns:
        Violations sorted by rule, path and line. Empty when every rule holds.
    """
    violations: list[ViolationRecord] = []
    violations.extend(_reference_violations(rules.boundary_rules, references))
    violations.extend(_publication_violations(rules.publication_rules, publications))
    violations.extend(_presence_violations(rules.presence_rules, files, entry_points))

    violations.sort(
        key=lambda violation: (
            violation.rule_id,
            violation.source_path,
            violation.source_line,
            violation.source_column,
        )
    )
    if violations:
        logger.warning("Architecture guards found %d violation(s)", len(violations))
    return violations


def _reference_violations(
    rules: Sequence[BoundaryRule],
    references: Sequence[ReferenceEdge],
) -> list[ViolationRecord]:
    """Return violations of the rules governing who may reach what.

    Args:
        rules: Reference boundary rules.
        references: Executable reference edges.

    Returns:
        The violations found.
    """
    return [
        ViolationRecord(
            source_path=reference.source_path,
            source_line=reference.source_line,
            source_column=reference.source_column,
            rule_id=rule.rule_id,
            caller=reference.source_path,
            callee=reference.to,
            edge_kind=reference.edge_kind,
            evidence_record_ids=(reference.record_id,),
        )
        for rule in rules
        for reference in references
        if _applies(rule, reference)
    ]


def _publication_violations(
    rules: Sequence[PublicationRule],
    publications: Sequence[GlobalPublicationRecord],
) -> list[ViolationRecord]:
    """Return violations of the rules governing global publication.

    Args:
        rules: Publication rules.
        publications: Global publications from root discovery.

    Returns:
        The violations found.
    """
    return [
        ViolationRecord(
            source_path=publication.source_path,
            source_line=publication.source_line,
            source_column=publication.source_column,
            rule_id=rule.rule_id,
            caller=publication.source_path,
            callee=publication.global_name,
            edge_kind=EDGE_KIND_GLOBAL_REFERENCE,
            evidence_record_ids=(publication.record_id,),
        )
        for rule in rules
        for publication in publications
        if _matches_any(publication.source_path, rule.scope_path_globs)
        and _matches_any(publication.global_name, rule.forbidden_global_globs)
        and not _matches_any(publication.source_path, rule.allowed_path_globs)
    ]


def _presence_violations(
    rules: Sequence[PresenceRule],
    files: Sequence[FileRecord],
    entry_points: Sequence[EntryPointRecord],
) -> list[ViolationRecord]:
    """Return violations of the rules governing what must not exist.

    A file-level violation has no meaningful source line, so it records line 0
    rather than pointing at a line that proves nothing.

    Args:
        rules: Presence rules.
        files: The inventoried files.
        entry_points: Runtime entry points.

    Returns:
        The violations found.
    """
    violations: list[ViolationRecord] = []
    for rule in rules:
        for file_record in files:
            if not _matches_any(file_record.path, rule.forbidden_path_globs):
                continue
            violations.append(
                ViolationRecord(
                    source_path=file_record.path,
                    source_line=0,
                    source_column=0,
                    rule_id=rule.rule_id,
                    caller=file_record.path,
                    callee=file_record.path,
                    edge_kind="file_present",
                    evidence_record_ids=(f"file:{file_record.path}",),
                )
            )
        for entry_point in entry_points:
            if not _matches_any(entry_point.target, rule.forbidden_entry_point_globs):
                continue
            violations.append(
                ViolationRecord(
                    source_path=entry_point.source_path,
                    source_line=entry_point.source_line,
                    source_column=entry_point.source_column,
                    rule_id=rule.rule_id,
                    caller=entry_point.window_or_host,
                    callee=entry_point.target,
                    edge_kind=entry_point.entry_point_kind,
                    evidence_record_ids=(entry_point.record_id,),
                )
            )
    return violations


def _applies(rule: BoundaryRule, reference: ReferenceEdge) -> bool:
    """Return True when a reference breaks a rule.

    A reference breaks a rule when it is in the rule's scope, matches a
    forbidden target, is of a kind the rule covers, and is not made by one of
    the rule's declared owners. Ownership is the exception: an owner may reach
    the mechanism it owns, which is what lets a sanctioned API stay callable
    while the layer beneath it does not.

    Args:
        rule: The rule being evaluated.
        reference: The reference being tested.

    Returns:
        True when the reference is a violation of this rule.
    """
    if rule.edge_kinds and reference.edge_kind not in rule.edge_kinds:
        return False
    if not _matches_any(reference.source_path, rule.scope_path_globs):
        return False
    if not _matches_any(reference.to, rule.forbidden_target_globs):
        return False
    return not _matches_any(reference.source_path, rule.allowed_path_globs)


def _matches_any(value: str, globs: tuple[str, ...]) -> bool:
    """Return True when a value matches any glob.

    Args:
        value: Text to match.
        globs: Configured glob patterns.

    Returns:
        True when at least one glob matches. No globs means no match, so a
        rule that declares no scope guards nothing rather than everything.
    """
    return any(fnmatchcase(value, pattern) for pattern in globs)
