"""Focused tests for the violation diff and the bounded agent handoff.

Contract: rey_architecture_enforcement_layer.sgc.yaml (REQ-227, REQ-320 to REQ-322).

These close the two acceptance items INC-007 left qualified.
"""

from __future__ import annotations

from rey_lib.repository_map.increment_gate import (
    FORBIDDEN_RECOVERIES,
    build_agent_handoff,
    diff_architecture_violations,
)
from rey_lib.repository_map.migration import MigrationManifest, MigrationRow


def _violation(rule: str, path: str, line: int = 1) -> dict:
    """Return one architecture_violation record."""
    return {
        "record_type": "architecture_violation",
        "record_id": f"violation:{rule}:{path}:{line}:0",
        "rule_id": rule,
        "source_path": path,
        "source_line": line,
        "caller": path,
        "callee": "x",
        "edge_kind": "call",
        "evidence_record_ids": [f"edge:{path}"],
    }


def test_a_new_violation_is_distinguished_from_a_pre_existing_one() -> None:
    """The question the increment gate is actually built on."""
    before = [_violation("old_rule", "a.py")]
    after = [_violation("old_rule", "a.py"), _violation("new_rule", "b.py")]

    diff = diff_architecture_violations(before, after)

    assert [v["rule_id"] for v in diff.introduced] == ["new_rule"]
    assert [v["rule_id"] for v in diff.pre_existing] == ["old_rule"]
    assert diff.resolved == ()
    assert not diff.is_clean


def test_resolving_one_and_introducing_another_is_not_clean() -> None:
    """A count cannot see this: the total is unchanged at one."""
    before = [_violation("rule_a", "a.py")]
    after = [_violation("rule_b", "b.py")]

    diff = diff_architecture_violations(before, after)

    assert len(before) == len(after)
    assert not diff.is_clean
    assert [v["rule_id"] for v in diff.introduced] == ["rule_b"]
    assert [v["rule_id"] for v in diff.resolved] == ["rule_a"]


def test_pre_existing_debt_does_not_fail_an_increment() -> None:
    """Otherwise every bounded change becomes unbounded.

    rey_console carries two known routing violations. A migration elsewhere in
    it must be able to pass without fixing them first.
    """
    known = [_violation("presentation_routing_via_coordinator", "static/js/object_window.js")]

    diff = diff_architecture_violations(known, known)

    assert diff.is_clean
    assert len(diff.pre_existing) == 1


def test_a_clean_increment_resolves_and_introduces_nothing() -> None:
    """The state INC-007 ended in."""
    diff = diff_architecture_violations([], [])

    assert diff.is_clean
    assert diff.summary() == "0 introduced, 0 resolved, 0 pre-existing"


def test_a_migration_that_removes_a_violation_is_reported_as_resolved() -> None:
    """INC-007 closed delimited_format_has_one_owner; that must be visible."""
    before = [_violation("delimited_format_has_one_owner", "rey_lib/files/file_utils.py")]

    diff = diff_architecture_violations(before, [])

    assert diff.is_clean
    assert [v["rule_id"] for v in diff.resolved] == ["delimited_format_has_one_owner"]


# The handoff.


MANIFEST = MigrationManifest(
    migration_id="retire_old",
    capability="pane placement",
    old_owner_path_globs=("static/js/old.js",),
    new_owner_path_globs=("frontend/src/new/*",),
    rows=(
        MigrationRow(name="host", disposition="moved_to_new_owner", evidence="new owner acquires it"),
        MigrationRow(name="placement"),
    ),
)


def _facts() -> list[dict]:
    """Return facts, only some of which touch the change."""
    return [
        {"record_type": "dependency_edge", "record_id": "e1",
         "source_path": "frontend/src/new/index.ts", "to": "x", "source_line": 1},
        {"record_type": "dependency_edge", "record_id": "e2",
         "source_path": "unrelated/module.ts", "to": "y", "source_line": 1},
        {"record_type": "file", "record_id": "f1", "source_path": "static/js/old.js"},
        _violation("some_rule", "elsewhere.ts"),
    ]


def test_the_handoff_carries_only_the_facts_the_change_touches() -> None:
    """An agent handed the whole map is back to rediscovering the architecture."""
    handoff = build_agent_handoff(MANIFEST, _facts(), {"some_rule": "rules.yaml"})

    paths = {r.get("source_path") for r in handoff.graph_slice}
    assert "unrelated/module.ts" not in paths
    assert "frontend/src/new/index.ts" in paths
    assert "static/js/old.js" in paths


def test_every_violation_reaches_the_handoff_regardless_of_scope() -> None:
    """The agent must not add to violations it was never shown."""
    handoff = build_agent_handoff(MANIFEST, _facts(), {})

    assert any(r.get("record_type") == "architecture_violation" for r in handoff.graph_slice)


def test_the_handoff_names_the_forbidden_recovery_moves() -> None:
    """REQ-322. These are the moves a failing migration invites."""
    handoff = build_agent_handoff(MANIFEST, _facts(), {})

    text = " ".join(handoff.forbidden_recoveries).lower()
    for move in ("shim", "fallback", "second dispatcher", "unused warning"):
        assert move in text
    assert handoff.forbidden_recoveries == FORBIDDEN_RECOVERIES


def test_the_handoff_reports_rules_with_their_source() -> None:
    """Two policy inputs, so a rule has to be findable again."""
    handoff = build_agent_handoff(
        MANIFEST, _facts(), {"some_rule": "01_core_architecture.yaml:canonical_ownership.x"}
    )

    assert handoff.applicable_rules["some_rule"].startswith("01_core_architecture.yaml")


def test_the_handoff_surfaces_unresolved_manifest_rows() -> None:
    """A row without a disposition is how a capability disappears in a move."""
    handoff = build_agent_handoff(MANIFEST, _facts(), {})

    assert [row["name"] for row in handoff.unresolved_rows] == ["placement"]
