"""Focused tests for the ownership and cardinality rule family.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-005).

This family asks a question the reference families cannot express: how many
places own one registered thing. Two registrations of one id are individually
legal edges and only wrong together, so the aggregate is the subject.

Every rule here is tested in both directions. The estate currently has zero
duplicate registrations, so a test that only observed real facts would pass
whether or not the family worked at all.
"""

from __future__ import annotations

import pytest

from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.records import RegistrationRecord, ScanRules
from rey_lib.repository_map.rule_families import RULE_FAMILIES

RULES = {
    "language_by_extension": {".py": "Python"},
    "ownership_rules": [
        {
            "rule_id": "one_owner_per_registered_id",
            "registry_globs": ["ActionRegistry", "OBJECT_REGISTRY.*"],
            "maximum_owners": 1,
        }
    ],
}


def _rules(overrides: dict | None = None) -> ScanRules:
    """Return scan rules declaring one ownership rule."""
    data = dict(RULES)
    if overrides:
        data = {**RULES, "ownership_rules": [{**RULES["ownership_rules"][0], **overrides}]}
    return ScanRules.from_mapping(data, RULE_FAMILIES)


def _registration(path: str, registry: str, registered_id: str, resolved: bool = True):
    """Return one registration fact."""
    return RegistrationRecord(
        source_path=path,
        source_line=10,
        source_column=0,
        registry=registry,
        registered_id=registered_id,
        implementation="Thing",
        registration_kind="call_site",
        registered_id_resolved=resolved,
    )


def _check(rules: ScanRules, registrations) -> list:
    """Evaluate the ownership family through the central authority."""
    return check_architecture_boundaries(rules, registrations=registrations)


def test_one_owner_per_id_passes() -> None:
    """The state the estate is actually in: 60 registrations, no duplicates."""
    violations = _check(
        _rules(),
        [
            _registration("a.py", "ActionRegistry", "run_workflow"),
            _registration("b.py", "ActionRegistry", "close_runner"),
            _registration("c.py", "OBJECT_REGISTRY.client_object", "tree"),
        ],
    )

    assert violations == []


def test_a_duplicate_owner_is_detected() -> None:
    """The negative case. Without this the family could be a no-op."""
    violations = _check(
        _rules(),
        [
            _registration("first.py", "ActionRegistry", "run_workflow"),
            _registration("second.py", "ActionRegistry", "run_workflow"),
        ],
    )

    assert len(violations) == 2
    assert {v.source_path for v in violations} == {"first.py", "second.py"}
    assert {v.edge_kind for v in violations} == {"duplicate_owner"}


def test_every_participating_site_is_reported() -> None:
    """Naming one site would imply the others are correct."""
    violations = _check(
        _rules(),
        [_registration(f"{n}.py", "ActionRegistry", "shared") for n in ("a", "b", "c")],
    )

    assert len(violations) == 3


def test_an_unresolved_id_is_never_counted_as_a_duplicate() -> None:
    """A scanner cannot follow a variable, so two unread ids are not one id.

    Reporting a duplicate from unresolved expressions would be a guess, which
    is the same discipline that keeps reachability from ever concluding dead.
    """
    violations = _check(
        _rules(),
        [
            _registration("a.py", "ActionRegistry", "actionId", resolved=False),
            _registration("b.py", "ActionRegistry", "actionId", resolved=False),
        ],
    )

    assert violations == []


def test_a_registry_outside_the_globs_is_not_governed() -> None:
    """A rule governs the registries it names and no others."""
    violations = _check(
        _rules(),
        [
            _registration("a.py", "SomeOtherRegistry", "dup"),
            _registration("b.py", "SomeOtherRegistry", "dup"),
        ],
    )

    assert violations == []


def test_registering_outside_the_declared_owner_is_a_violation() -> None:
    """Cardinality and ownership are distinct failures in one family."""
    violations = _check(
        _rules({"allowed_path_globs": ["frontend/src/object_registrations/*"]}),
        [_registration("frontend/src/rogue/panel.py", "ActionRegistry", "run_workflow")],
    )

    assert len(violations) == 1
    assert violations[0].edge_kind == "registered_by_non_owner"


def test_the_declared_owner_may_register() -> None:
    """The permitted owner is not reported; an exemption that exempts nothing is noise."""
    violations = _check(
        _rules({"allowed_path_globs": ["frontend/src/object_registrations/*"]}),
        [_registration("frontend/src/object_registrations/index.py", "ActionRegistry", "run")],
    )

    assert violations == []


def test_a_higher_maximum_permits_declared_multiplicity() -> None:
    """Some registries legitimately hold more than one owner per id."""
    violations = _check(
        _rules({"maximum_owners": 2}),
        [
            _registration("a.py", "ActionRegistry", "dup"),
            _registration("b.py", "ActionRegistry", "dup"),
        ],
    )

    assert violations == []


def test_the_family_is_reached_only_through_the_central_authority() -> None:
    """Extension is one implementation plus one registration (COR-008)."""
    assert "ownership_rules" in {family.config_key for family in RULE_FAMILIES}

    import inspect

    from rey_lib.repository_map import boundaries

    source = inspect.getsource(boundaries)
    assert "ownership_rules" not in source, (
        "The central evaluator names the new family; families must be reached "
        "through the registry."
    )


def test_a_violation_carries_the_registration_evidence() -> None:
    """Every verdict points at the fact that produced it."""
    violations = _check(
        _rules(),
        [
            _registration("a.py", "ActionRegistry", "dup"),
            _registration("b.py", "ActionRegistry", "dup"),
        ],
    )

    assert all(v.evidence_record_ids for v in violations)
    assert all(v.callee == "ActionRegistry:dup" for v in violations)
