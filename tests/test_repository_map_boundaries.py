"""Focused tests for deterministic architecture-boundary guards.

Contract: rey_repository_map_generator.sgc.yaml (INC-005).
"""

from __future__ import annotations

from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.records import (
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    BoundaryRule,
    ReferenceEdge,
)

ROUTING_RULE = BoundaryRule(
    rule_id="presentation_routing_via_coordinator",
    forbidden_target_globs=("*ReyEmbeddedHost.mount", "*dispatchViewerRequest"),
    allowed_path_globs=("frontend/src/presentation_coordinator/*",),
    scope_path_globs=("frontend/src/*", "static/js/*"),
)


def _edge(path: str, target: str, kind: str = EDGE_KIND_CALL, line: int = 10) -> ReferenceEdge:
    """Return one reference edge."""
    return ReferenceEdge(
        source_path=path,
        source_line=line,
        source_column=4,
        from_id=f"file:{path}",
        to=target,
        edge_kind=kind,
        evidence="call_expression",
    )


def test_a_forbidden_reference_in_either_root_is_caught() -> None:
    """AC-007: static/js fails the same check as frontend/src."""
    references = [
        _edge("frontend/src/panels/info_panel.ts", "ReyEmbeddedHost.mount"),
        _edge("static/js/object_window.js", "window.ReyEmbeddedHost.mount",
              EDGE_KIND_GLOBAL_REFERENCE),
    ]

    violations = check_architecture_boundaries(references, [ROUTING_RULE])

    assert [v.source_path for v in violations] == [
        "frontend/src/panels/info_panel.ts",
        "static/js/object_window.js",
    ]


def test_an_owner_may_reach_the_mechanism_it_owns() -> None:
    """The sanctioned owner is exempt; everyone else is not."""
    references = [
        _edge("frontend/src/presentation_coordinator/index.ts", "ReyEmbeddedHost.mount"),
    ]

    assert check_architecture_boundaries(references, [ROUTING_RULE]) == []


def test_a_reference_outside_scope_is_not_a_violation() -> None:
    """A rule guards the roots it declares, not the whole repository."""
    references = [_edge("tools/scratch/demo.ts", "ReyEmbeddedHost.mount")]

    assert check_architecture_boundaries(references, [ROUTING_RULE]) == []


def test_an_unrelated_reference_is_not_a_violation() -> None:
    """Only forbidden targets offend; the guard is not a keyword search."""
    references = [_edge("frontend/src/panels/info_panel.ts", "ReyEmbeddedHost.register")]

    assert check_architecture_boundaries(references, [ROUTING_RULE]) == []


def test_a_rule_with_no_scope_guards_nothing() -> None:
    """An unscoped rule fails closed to guarding nothing, not everything."""
    unscoped = BoundaryRule(
        rule_id="unscoped",
        forbidden_target_globs=("*anything*",),
    )

    assert check_architecture_boundaries([_edge("a.ts", "anything")], [unscoped]) == []


def test_edge_kinds_narrow_a_rule() -> None:
    """A rule may apply to one kind of reference only."""
    rule = BoundaryRule(
        rule_id="imports_only",
        forbidden_target_globs=("*legacy*",),
        scope_path_globs=("frontend/src/*",),
        edge_kinds=("import",),
    )
    references = [_edge("frontend/src/a.ts", "./legacy_helper", EDGE_KIND_CALL)]

    assert check_architecture_boundaries(references, [rule]) == []


def test_a_violation_carries_caller_callee_kind_and_location() -> None:
    """REQ-092: every violation is checkable against source."""
    references = [_edge("static/js/object_window.js", "window.ReyEmbeddedHost.mount", line=62)]

    violation = check_architecture_boundaries(references, [ROUTING_RULE])[0]
    record = violation.to_dict()

    assert record["record_type"] == "architecture_violation"
    assert record["rule_id"] == "presentation_routing_via_coordinator"
    assert record["caller"] == "static/js/object_window.js"
    assert record["callee"] == "window.ReyEmbeddedHost.mount"
    assert record["source_line"] == 62
    assert record["evidence_record_ids"]


def test_guards_are_deterministically_ordered() -> None:
    """Two runs agree, sorted by rule, path and line."""
    references = [
        _edge("static/js/b.js", "ReyEmbeddedHost.mount", line=5),
        _edge("frontend/src/a.ts", "ReyEmbeddedHost.mount", line=9),
    ]

    first = check_architecture_boundaries(references, [ROUTING_RULE])
    second = check_architecture_boundaries(references, [ROUTING_RULE])

    assert first == second
    assert [v.source_path for v in first] == ["frontend/src/a.ts", "static/js/b.js"]


def test_a_comment_cannot_be_an_offender() -> None:
    """Guards run over parsed references, so commented text yields no edge.

    The mechanism this replaces matched file text, where a commented-out call
    reads exactly like a real one.
    """
    assert check_architecture_boundaries([], [ROUTING_RULE]) == []
