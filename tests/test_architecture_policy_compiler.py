"""Focused tests for compiling architecture annotations into rule payloads.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-002).

The compiler owns no schema of its own. It resolves a family from the registry
and hands the entry to that family's build function, so these tests assert the
delegation and the failure modes rather than re-testing any family's fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.architecture_policy import (
    ArchitectureRuleSource,
    compile_architecture_policy,
)
from rey_lib.repository_map.rule_families import RULE_FAMILIES

DOC = """
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
            allowed_path_globs: ["shared_lib/encryption.py"]
            scope_path_globs: ["shared_lib/*"]
        plain_prose:
          summary: Real architecture that is not a static property.
      canonical_symbols:
        - summary: shared_lib.files.csv (sole owner of delimited format)
          enforcement:
            family: architecture_rules
            rule_id: delimited_has_one_owner
            forbidden_target_globs: ["csv.*"]
            scope_path_globs: ["shared_lib/*"]
    other_repo:
      canonical_modules:
        thing:
          summary: Unannotated only.
"""


@pytest.fixture()
def doc(tmp_path: Path) -> Path:
    """Write a small architecture document."""
    path = tmp_path / "01_core_architecture.yaml"
    path.write_text(DOC, encoding="utf-8")
    return path


def test_annotations_compile_through_the_owning_family(doc: Path) -> None:
    """Rules are built by the family the annotation names, not here."""
    compiled = compile_architecture_policy(doc, "shared_lib")

    rules = compiled.rule_sets["architecture_rules"]
    assert {rule.rule_id for rule in rules} == {"hashing_has_one_owner", "delimited_has_one_owner"}
    # The typed rule is whatever the family builds; the compiler adds no shape.
    built = next(r for r in rules if r.rule_id == "hashing_has_one_owner")
    assert built.forbidden_target_globs == ("hashlib.*",)
    assert built.allowed_path_globs == ("shared_lib/encryption.py",)


def test_an_unannotated_statement_compiles_to_nothing(doc: Path) -> None:
    """Absence of an enforcement block is a recorded state, not an input."""
    compiled = compile_architecture_policy(doc, "shared_lib")

    ids = {rule.rule_id for rule in compiled.rule_sets["architecture_rules"]}
    assert "plain_prose" not in ids
    assert len(ids) == 2


def test_a_repository_declaring_nothing_is_empty(doc: Path) -> None:
    """An unannotated repository yields no policy rather than an error."""
    assert compile_architecture_policy(doc, "other_repo").is_empty
    assert compile_architecture_policy(doc, "absent_repo").is_empty


def test_provenance_locates_the_declaration(doc: Path) -> None:
    """A violation names a rule_id; the reviewer must find the statement again."""
    compiled = compile_architecture_policy(doc, "shared_lib")

    source = compiled.provenance["hashing_has_one_owner"]
    assert source == ArchitectureRuleSource(
        repository="shared_lib",
        section="canonical_modules",
        statement_key="encryption",
        config_key="architecture_rules",
    )
    assert "canonical_ownership.shared_lib.canonical_modules[encryption]" in source.describe()


def test_an_unknown_family_fails_loudly(tmp_path: Path) -> None:
    """A mistyped family must not read as a statement nobody chose to enforce."""
    path = tmp_path / "arch.yaml"
    path.write_text(
        "canonical_ownership:\n"
        "  repositories:\n"
        "    shared_lib:\n"
        "      canonical_modules:\n"
        "        thing:\n"
        "          summary: x\n"
        "          enforcement:\n"
        "            family: refrence_rules\n"
        "            rule_id: typo\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown rule family"):
        compile_architecture_policy(path, "shared_lib")


def test_malformed_fields_fail_through_the_owning_family(tmp_path: Path) -> None:
    """The family owns its schema; the compiler does not re-validate fields."""
    path = tmp_path / "arch.yaml"
    path.write_text(
        "canonical_ownership:\n"
        "  repositories:\n"
        "    shared_lib:\n"
        "      canonical_modules:\n"
        "        thing:\n"
        "          summary: x\n"
        "          enforcement:\n"
        "            family: architecture_rules\n"
        "            rule_id: missing_required_fields\n",
        encoding="utf-8",
    )

    # architecture_rules requires forbidden_target_globs and scope_path_globs.
    with pytest.raises(ValueError, match="missing"):
        compile_architecture_policy(path, "shared_lib")


def test_the_compiler_names_no_concrete_family(doc: Path) -> None:
    """Extension stays a registration, so the registry decides what is valid."""
    import inspect

    from rey_lib.repository_map import architecture_policy

    source = inspect.getsource(architecture_policy)
    for family in RULE_FAMILIES:
        assert family.config_key not in source, (
            f"{family.config_key} is named in the compiler; families must be "
            "resolved through the registry."
        )


def test_a_missing_document_raises_rather_than_guessing(tmp_path: Path) -> None:
    """The path is configuration and is never substituted."""
    with pytest.raises(FileNotFoundError):
        compile_architecture_policy(tmp_path / "absent.yaml", "shared_lib")
