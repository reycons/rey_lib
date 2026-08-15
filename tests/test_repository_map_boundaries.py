"""Focused tests for deterministic architecture-boundary guards.

Contract: rey_repository_map_generator.sgc.yaml (INC-005).
"""

from __future__ import annotations

from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.records import (
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    BoundaryRule,
    EntryPointRecord,
    FileRecord,
    GlobalPublicationRecord,
    PresenceRule,
    PublicationRule,
    ReferenceEdge,
    ScanRules,
)


def _rules(**overrides) -> ScanRules:
    """Return scan rules carrying only the policy under test."""
    defaults = dict(
        ignored_directory_names=frozenset(),
        ignored_path_globs=(),
        language_by_extension={},
        generated_path_globs=(),
        vendor_path_globs=(),
        test_path_globs=(),
    )
    defaults.update(overrides)
    return ScanRules(**defaults)


def _check(**kwargs):
    """Evaluate policy through the one authority."""
    rule_kinds = {k: kwargs.pop(k) for k in list(kwargs) if k.endswith("_rules")}
    return check_architecture_boundaries(_rules(**rule_kinds), **kwargs)


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

    violations = _check(boundary_rules=(ROUTING_RULE,), references=references)

    assert [v.source_path for v in violations] == [
        "frontend/src/panels/info_panel.ts",
        "static/js/object_window.js",
    ]


def test_an_owner_may_reach_the_mechanism_it_owns() -> None:
    """The sanctioned owner is exempt; everyone else is not."""
    references = [
        _edge("frontend/src/presentation_coordinator/index.ts", "ReyEmbeddedHost.mount"),
    ]

    assert _check(boundary_rules=(ROUTING_RULE,), references=references) == []


def test_a_reference_outside_scope_is_not_a_violation() -> None:
    """A rule guards the roots it declares, not the whole repository."""
    references = [_edge("tools/scratch/demo.ts", "ReyEmbeddedHost.mount")]

    assert _check(boundary_rules=(ROUTING_RULE,), references=references) == []


def test_an_unrelated_reference_is_not_a_violation() -> None:
    """Only forbidden targets offend; the guard is not a keyword search."""
    references = [_edge("frontend/src/panels/info_panel.ts", "ReyEmbeddedHost.register")]

    assert _check(boundary_rules=(ROUTING_RULE,), references=references) == []


def test_a_rule_with_no_scope_guards_nothing() -> None:
    """An unscoped rule fails closed to guarding nothing, not everything."""
    unscoped = BoundaryRule(
        rule_id="unscoped",
        forbidden_target_globs=("*anything*",),
    )

    assert _check(boundary_rules=(unscoped,), references=[_edge("a.ts", "anything")]) == []


def test_edge_kinds_narrow_a_rule() -> None:
    """A rule may apply to one kind of reference only."""
    rule = BoundaryRule(
        rule_id="imports_only",
        forbidden_target_globs=("*legacy*",),
        scope_path_globs=("frontend/src/*",),
        edge_kinds=("import",),
    )
    references = [_edge("frontend/src/a.ts", "./legacy_helper", EDGE_KIND_CALL)]

    assert _check(boundary_rules=(rule,), references=references) == []


def test_a_violation_carries_caller_callee_kind_and_location() -> None:
    """REQ-092: every violation is checkable against source."""
    references = [_edge("static/js/object_window.js", "window.ReyEmbeddedHost.mount", line=62)]

    violation = _check(boundary_rules=(ROUTING_RULE,), references=references)[0]
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

    first = _check(boundary_rules=(ROUTING_RULE,), references=references)
    second = _check(boundary_rules=(ROUTING_RULE,), references=references)

    assert first == second
    assert [v.source_path for v in first] == ["frontend/src/a.ts", "static/js/b.js"]


def test_a_comment_cannot_be_an_offender() -> None:
    """Guards run over parsed references, so commented text yields no edge.

    The mechanism this replaces matched file text, where a commented-out call
    reads exactly like a real one.
    """
    assert _check(boundary_rules=(ROUTING_RULE,), references=[]) == []


PUBLICATION_RULE = PublicationRule(
    rule_id="viewer_mechanism_not_publicly_reachable",
    forbidden_global_globs=("window.ReyViewerDispatch*",),
    scope_path_globs=("frontend/src/*",),
)

PRESENCE_RULE = PresenceRule(
    rule_id="generic_ui_namespace_stays_deleted",
    forbidden_path_globs=("static/js/components/*",),
    forbidden_entry_point_globs=("*components/widgets.js*",),
)


def _publication(path: str, name: str) -> GlobalPublicationRecord:
    """Return one global publication."""
    return GlobalPublicationRecord(
        source_path=path,
        source_line=7,
        source_column=0,
        global_name=name,
        implementation="{}",
    )


def _file(path: str) -> FileRecord:
    """Return one inventoried file."""
    return FileRecord(
        path=path,
        language="JavaScript",
        size_bytes=1,
        is_generated=False,
        is_vendor=False,
        is_test=False,
    )


def test_publishing_a_forbidden_global_is_a_violation() -> None:
    """A mechanism reached through one entry point stays off the global surface."""
    publications = [
        _publication("frontend/src/viewer_dispatch/index.ts", "window.ReyViewerDispatch")
    ]

    violations = _check(publication_rules=(PUBLICATION_RULE,), publications=publications)

    assert [v.rule_id for v in violations] == ["viewer_mechanism_not_publicly_reachable"]
    assert violations[0].callee == "window.ReyViewerDispatch"


def test_an_unrelated_publication_is_allowed() -> None:
    """Only the named globals are forbidden."""
    publications = [_publication("frontend/src/tree/index.ts", "window.ReyTree")]

    assert _check(publication_rules=(PUBLICATION_RULE,), publications=publications) == []


def test_a_deleted_namespace_reappearing_is_a_violation() -> None:
    """Absence is a structural fact the inventory answers directly."""
    files = [_file("static/js/components/widgets.js")]

    violations = _check(presence_rules=(PRESENCE_RULE,), files=files)

    assert [v.source_path for v in violations] == ["static/js/components/widgets.js"]
    # A file-level violation has no meaningful line.
    assert violations[0].source_line == 0


def test_a_template_loading_a_deleted_namespace_is_a_violation() -> None:
    """Nothing may load what was deleted, even if the file is gone."""
    entry_points = [
        EntryPointRecord(
            source_path="templates/index.html",
            source_line=12,
            source_column=2,
            entry_point_kind="classic_script",
            target="/static/js/components/widgets.js",
            window_or_host="templates/index.html",
        )
    ]

    violations = _check(presence_rules=(PRESENCE_RULE,), entry_points=entry_points)

    assert [v.source_line for v in violations] == [12]
    assert violations[0].caller == "templates/index.html"


def test_all_three_rule_families_answer_one_call() -> None:
    """One authority, asked once, covers references, publications and presence."""
    violations = _check(
        boundary_rules=(ROUTING_RULE,),
        publication_rules=(PUBLICATION_RULE,),
        presence_rules=(PRESENCE_RULE,),
        references=[_edge("static/js/a.js", "ReyEmbeddedHost.mount")],
        publications=[_publication("frontend/src/x/index.ts", "window.ReyViewerDispatch")],
        files=[_file("static/js/components/widgets.js")],
    )

    assert {v.rule_id for v in violations} == {
        "presentation_routing_via_coordinator",
        "viewer_mechanism_not_publicly_reachable",
        "generic_ui_namespace_stays_deleted",
    }
