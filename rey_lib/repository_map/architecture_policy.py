"""Compile architecture-context annotations into rule-family payloads.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-002).

A repository declares how it is scanned in its own repository_map.rules.yaml.
The system declares what its architecture *means* in 01_core_architecture.yaml,
and a statement there that is statically checkable carries an ``enforcement``
block beside the prose that states it. This module turns those blocks into the
same typed rules a rules file produces.

Two inputs, one authored truth. The architecture document answers "who owns
what"; the rules file answers "how is this repository scanned". They are merged
into one effective policy in INC-003 and evaluated by the one existing
authority. Nothing is written to disk: a generated policy file would be a
second place to read ownership from.

This module names no concrete rule family. It looks a family up in the registry
by the config key an annotation declares, and hands the entry to that family's
own build function, so a fifth family becomes annotatable without changing
anything here. An annotation naming an unknown family raises rather than being
skipped, because a silently dropped rule is indistinguishable from an
architecture that is genuinely unenforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import read_text_file
from rey_lib.repository_map.rule_families import RULE_FAMILIES

__all__ = [
    "ArchitectureRuleSource",
    "CompiledArchitecturePolicy",
    "compile_architecture_policy",
]

ANNOTATION_KEY = "enforcement"
FAMILY_KEY = "family"

_OWNERSHIP_SECTION = "canonical_ownership"
_STATEMENT_SECTIONS = ("canonical_modules", "canonical_symbols")


@dataclass(frozen=True)
class ArchitectureRuleSource:
    """Where an annotated rule was declared.

    A violation names a rule_id. With two inputs to one evaluator, a reviewer
    must be able to find the declaration again, so every compiled rule keeps
    the coordinates of the statement it came from.

    Attributes:
        repository: The repository the statement is recorded under.
        section: canonical_modules or canonical_symbols.
        statement_key: The module path or symbol identity carrying the block.
        config_key: The rule family the annotation declared.
    """

    repository: str
    section: str
    statement_key: str
    config_key: str

    def describe(self) -> str:
        """Return a one-line locator a reviewer can search for."""
        return (
            f"01_core_architecture.yaml:{_OWNERSHIP_SECTION}."
            f"{self.repository}.{self.section}[{self.statement_key}]"
        )


@dataclass(frozen=True)
class CompiledArchitecturePolicy:
    """Rules compiled from architecture annotations, with their provenance.

    Attributes:
        rule_sets: Rules keyed by the config key of the family that built them,
            matching the shape ScanRules already uses.
        provenance: rule_id to the statement that declared it.
    """

    rule_sets: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    provenance: dict[str, ArchitectureRuleSource] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Return True when the architecture declares nothing for this repository."""
        return not any(self.rule_sets.values())


def _families_by_config_key(rule_families: "tuple[Any, ...]") -> dict[str, Any]:
    """Index the family registry by the section each family is declared under."""
    return {family.config_key: family for family in rule_families}


def _annotated_statements(block: dict[str, Any]) -> "list[tuple[str, str, dict[str, Any]]]":
    """Return every annotated statement in one repository's ownership block.

    A statement is annotated when its value is a mapping carrying an
    ``enforcement`` key. A plain string is an unannotated statement, which is
    the recorded state of a claim that is real architecture and deliberately
    not statically enforced.

    Args:
        block: One repository's canonical_ownership entry.

    Returns:
        Tuples of section, statement key and the enforcement mapping.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for section in _STATEMENT_SECTIONS:
        entries = block.get(section)
        if not entries:
            continue
        # Modules are a mapping keyed by path; symbols are a list whose identity
        # is the leading text of each entry. Both carry the same annotation.
        pairs = entries.items() if isinstance(entries, dict) else enumerate(entries)
        for key, value in pairs:
            if not isinstance(value, dict) or ANNOTATION_KEY not in value:
                continue
            statement_key = key if isinstance(key, str) else _symbol_identity(value)
            found.append((section, statement_key, value[ANNOTATION_KEY]))
    return found


def _symbol_identity(value: dict[str, Any]) -> str:
    """Return a symbol statement's identity, taken from its summary."""
    summary = str(value.get("summary", "")).strip()
    return summary.split("(")[0].strip() or summary[:60]


def compile_architecture_policy(
    architecture_path: Path,
    repository: str,
    rule_families: "tuple[Any, ...]" = RULE_FAMILIES,
) -> CompiledArchitecturePolicy:
    """Compile one repository's architecture annotations into typed rules.

    Args:
        architecture_path: Path to 01_core_architecture.yaml.
        repository: Which repository's statements to compile.
        rule_families: The family registry. Supplied so this module needs no
            knowledge of any concrete family.

    Returns:
        The compiled rules and the provenance of each.

    Raises:
        FileNotFoundError: If the architecture document is absent. The path is
            configuration and is never guessed or substituted.
        ValueError: If the document is not valid YAML, if an annotation names a
            family the registry does not hold, or if an annotation is malformed
            for the family that owns it.
    """
    if not architecture_path.is_file():
        raise FileNotFoundError(f"Architecture context not found: {architecture_path}")

    try:
        parsed = parse_yaml(read_text_file(architecture_path))
    except Exception as exc:  # Surface the offending file, not a bare parse error.
        raise ValueError(f"Architecture context is not valid YAML: {architecture_path}") from exc

    repositories = (parsed or {}).get(_OWNERSHIP_SECTION, {}).get("repositories", {})
    block = repositories.get(repository)
    if not block:
        return CompiledArchitecturePolicy()

    families = _families_by_config_key(rule_families)
    entries_by_key: dict[str, list[dict[str, Any]]] = {}
    provenance: dict[str, ArchitectureRuleSource] = {}

    for section, statement_key, annotation in _annotated_statements(block):
        if not isinstance(annotation, dict):
            raise ValueError(
                f"Architecture annotation on {repository}.{section}[{statement_key}] "
                f"must be a mapping, got {type(annotation).__name__}."
            )
        config_key = annotation.get(FAMILY_KEY)
        if config_key not in families:
            # Loud, not skipped: a mistyped family would otherwise read as a
            # statement nobody chose to enforce.
            raise ValueError(
                f"Architecture annotation on {repository}.{section}[{statement_key}] names "
                f"unknown rule family {config_key!r}. Known families: {sorted(families)}."
            )
        entry = {key: value for key, value in annotation.items() if key != FAMILY_KEY}
        entries_by_key.setdefault(config_key, []).append(entry)
        rule_id = entry.get("rule_id")
        if rule_id:
            provenance[rule_id] = ArchitectureRuleSource(
                repository=repository,
                section=section,
                statement_key=statement_key,
                config_key=config_key,
            )

    # Each family parses and validates its own fields. A malformed annotation
    # fails through the owning family's build path, so there is one schema per
    # family rather than a second one here.
    rule_sets = {
        config_key: families[config_key].build(entries)
        for config_key, entries in entries_by_key.items()
    }
    return CompiledArchitecturePolicy(rule_sets=rule_sets, provenance=provenance)
