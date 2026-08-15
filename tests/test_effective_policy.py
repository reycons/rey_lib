"""Focused tests for the merged effective policy and its reported status.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-003, INC-004).

Two inputs answer different questions — how a repository is scanned, and what
its architecture means. They merge into one policy evaluated by the one
existing authority. These tests fix the merge, the provenance that keeps a
violation traceable, and the status the generated header reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.architecture_policy import (
    build_effective_policy,
    compile_architecture_policy,
)
from rey_lib.repository_map.records import ScanRules
from rey_lib.repository_map.rule_families import RULE_FAMILIES
from rey_lib.repository_map.writer import (
    POLICY_EVALUATED,
    POLICY_NOT_CONFIGURED,
    _policy_status,
    effective_policy_for,
)

ARCHITECTURE = """
canonical_ownership:
  repositories:
    shared_lib:
      canonical_modules:
        encryption:
          summary: The sole owner of hashing.
          enforcement:
            family: architecture_rules
            rule_id: hashing_has_one_owner
            forbidden_target_globs: ["hashlib.*"]
            scope_path_globs: ["shared_lib/*"]
    bare_repo:
      canonical_modules:
        thing:
          summary: Architecture that is not a static property.
"""

LOCAL_RULES = {
    "language_by_extension": {".py": "Python"},
    "architecture_rules": [
        {
            "rule_id": "local_rule",
            "forbidden_target_globs": ["forbidden.*"],
            "scope_path_globs": ["*"],
        }
    ],
}


@pytest.fixture()
def architecture(tmp_path: Path) -> Path:
    """Write an architecture document with one annotated repository."""
    path = tmp_path / "01_core_architecture.yaml"
    path.write_text(ARCHITECTURE, encoding="utf-8")
    return path


def _local() -> ScanRules:
    """Return rules declaring one architecture rule of their own."""
    return ScanRules.from_mapping(LOCAL_RULES, RULE_FAMILIES)


def _bare() -> ScanRules:
    """Return rules declaring no policy at all."""
    return ScanRules.from_mapping({"language_by_extension": {".py": "Python"}}, RULE_FAMILIES)


def test_both_sources_merge_into_one_family(architecture: Path) -> None:
    """The merge is per family and additive; neither input overrides the other."""
    policy = build_effective_policy(
        _local(), compile_architecture_policy(architecture, "shared_lib")
    )

    ids = {rule.rule_id for rule in policy.rules.rules_for("architecture_rules")}
    assert ids == {"local_rule", "hashing_has_one_owner"}


def test_a_violation_is_traceable_to_its_declaration(architecture: Path) -> None:
    """With two inputs, a rule_id has to be findable again (AC-013)."""
    policy = build_effective_policy(
        _local(), compile_architecture_policy(architecture, "shared_lib")
    )

    assert policy.source_of("local_rule") == "repository_map.rules.yaml"
    assert "canonical_ownership.shared_lib" in policy.source_of("hashing_has_one_owner")


def test_a_duplicate_rule_id_across_sources_is_refused(architecture: Path) -> None:
    """One rule id must identify one rule, or provenance means nothing."""
    colliding = ScanRules.from_mapping(
        {
            "language_by_extension": {".py": "Python"},
            "architecture_rules": [
                {
                    "rule_id": "hashing_has_one_owner",
                    "forbidden_target_globs": ["x.*"],
                    "scope_path_globs": ["*"],
                }
            ],
        },
        RULE_FAMILIES,
    )

    with pytest.raises(ValueError, match="declared both in"):
        build_effective_policy(
            colliding, compile_architecture_policy(architecture, "shared_lib")
        )


def test_the_merged_policy_is_never_written_to_disk(architecture: Path, tmp_path: Path) -> None:
    """A generated policy file would be a second place to read ownership from."""
    before = set(tmp_path.iterdir())

    effective_policy_for("shared_lib", _local(), architecture)

    assert set(tmp_path.iterdir()) == before


# INC-004. Status is derived from the effective policy, so each combination of
# the two inputs is fixed separately: a repository enforced only by the
# architecture must not report that nothing was configured.


def test_status_local_rules_only(architecture: Path) -> None:
    """A repository declaring its own policy is evaluated."""
    policy = effective_policy_for("bare_repo", _local(), architecture)

    assert _policy_status(policy.rules) == POLICY_EVALUATED


def test_status_architecture_annotations_only(architecture: Path) -> None:
    """Policy from the architecture alone is still policy that ran."""
    policy = effective_policy_for("shared_lib", _bare(), architecture)

    assert policy.rules.rules_for("architecture_rules")
    assert _policy_status(policy.rules) == POLICY_EVALUATED


def test_status_both_sources(architecture: Path) -> None:
    """Both declaring is evaluated."""
    policy = effective_policy_for("shared_lib", _local(), architecture)

    assert _policy_status(policy.rules) == POLICY_EVALUATED


def test_status_neither_source(architecture: Path) -> None:
    """Only a repository governed by nothing reports not_configured."""
    policy = effective_policy_for("bare_repo", _bare(), architecture)

    assert _policy_status(policy.rules) == POLICY_NOT_CONFIGURED


def test_status_is_not_inferred_from_prose_or_filenames(architecture: Path) -> None:
    """bare_repo has architecture prose and no annotation; that is not policy."""
    policy = effective_policy_for("bare_repo", _bare(), architecture)

    assert not policy.rules.declares_any_policy
    assert _policy_status(policy.rules) == POLICY_NOT_CONFIGURED
