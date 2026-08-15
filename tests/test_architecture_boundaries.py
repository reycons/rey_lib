"""rey_lib's architecture boundaries, asserted through the central authority.

Contract: rey_lib_architecture_policy.sgc.yaml

This repository declares its policy as data in repository_map.rules.yaml. The
generator produces the structural facts and rey_lib.repository_map.boundaries
evaluates the policy against them. This test invokes that one authority and
asserts the result; it encodes no rule, and it reads no source tree.

rey_lib owns both the map implementation and its own repository policy, so this
makes rey_lib a consumer of its own package. That self-consumption is
intentional and recorded in the contract. The alternative — a private checker
inside the repository that owns the shared one — is the second enforcement path
this whole architecture exists to avoid.

Only what the evidence model can prove is asserted here. Statements this
repository is genuinely bound by but the facts cannot demonstrate, such as "no
hard-coded paths", are listed in the contract as not claimed rather than
approximated with a rule that would look like enforcement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map import (
    LANGUAGE_EXTRACTORS,
    extract_executable_references,
    inventory_files,
    load_scan_rules,
)
from rey_lib.repository_map.boundaries import check_architecture_boundaries

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "repository_map.rules.yaml"


@pytest.fixture(scope="module")
def violations():
    """Return every architecture-boundary violation in this repository."""
    rules = load_scan_rules(RULES_PATH)
    files = inventory_files(REPO_ROOT, rules)
    references = []
    for record in files:
        if record.language not in LANGUAGE_EXTRACTORS:
            continue
        if not rules.extracts_facts_from(record):
            continue
        references.extend(
            extract_executable_references(REPO_ROOT / record.path, record.language, record.path)
        )
    return check_architecture_boundaries(rules, references=references, files=files)


def test_this_repository_declares_policy() -> None:
    """A framework nine repositories depend on should not be unjudged."""
    rules = load_scan_rules(RULES_PATH)

    assert rules.rules_for("architecture_rules"), "rey_lib declares no architecture policy."
    assert rules.declares_any_policy is True


def test_the_shared_framework_imports_no_application(violations) -> None:
    """rey_lib is depended upon by nine repositories and depends on none.

    Proves the import relationship only. It is a proxy for one dimension of
    "owns no application business logic" and is not that statement.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "shared_framework_imports_no_application"
    ]

    assert offenders == []


def test_logging_routes_through_the_logging_owner(violations) -> None:
    """Framework code obtains loggers through the rey_lib logging API.

    Known failures when this policy was declared: eleven sites construct a
    logger directly or print, across control_utils, db/procedure_map,
    installation/folder_maker and three logs modules. They are deliberately not
    exempted. This test stays red until they are corrected or reviewed, and
    widening the exemption list to make it green is a stated non-goal — the
    exemptions name infrastructure owners, not sites awaiting repair.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "logging_routes_through_the_logging_owner"
    ]

    assert offenders == []


def test_the_exemption_list_names_only_the_implementation_layer() -> None:
    """One exemption, and it is the module that cannot route through itself.

    Guards the failure mode this policy is most likely to suffer: an exemption
    list quietly growing until the rule holds by construction.
    """
    rules = load_scan_rules(RULES_PATH)
    logging_rule = next(
        rule
        for rule in rules.rules_for("architecture_rules")
        if rule.rule_id == "logging_routes_through_the_logging_owner"
    )

    assert logging_rule.allowed_path_globs == ("rey_lib/logs/logging_setup.py",)


def test_the_test_suite_is_outside_the_logging_rule_scope() -> None:
    """A test asserting logging behaviour must construct a logger.

    That is not architectural drift, so tests are out of scope by path rather
    than by exemption — which keeps the exemption list meaningful.
    """
    rules = load_scan_rules(RULES_PATH)
    logging_rule = next(
        rule
        for rule in rules.rules_for("architecture_rules")
        if rule.rule_id == "logging_routes_through_the_logging_owner"
    )

    assert logging_rule.scope_path_globs == ("rey_lib/*",)
