"""Deterministic architecture-boundary guards over generated evidence.

Contract: rey_repository_map_generator.sgc.yaml (INC-005, REQ-090 to REQ-093)
and rey_system_repository_map_correction.sgc.yaml (COR-008).

The one public enforcement entry point. It owns no policy and knows no family:
policy is data supplied by the repository being scanned, and the families that
interpret it live in the registry. This iterates that registry, so a fifth kind
of boundary changes nothing here.

Guards run over parse-tree facts rather than file text, so a call written in a
comment or a string cannot be reported as an offender, and a call spread across
lines cannot hide from a single-line pattern.

Scope is explicit per rule, so a guard covers every surviving root a rule
applies to rather than only the tree its test framework happens to reach
(REQ-093).
"""

from __future__ import annotations

from collections.abc import Sequence

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    DispatcherRecord,
    EntryPointRecord,
    FileRecord,
    GlobalPublicationRecord,
    ReferenceEdge,
    ScanRules,
    ViolationRecord,
)
from rey_lib.repository_map.rule_families import RULE_FAMILIES, Evidence

__all__ = ["check_architecture_boundaries"]

logger = get_logger(__name__)


def check_architecture_boundaries(
    rules: ScanRules,
    *,
    references: Sequence[ReferenceEdge] = (),
    publications: Sequence[GlobalPublicationRecord] = (),
    files: Sequence[FileRecord] = (),
    entry_points: Sequence[EntryPointRecord] = (),
    dispatchers: Sequence[DispatcherRecord] = (),
) -> list[ViolationRecord]:
    """Evaluate every declared architectural boundary against generated facts.

    A consumer asks one question and no subsystem interprets a boundary of its
    own. Which kinds of boundary exist is the registry's business; a boundary
    that cannot be expressed as one of them does not belong here, and
    coding-style rules are not architectural boundaries.

    Args:
        rules: The scanned repository's own scan rules, carrying its policy.
        references: Executable reference edges from extraction.
        publications: Global publications from root discovery.
        files: The inventoried files.
        entry_points: Runtime entry points from root discovery.
        dispatchers: Dispatcher facts from the dispatcher inventory.

    Returns:
        Violations sorted by rule, path and line. Empty when every rule holds.
    """
    evidence = Evidence(
        references=references,
        publications=publications,
        files=files,
        entry_points=entry_points,
        dispatchers=dispatchers,
    )

    violations: list[ViolationRecord] = []
    for family in RULE_FAMILIES:
        violations.extend(family.evaluate(rules.rules_for(family.config_key), evidence))

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
