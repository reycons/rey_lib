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

from fnmatch import fnmatchcase

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    BoundaryRule,
    ReferenceEdge,
    ViolationRecord,
)

__all__ = ["check_architecture_boundaries"]

logger = get_logger(__name__)


def check_architecture_boundaries(
    references: list[ReferenceEdge],
    rules: list[BoundaryRule],
) -> list[ViolationRecord]:
    """Evaluate every boundary rule against the executable reference facts.

    Args:
        references: Executable reference edges from extraction.
        rules: The scanned repository's declared boundary rules.

    Returns:
        Violations sorted by rule, path and line. Empty when every rule holds.
    """
    violations: list[ViolationRecord] = []
    for rule in rules:
        for reference in references:
            if not _applies(rule, reference):
                continue
            violations.append(
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
            )

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
