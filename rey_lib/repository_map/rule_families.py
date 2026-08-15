"""The architecture rule-family registry.

Contract: rey_system_repository_map_correction.sgc.yaml (COR-008).

A rule family owns three things and nothing else: the configuration key it is
declared under, how its entries are read, and how they are evaluated against
generated evidence. Adding a family is that implementation plus one entry in
``RULE_FAMILIES``.

No central file names a concrete family. The loader iterates the registry and
the evaluator iterates the registry, so a fifth family changes neither.

Every evaluator receives the same ``Evidence`` and takes only the parts it
needs, which is what lets one registry hold families that answer different
questions — who may reach what, what may reach a global surface, what must not
exist, and where a decision point may live.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rey_lib.repository_map.records import (
    EDGE_KIND_GLOBAL_REFERENCE,
    BoundaryRule,
    DispatcherRecord,
    DispatcherRule,
    EntryPointRecord,
    FileRecord,
    GlobalPublicationRecord,
    OwnershipRule,
    PresenceRule,
    RegistrationRecord,
    PublicationRule,
    ReferenceEdge,
    ViolationRecord,
    matches_any_glob,
)

__all__ = ["RULE_FAMILIES", "Evidence", "RuleFamily"]


@dataclass(frozen=True)
class Evidence:
    """The generated facts an architecture rule may be evaluated against.

    Attributes:
        references: Executable reference edges.
        publications: Global publications.
        files: The inventoried files.
        entry_points: Runtime entry points.
        dispatchers: Dispatcher facts.
        registrations: Explicit id-to-object registrations.
    """

    references: Sequence[ReferenceEdge] = ()
    publications: Sequence[GlobalPublicationRecord] = ()
    files: Sequence[FileRecord] = ()
    entry_points: Sequence[EntryPointRecord] = ()
    dispatchers: Sequence[DispatcherRecord] = ()
    registrations: Sequence[RegistrationRecord] = ()


@dataclass(frozen=True)
class RuleFamily:
    """One kind of architectural boundary, and how to read and evaluate it.

    Attributes:
        config_key: The section a repository declares this family under.
        build: Turns that section's entries into typed rules.
        evaluate: Turns typed rules plus evidence into violations.
    """

    config_key: str
    build: Callable[[list[dict[str, Any]]], tuple[Any, ...]]
    evaluate: Callable[[tuple[Any, ...], Evidence], list[ViolationRecord]]


def _required(entry: dict[str, Any], key: str, section: str) -> Any:
    """Return a required rule field.

    Args:
        entry: One rule entry.
        key: Field name.
        section: Section name, for the error message.

    Returns:
        The field value.

    Raises:
        ValueError: If the field is missing.
    """
    if key not in entry:
        raise ValueError(f"Scan rules section '{section}' entry is missing '{key}'.")
    return entry[key]


def _build_reference_rules(entries: list[dict[str, Any]]) -> tuple[BoundaryRule, ...]:
    """Build rules governing who may reach what."""
    section = "architecture_rules"
    return tuple(
        BoundaryRule(
            rule_id=_required(entry, "rule_id", section),
            forbidden_target_globs=tuple(_required(entry, "forbidden_target_globs", section)),
            allowed_path_globs=tuple(entry.get("allowed_path_globs", ())),
            scope_path_globs=tuple(_required(entry, "scope_path_globs", section)),
            edge_kinds=tuple(entry.get("edge_kinds", ())),
        )
        for entry in entries
    )


def _evaluate_reference_rules(
    rules: tuple[BoundaryRule, ...],
    evidence: Evidence,
) -> list[ViolationRecord]:
    """Return violations of the rules governing who may reach what."""
    return [
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
        for rule in rules
        for reference in evidence.references
        if _reference_breaks(rule, reference)
    ]


def _reference_breaks(rule: BoundaryRule, reference: ReferenceEdge) -> bool:
    """Return True when a reference breaks a rule.

    Ownership is the exception rather than the subject: an owner may reach the
    mechanism it owns, which keeps a sanctioned API callable while the layer
    beneath it is not.

    Args:
        rule: The rule being evaluated.
        reference: The reference being tested.

    Returns:
        True when the reference is a violation.
    """
    if rule.edge_kinds and reference.edge_kind not in rule.edge_kinds:
        return False
    if not matches_any_glob(reference.source_path, rule.scope_path_globs):
        return False
    if not matches_any_glob(reference.to, rule.forbidden_target_globs):
        return False
    return not matches_any_glob(reference.source_path, rule.allowed_path_globs)


def _build_publication_rules(entries: list[dict[str, Any]]) -> tuple[PublicationRule, ...]:
    """Build rules governing what may reach a global surface."""
    section = "publication_rules"
    return tuple(
        PublicationRule(
            rule_id=_required(entry, "rule_id", section),
            forbidden_global_globs=tuple(_required(entry, "forbidden_global_globs", section)),
            allowed_path_globs=tuple(entry.get("allowed_path_globs", ())),
            scope_path_globs=tuple(_required(entry, "scope_path_globs", section)),
        )
        for entry in entries
    )


def _evaluate_publication_rules(
    rules: tuple[PublicationRule, ...],
    evidence: Evidence,
) -> list[ViolationRecord]:
    """Return violations of the rules governing global publication."""
    return [
        ViolationRecord(
            source_path=publication.source_path,
            source_line=publication.source_line,
            source_column=publication.source_column,
            rule_id=rule.rule_id,
            caller=publication.source_path,
            callee=publication.global_name,
            edge_kind=EDGE_KIND_GLOBAL_REFERENCE,
            evidence_record_ids=(publication.record_id,),
        )
        for rule in rules
        for publication in evidence.publications
        if matches_any_glob(publication.source_path, rule.scope_path_globs)
        and matches_any_glob(publication.global_name, rule.forbidden_global_globs)
        and not matches_any_glob(publication.source_path, rule.allowed_path_globs)
    ]


def _build_presence_rules(entries: list[dict[str, Any]]) -> tuple[PresenceRule, ...]:
    """Build rules governing what must not exist or be loaded."""
    section = "presence_rules"
    return tuple(
        PresenceRule(
            rule_id=_required(entry, "rule_id", section),
            forbidden_path_globs=tuple(entry.get("forbidden_path_globs", ())),
            forbidden_entry_point_globs=tuple(entry.get("forbidden_entry_point_globs", ())),
        )
        for entry in entries
    )


def _evaluate_presence_rules(
    rules: tuple[PresenceRule, ...],
    evidence: Evidence,
) -> list[ViolationRecord]:
    """Return violations of the rules governing what must not exist.

    A file-level violation records line 0: it has no meaningful source line,
    and pointing at one would prove nothing.
    """
    violations: list[ViolationRecord] = []
    for rule in rules:
        for file_record in evidence.files:
            if not matches_any_glob(file_record.path, rule.forbidden_path_globs):
                continue
            violations.append(
                ViolationRecord(
                    source_path=file_record.path,
                    source_line=0,
                    source_column=0,
                    rule_id=rule.rule_id,
                    caller=file_record.path,
                    callee=file_record.path,
                    edge_kind="file_present",
                    evidence_record_ids=(f"file:{file_record.path}",),
                )
            )
        for entry_point in evidence.entry_points:
            if not matches_any_glob(entry_point.target, rule.forbidden_entry_point_globs):
                continue
            violations.append(
                ViolationRecord(
                    source_path=entry_point.source_path,
                    source_line=entry_point.source_line,
                    source_column=entry_point.source_column,
                    rule_id=rule.rule_id,
                    caller=entry_point.window_or_host,
                    callee=entry_point.target,
                    edge_kind=entry_point.entry_point_kind,
                    evidence_record_ids=(entry_point.record_id,),
                )
            )
    return violations


def _build_dispatcher_rules(entries: list[dict[str, Any]]) -> tuple[DispatcherRule, ...]:
    """Build rules governing where a decision point may live."""
    section = "dispatcher_rules"
    return tuple(
        DispatcherRule(
            rule_id=_required(entry, "rule_id", section),
            forbidden_vocabulary_globs=tuple(
                _required(entry, "forbidden_vocabulary_globs", section)
            ),
            allowed_path_globs=tuple(entry.get("allowed_path_globs", ())),
            scope_path_globs=tuple(_required(entry, "scope_path_globs", section)),
            minimum_branch_count=entry.get("minimum_branch_count", 2),
        )
        for entry in entries
    )


def _evaluate_dispatcher_rules(
    rules: tuple[DispatcherRule, ...],
    evidence: Evidence,
) -> list[ViolationRecord]:
    """Return violations of the rules governing where a decision point may live.

    The dispatcher facts say a decision point exists and what it branches on;
    whether that is permitted is decided from policy data alone.
    """
    return [
        ViolationRecord(
            source_path=dispatcher.source_path,
            source_line=dispatcher.source_line,
            source_column=dispatcher.source_column,
            rule_id=rule.rule_id,
            caller=f"{dispatcher.source_path}:{dispatcher.symbol}",
            callee=dispatcher.vocabulary,
            edge_kind="dispatch",
            evidence_record_ids=(dispatcher.record_id,),
        )
        for rule in rules
        for dispatcher in evidence.dispatchers
        if dispatcher.branch_count >= rule.minimum_branch_count
        and matches_any_glob(dispatcher.source_path, rule.scope_path_globs)
        and matches_any_glob(dispatcher.vocabulary, rule.forbidden_vocabulary_globs)
        and not matches_any_glob(dispatcher.source_path, rule.allowed_path_globs)
    ]


def _build_ownership_rules(entries: list[dict[str, Any]]) -> tuple[OwnershipRule, ...]:
    """Build rules governing how many places may own one registered thing."""
    section = "ownership_rules"
    return tuple(
        OwnershipRule(
            rule_id=_required(entry, "rule_id", section),
            registry_globs=tuple(_required(entry, "registry_globs", section)),
            registered_id_globs=tuple(entry.get("registered_id_globs", ())),
            maximum_owners=entry.get("maximum_owners", 1),
            allowed_path_globs=tuple(entry.get("allowed_path_globs", ())),
            scope_path_globs=tuple(entry.get("scope_path_globs", ())),
        )
        for entry in entries
    )


def _governed_registrations(
    rule: OwnershipRule,
    evidence: Evidence,
) -> list[RegistrationRecord]:
    """Return the registrations one ownership rule governs.

    An unresolved id is excluded rather than grouped. Two expressions that
    cannot be read might name one id or two, and a duplicate reported from that
    would be a guess rather than a fact.
    """
    return [
        registration
        for registration in evidence.registrations
        if registration.registered_id_resolved
        and matches_any_glob(registration.source_path, rule.scope_path_globs, when_unconfigured=True)
        and matches_any_glob(registration.registry, rule.registry_globs)
        and matches_any_glob(
            registration.registered_id, rule.registered_id_globs, when_unconfigured=True
        )
    ]


def _evaluate_ownership_rules(
    rules: tuple[OwnershipRule, ...],
    evidence: Evidence,
) -> list[ViolationRecord]:
    """Return violations of the rules governing ownership and cardinality.

    Two distinct failures share this family. A registration outside the
    declared owner is wrong wherever it appears, and is reported per site. Too
    many owners of one id is wrong only in aggregate, so every participating
    site is reported: naming one of them would imply the others are correct.
    """
    violations: list[ViolationRecord] = []
    for rule in rules:
        governed = _governed_registrations(rule, evidence)

        if rule.allowed_path_globs:
            for registration in governed:
                if matches_any_glob(registration.source_path, rule.allowed_path_globs):
                    continue
                violations.append(_ownership_violation(rule, registration, "registered_by_non_owner"))

        owners_by_id: dict[tuple[str, str], list[RegistrationRecord]] = {}
        for registration in governed:
            owners_by_id.setdefault(
                (registration.registry, registration.registered_id), []
            ).append(registration)
        for sites in owners_by_id.values():
            if len(sites) <= rule.maximum_owners:
                continue
            for registration in sites:
                violations.append(_ownership_violation(rule, registration, "duplicate_owner"))
    return violations


def _ownership_violation(
    rule: OwnershipRule,
    registration: RegistrationRecord,
    edge_kind: str,
) -> ViolationRecord:
    """Return one ownership violation against a registration fact."""
    return ViolationRecord(
        source_path=registration.source_path,
        source_line=registration.source_line,
        source_column=registration.source_column,
        rule_id=rule.rule_id,
        caller=registration.source_path,
        callee=f"{registration.registry}:{registration.registered_id}",
        edge_kind=edge_kind,
        evidence_record_ids=(registration.record_id,),
    )


# The registry. Adding a family is one entry here plus its implementation
# above; no central loader or evaluator changes.
RULE_FAMILIES: tuple[RuleFamily, ...] = (
    RuleFamily("architecture_rules", _build_reference_rules, _evaluate_reference_rules),
    RuleFamily("publication_rules", _build_publication_rules, _evaluate_publication_rules),
    RuleFamily("presence_rules", _build_presence_rules, _evaluate_presence_rules),
    RuleFamily("dispatcher_rules", _build_dispatcher_rules, _evaluate_dispatcher_rules),
    RuleFamily("ownership_rules", _build_ownership_rules, _evaluate_ownership_rules),
)
