"""The architecture traversal projection: authored meaning joined to current code.

Two authorities, one derived artifact. ``01_core_architecture.yaml`` says what a
module or symbol *means*; the generated repository maps say what exists and
where. Every consumer used to perform that join for itself, which is how an
unreferenced class comes to look canonical merely because it is present in a
map. This does it once.

**The projection owns nothing.** Every node is an authored statement resolved
against a structural record, or a repository the authority declares a member.
Nothing here decides what architecture is, and nothing here is a place to
record a fact that has no other home.

**The hierarchy is the application architecture, not source organization.**
Concepts are authored in ``architecture_concepts`` and nest as a product tree;
each declares the code that realizes it in ``realized_by``. Repository is a
fact *on* the evidence, never a level above it -- a concept like Logging spans
rey_lib, control and every application that uses it, and putting repository in
the tree would put source organization back into the architecture.

The invariant that gives the artifact its worth:

    every concept node is authored, and every evidence node under it is one
    of that concept's realized_by references resolved exactly to current
    structural evidence

One module or symbol may realize several concepts. It appears under each, as
several *occurrences* of one identity: ``record_id`` differs per placement and
``evidence_id`` is the same everywhere, which is the estate's own rule --
*"One object reached down two branches is one identity and two occurrences."*
Nothing is deduplicated, and nothing enforces one concept per symbol; an
exclusivity rule would be architecture, and none is authored.

So resolution is exact -- zero matches or several is a failure, never a nearest
guess -- and a statement marked ``role_in_migration:
SOURCE_CAPABILITY_ARCHITECTURE`` is recognized and excluded before resolution
is attempted. Source evidence describes a system that no longer exists; asking
it to resolve against current code would enforce the opposite of why it is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.encryption import sha256_text
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.jsonl import render_jsonl_line, write_jsonl_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_METHOD,
    RECORD_TYPE_ARCHITECTURE_MAP,
    RECORD_TYPE_ARCHITECTURE_NODE,
    RECORD_TYPE_FILE,
    RECORD_TYPE_REACHABILITY,
    RECORD_TYPE_SYMBOL,
    dotted_identity,
)
from rey_lib.repository_map.writer import GENERATOR_VERSION, RepositoryMap

__all__ = [
    "ARCHITECTURE_ARTIFACT_NAME",
    "ARCHITECTURE_SOURCE_NAME",
    "ArchitectureProjection",
    "ArchitectureProjectionError",
    "CONSUMED_OWNERSHIP_SECTIONS",
    "CONCEPTS_SECTION",
    "NODE_TYPE_IMPLEMENTATION",
    "NODE_TYPE_CONCEPT",
    "NODE_TYPE_MODULE",
    "NODE_TYPE_PACKAGE",
    "NODE_TYPE_SYMBOL",
    "SOURCE_CAPABILITY_ARCHITECTURE",
    "build_architecture_projection",
    "system_membership",
    "validate_architecture_projection",
    "write_architecture_projection",
]

logger = get_logger(__name__)

ARCHITECTURE_ARTIFACT_NAME = "03_repository_map.architecture.generated.jsonl"
ARCHITECTURE_SOURCE_NAME = "01_core_architecture.yaml"

NODE_TYPE_CONCEPT = "concept"
#: The one container between a concept and the code that realizes it. Without
#: it a package sits among a concept's sub-concepts and reads as one of them,
#: which is source organization wearing an architectural name.
NODE_TYPE_IMPLEMENTATION = "implementation"
NODE_TYPE_PACKAGE = "package"
NODE_TYPE_MODULE = "module"
NODE_TYPE_SYMBOL = "symbol"

#: What a module opens into: the things it can be asked to do. "Public
#: functions" is the whole of it -- the exported flag alone admits classes,
#: constants, interfaces and aliases, which is a different question.
_CALLABLE_KINDS = frozenset({SYMBOL_KIND_FUNCTION, SYMBOL_KIND_METHOD})

#: Where the authored concept tree lives, and the key its children nest under.
CONCEPTS_SECTION = "architecture_concepts"
CONCEPTS_KEY = "concepts"

# The marker a statement carries when it describes a system that was migrated
# away from. Recognized, then excluded: it is authority about the source, and
# the projection is about the target.
SOURCE_CAPABILITY_ARCHITECTURE = "SOURCE_CAPABILITY_ARCHITECTURE"

# Where a module's or symbol's own prose is written. Not the hierarchy any
# more -- concepts are -- but still the authored meaning of one file or one
# declaration, carried onto the evidence node that resolves to it.
#
# `scripts` is deliberately not read. It lists packaging console-script names
# with no statement beside them, and the name-to-target mapping lives in the
# repository's pyproject.toml, so it would make packaging metadata a third
# authority in a two-authority join.
CONSUMED_OWNERSHIP_SECTIONS = ("canonical_modules", "canonical_symbols")


class ArchitectureProjectionError(Exception):
    """Raised when authored architecture cannot be joined to current evidence.

    Every case is a contradiction between the two authorities, and none has a
    safe default. A statement naming code that is not there is either stale
    architecture or a rename nobody recorded; either way the answer is a person
    reading it, not a projection quietly emitting less.
    """


@dataclass
class ArchitectureProjection:
    """The projection: a header and its architecture nodes.

    Attributes:
        header: The architecture_map header record.
        records: The architecture_node records, deterministically ordered.
    """

    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)


def system_membership(architecture_path: Path) -> list[str]:
    """Return the repositories the architecture declares as system members.

    The one membership authority. Every step that needs the set reads it here
    rather than carrying a list of its own, so a repository joining the system
    is one edit in one file.

    Args:
        architecture_path: Path to the architecture context.

    Returns:
        The declared repository names, in declared order.

    Raises:
        ArchitectureProjectionError: If the document declares no membership.
    """
    return _declared_membership(_parsed_architecture(architecture_path), architecture_path)


def _declared_membership(parsed: dict[str, Any], architecture_path: Path) -> list[str]:
    """Return the declared membership from an already-parsed document.

    Args:
        parsed: The parsed architecture context.
        architecture_path: Where it came from, for the message.

    Returns:
        The declared repository names, in declared order.

    Raises:
        ArchitectureProjectionError: If the document declares no membership.
    """
    members = (parsed.get("system_membership") or {}).get("repositories")
    if not members:
        raise ArchitectureProjectionError(
            f"{architecture_path} declares no system_membership.repositories."
        )
    return [str(member) for member in members]


def build_architecture_projection(
    architecture_path: Path,
    maps: dict[str, RepositoryMap],
) -> ArchitectureProjection:
    """Join the authored concept tree to current structural evidence.

    Args:
        architecture_path: Path to the architecture context.
        maps: Repository name to its parsed map, for every declared member.

    Returns:
        The projection, deterministically ordered.

    Raises:
        ArchitectureProjectionError: If the declared membership and the
            supplied maps differ, or any reference cannot be resolved exactly.
    """
    # One read. Membership and the concept tree must come from the same file
    # contents: two reads would let a document edited between them contribute a
    # member list from one version and concepts from another.
    parsed = _parsed_architecture(architecture_path)
    members = _declared_membership(parsed, architecture_path)
    _refuse_membership_drift(members, maps)

    evidence = {name: _Evidence(maps[name]) for name in members}
    statements = _authored_statements(parsed)
    concepts = (parsed.get(CONCEPTS_SECTION) or {}).get(CONCEPTS_KEY)
    if not concepts:
        raise ArchitectureProjectionError(
            f"{architecture_path} declares no {CONCEPTS_SECTION}.{CONCEPTS_KEY}."
        )

    records: list[dict[str, Any]] = []
    _walk_concepts(concepts, None, (), evidence, statements, records)

    _refuse_duplicate_identity(records)
    _refuse_orphans(records)
    _set_has_children(records)

    header = _header(architecture_path, members, maps, records)
    return ArchitectureProjection(header=header, records=records)


def _walk_concepts(
    concepts: list[Any],
    parent_id: str | None,
    path: tuple[str, ...],
    evidence: dict[str, "_Evidence"],
    statements: dict[str, str],
    records: list[dict[str, Any]],
) -> None:
    """Emit one concept, its sub-concepts, and the code that realizes it.

    Declared order is display order, so nothing is sorted here.

    Code hangs beneath one **implementation** container rather than directly
    under the concept. A package sitting among a concept's sub-concepts reads
    as one of them, and the tree stops answering how the system is built and
    starts listing where files are. The container is the whole of the
    distinction: everything beneath it is the same package, module and callable
    it always was.

    A concept that names nothing gets no container. An empty one would say
    there is an implementation and offer nothing.

    Args:
        concepts: The concept declarations at this level.
        parent_id: The concept above, or None at the top.
        path: The keys of the concepts above, for identity.
        evidence: Repository name to its structural facts.
        statements: Authored prose for a resolved path or symbol, if any.
        records: Every node so far, appended to.

    Raises:
        ArchitectureProjectionError: If a concept declares no key, or a
            realized_by reference does not resolve exactly.
    """
    for declared in concepts:
        if not isinstance(declared, dict) or not str(declared.get("key") or ""):
            raise ArchitectureProjectionError(
                f"A concept under {'.'.join(path) or CONCEPTS_SECTION} declares no key."
            )
        here = (*path, str(declared["key"]))
        scope = ".".join(here)
        record_id = _identity(scope)
        records.append(_concept_node(declared, record_id, parent_id, here))
        _walk_concepts(
            declared.get(CONCEPTS_KEY) or [],
            record_id, here, evidence, statements, records,
        )
        references = declared.get("realized_by") or []
        if not references:
            continue
        container = _implementation_node(record_id, scope)
        records.append(container)
        for reference in references:
            _emit_evidence(
                str(reference), container["record_id"], scope,
                evidence, statements, records,
            )


def _emit_evidence(
    reference: str,
    parent_id: str,
    scope: str,
    evidence: dict[str, "_Evidence"],
    statements: dict[str, str],
    records: list[dict[str, Any]],
) -> None:
    """Emit one realized_by reference, and what can be opened inside it.

    A package holds what sits directly inside it, one level, so the source's
    own hierarchy is kept rather than flattened into a list of every
    descendant. A module holds the public callables it declares. A callable is
    where looking inside ends.
    """
    repository, kind, resolved = _resolved_reference(reference, evidence)
    facts = evidence[repository]
    node = _evidence_node(
        reference, parent_id, scope, repository, kind, resolved,
        statements, facts.reachability,
    )
    records.append(node)
    if kind == NODE_TYPE_PACKAGE:
        for child in facts.directly_inside(resolved):
            _emit_evidence(child, node["record_id"], scope, evidence, statements, records)
        return
    if kind != NODE_TYPE_MODULE:
        return
    for record in facts.declared_callables(resolved):
        records.append(_symbol_node(
            record, node["record_id"], scope, repository,
            record.get("qualified_name") or record["name"], statements,
        ))


def _implementation_node(parent_id: str, scope: str) -> dict[str, Any]:
    """The container between one concept and the code that realizes it."""
    return {
        "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
        "record_id": _identity(scope, NODE_TYPE_IMPLEMENTATION),
        "parent_id": parent_id,
        "node_type": NODE_TYPE_IMPLEMENTATION,
        "label": "Implementation",
        "concept_key": scope,
        "statement": None,
        "repository": None,
        "evidence_id": None,
        "architecture_key": None,
        "source_path": None,
        "relative_path": None,
        "source_line": None,
        "symbol_kind": None,
        "owner": None,
        "qualified_name": None,
        "exported": None,
        "reachability": None,
        "has_children": False,
    }


def _concept_node(
    declared: dict[str, Any],
    record_id: str,
    parent_id: str | None,
    path: tuple[str, ...],
) -> dict[str, Any]:
    """One authored concept, as a node.

    It carries no structural field: a concept is not a file, and inventing a
    path for one would put source organization back into the hierarchy.
    """
    return {
        "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
        "record_id": record_id,
        "parent_id": parent_id,
        "node_type": NODE_TYPE_CONCEPT,
        "label": str(declared.get("label") or path[-1]),
        "concept_key": ".".join(path),
        "statement": str(declared.get("statement") or "") or None,
        "repository": None,
        "evidence_id": None,
        "architecture_key": None,
        "source_path": None,
        "relative_path": None,
        "source_line": None,
        "symbol_kind": None,
        "owner": None,
        "qualified_name": None,
        "exported": None,
        "reachability": None,
        "has_children": False,
    }


def _identity(scope: str, *parts: str) -> str:
    """One projected row's identity, which is its placement.

    ``scope`` is the concept the row sits under, because one module may realize
    several concepts and each placement is its own occurrence.

    What the thing *is* stays in evidence_id, which the scope never touches --
    that is what lets a consumer see that two rows under two concepts are one
    module.
    """
    return ":".join(
        part for part in (RECORD_TYPE_ARCHITECTURE_NODE, scope, *parts) if part
    )


def _evidence_node(
    reference: str,
    parent_id: str | None,
    scope: str,
    repository: str,
    kind: str,
    resolved: Any,
    statements: dict[str, str],
    reachability: dict[str, str],
) -> dict[str, Any]:
    """One realized_by reference, resolved and placed under its concept.

    The reference names no repository. Which one holds it is a structural
    question, so every declared member is asked and exactly one must answer --
    a name two repositories both hold is ambiguous, and guessing between them
    would attach a concept to code that does not realize it.

    ``record_id`` carries the concept, because it is an occurrence: the same
    symbol under two concepts is two placements of one thing. ``evidence_id``
    carries the identity, and is the same wherever it appears.
    """
    if kind in (NODE_TYPE_MODULE, NODE_TYPE_PACKAGE):
        evidence_id = f"{repository}:{resolved}"
        return {
            "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
            "record_id": _identity(scope, repository, resolved),
            "parent_id": parent_id,
            "node_type": kind,
            "label": resolved,
            "statement": statements.get(f"{repository}:{resolved}"),
            "repository": repository,
            "evidence_id": evidence_id,
            "architecture_key": reference,
            "source_path": resolved,
            # Where the file sits under the checkout root, so a mapping can
            # address it. Composed from facts the node already carries, and
            # never an absolute path: where the checkouts are is the
            # installation's answer, not this artifact's.
            #
            # A package carries none. It is a container, nothing opens it, and
            # a directory reaching a mapping that expects a file is a failure
            # waiting for whoever declares the next action.
            "relative_path": (
                f"{repository}/{resolved}" if kind == NODE_TYPE_MODULE else None
            ),
            "source_line": None,
            "symbol_kind": None,
            "owner": None,
            "qualified_name": None,
            "exported": None,
            # A fact about a file, so a package has none.
            "reachability": (
                reachability.get(resolved) if kind == NODE_TYPE_MODULE else None
            ),
            "has_children": False,
        }
    return _symbol_node(
        resolved, parent_id, scope, repository,
        resolved.get("qualified_name") or resolved["name"], statements,
        architecture_key=reference,
    )


def _symbol_node(
    record: dict[str, Any],
    parent_id: str | None,
    scope: str,
    repository: str,
    qualified: str,
    statements: dict[str, str],
    architecture_key: str | None = None,
) -> dict[str, Any]:
    """One declaration, as a node.

    ``label`` is the authored reference when a concept named this symbol
    directly, because a bare name under a concept says nothing about where it
    lives -- a reader meeting `composed_objects` beneath Groups cannot tell
    what it is. Under its own module the file is the parent, so the name alone
    is enough.
    """
    evidence_id = f"{repository}:{record['source_path']}:{qualified}"
    return {
        "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
        "record_id": _identity(
            scope, repository, record["source_path"], qualified,
        ),
        "parent_id": parent_id,
        "node_type": NODE_TYPE_SYMBOL,
        "label": qualified,
        "statement": statements.get(architecture_key or "")
        or statements.get(dotted_identity(record["source_path"], qualified)),
        "repository": repository,
        "evidence_id": evidence_id,
        "architecture_key": architecture_key,
        # Copied from the resolved record, so owner "" keeps meaning a
        # top-level declaration rather than a field that does not apply.
        "source_path": record["source_path"],
        "relative_path": f"{repository}/{record['source_path']}",
        "source_line": record.get("source_line"),
        "symbol_kind": record.get("symbol_kind"),
        "owner": record.get("owner"),
        "qualified_name": qualified,
        "exported": record.get("exported"),
        # Reachability is recorded against a file, never a symbol.
        "reachability": None,
        "has_children": False,
    }


def _resolved_reference(
    reference: str,
    evidence: dict[str, "_Evidence"],
) -> tuple[str, str, Any]:
    """Return which repository answers for one realized_by reference.

    A reference is a path when it names a file or a directory the maps record,
    and a dotted identity otherwise. Both are exact: zero answers or several is
    a failure, never a nearest guess.

    Returns:
        The repository, the node type, and either the resolved path or the
        resolved symbol record.

    Raises:
        ArchitectureProjectionError: If nothing answers, or more than one does.
    """
    answers: list[tuple[str, str, Any]] = []
    for repository, facts in evidence.items():
        if reference in facts.files:
            answers.append((repository, NODE_TYPE_MODULE, reference))
        elif reference.endswith("/") and reference[:-1] in facts.directories:
            answers.append((repository, NODE_TYPE_PACKAGE, reference))
        for record in facts.symbols.get(reference, []):
            answers.append((repository, NODE_TYPE_SYMBOL, record))
    if not answers:
        raise ArchitectureProjectionError(
            f"realized_by '{reference}' names no file, directory or symbol any "
            f"repository map records."
        )
    if len(answers) > 1:
        where = ", ".join(sorted(repository for repository, _, _ in answers))
        raise ArchitectureProjectionError(
            f"realized_by '{reference}' resolves in several places: {where}."
        )
    return answers[0]


def _authored_statements(parsed: dict[str, Any]) -> dict[str, str]:
    """Return the prose canonical_ownership holds, keyed by what it describes.

    canonical_ownership is no longer the hierarchy -- concepts are -- but it is
    still where a module's or a symbol's own meaning is written. An evidence
    node carries that statement when one exists, so the authored prose is not
    lost by the tree changing shape.

    Source-capability statements are skipped: they describe a system that was
    migrated away from, and nothing current realizes them.
    """
    found: dict[str, str] = {}
    for repository, block in (
        (parsed.get("canonical_ownership") or {}).get("repositories") or {}
    ).items():
        for key, value in (block.get("canonical_modules") or {}).items():
            if not _is_source_evidence(value):
                found[f"{repository}:{key}"] = _statement_of(value)
        for entry in block.get("canonical_symbols") or []:
            if _is_source_evidence(entry):
                continue
            for dotted in entry.get("symbols") or []:
                found[str(dotted)] = _statement_of(entry)
    return found


def validate_architecture_projection(
    projection: ArchitectureProjection,
    maps: dict[str, RepositoryMap],
    architecture_path: Path,
) -> list[str]:
    """Return why a projection no longer describes the authorities it joined.

    A projection is a join of two authorities and is only as current as the
    staler of them. Checking the maps alone would let edited *meaning* pass:
    a statement rewritten, retired or newly authored since the projection was
    built changes what the artifact should say while every map hash still
    matches.

    Args:
        projection: The projection to check.
        maps: Repository name to its currently parsed map.
        architecture_path: The architecture context as it stands now.

    Returns:
        Human-readable reasons, empty when the projection is current.
    """
    reasons: list[str] = []
    recorded_source = projection.header.get("architecture_source") or {}
    if recorded_source.get("content_hash") != sha256_text(read_text_file(architecture_path)):
        reasons.append(
            f"{recorded_source.get('artifact_path', ARCHITECTURE_SOURCE_NAME)}: "
            "the architecture context has been edited since the projection"
        )
    recorded = {
        entry["repository"]: entry for entry in projection.header.get("source_maps", [])
    }
    for repository in sorted(set(recorded) | set(maps)):
        if repository not in maps:
            reasons.append(f"{repository}: projection references a map that was not supplied")
            continue
        if repository not in recorded:
            reasons.append(f"{repository}: map supplied but not referenced by the projection")
            continue
        current = maps[repository].header.get("content_hash", "")
        if recorded[repository].get("content_hash") != current:
            reasons.append(f"{repository}: map has been regenerated since the projection")
    return reasons


def write_architecture_projection(
    projection: ArchitectureProjection,
    output_path: Path,
) -> None:
    """Write the projection as deterministic JSONL.

    Args:
        projection: The projection to write.
        output_path: Destination file.
    """
    write_jsonl_file(output_path, [projection.header, *projection.records])
    logger.info(
        "Wrote architecture projection with %d nodes to %s",
        len(projection.records),
        output_path,
    )


# ---------------------------------------------------------------------------
# Structural evidence
# ---------------------------------------------------------------------------


class _Evidence:
    """One repository's structural facts, indexed for exact lookup."""

    def __init__(self, report: RepositoryMap) -> None:
        """Index a parsed map.

        Args:
            report: The repository's parsed map.
        """
        self.files: set[str] = set()
        # Every record a dotted identity names, not the first one seen. An
        # index that kept one could not tell "resolves once" from "resolved
        # several ways and the rest were dropped", which is precisely the
        # ambiguity this projection exists to refuse.
        self.symbols: dict[str, list[dict[str, Any]]] = {}
        #: What each file declares, so a module node can be opened up rather
        #: than being a name a reader cannot look inside.
        self.by_file: dict[str, list[dict[str, Any]]] = {}
        self.reachability: dict[str, str] = {}
        for record in report.records:
            kind = record.get("record_type")
            if kind == RECORD_TYPE_FILE:
                self.files.add(record["path"])
            elif kind == RECORD_TYPE_SYMBOL:
                identity = dotted_identity(
                    record["source_path"],
                    record.get("qualified_name") or record["name"],
                )
                self.symbols.setdefault(identity, []).append(record)
                self.by_file.setdefault(record["source_path"], []).append(record)
            elif kind == RECORD_TYPE_REACHABILITY:
                target = str(record.get("target", ""))
                if target.startswith("file:"):
                    self.reachability[target[len("file:"):]] = str(record.get("status", ""))
        self.directories = {
            "/".join(parts[:index + 1])
            for path in self.files
            for parts in [path.split("/")]
            for index in range(len(parts) - 1)
        }

    def directly_inside(self, directory: str) -> list[str]:
        """What sits immediately inside one package, in path order.

        One level. A package's sub-packages come back with their trailing
        slash, so they resolve as packages in turn and the source's own
        hierarchy is kept -- listing every descendant instead would make a
        directory into a file browser.
        """
        depth = directory.count("/")
        found: set[str] = set()
        for path in self.files:
            if not path.startswith(directory):
                continue
            if path.count("/") == depth:
                found.add(path)
            else:
                found.add("/".join(path.split("/")[:depth + 1]) + "/")
        return sorted(found)

    def declared_callables(self, path: str) -> list[dict[str, Any]]:
        """The public callables one file declares, in source order.

        Callables, because that is what a reader opening a module is looking
        for: what it can be asked to do. A class, a constant, an interface and
        a type alias are all published too, and none of them answers that.

        Public, as the map already records it. Nothing else is filtered here --
        which files the map holds, and what each declares, are its answers and
        not this projection's to narrow.
        """
        return sorted(
            (
                record for record in self.by_file.get(path, [])
                if record.get("exported")
                and record.get("symbol_kind") in _CALLABLE_KINDS
            ),
            key=lambda record: (record.get("source_line") or 0, record["record_id"]),
        )


# dotted_identity now lives beside the record it identifies, in records.py: the
# code index stores it on every symbol, so the projection and the database
# resolve an authored reference by the same arithmetic rather than two.
# Imported above, and re-exported by that import for existing callers.


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _set_has_children(records: list[dict[str, Any]]) -> None:
    """Mark every node that something else names as its parent.

    Computed from the assembled set rather than from node type, so a repository
    owning symbols directly and a module owning none are both answered by the
    same rule.

    Args:
        records: Every node record, mutated in place.
    """
    parents = {record["parent_id"] for record in records if record["parent_id"]}
    for record in records:
        record["has_children"] = record["record_id"] in parents


def _refuse_membership_drift(members: list[str], maps: dict[str, RepositoryMap]) -> None:
    """Refuse a map set that is not exactly the declared membership.

    Args:
        members: The declared system membership.
        maps: The supplied maps.

    Raises:
        ArchitectureProjectionError: If either side holds a repository the
            other does not. A second membership list is how a declared member
            quietly stops being projected.
    """
    declared, supplied = set(members), set(maps)
    missing = sorted(declared - supplied)
    unexpected = sorted(supplied - declared)
    if missing or unexpected:
        raise ArchitectureProjectionError(
            "Supplied repository maps are not the declared system membership. "
            f"Missing: {missing or 'none'}. Unexpected: {unexpected or 'none'}."
        )


def _refuse_duplicate_identity(records: list[dict[str, Any]]) -> None:
    """Refuse two nodes claiming one identity.

    Args:
        records: Every node record.

    Raises:
        ArchitectureProjectionError: If a record_id repeats. Traversal is by
            parent_id, so a repeated identity is an ambiguous branch.
    """
    seen: set[str] = set()
    duplicates = sorted({
        record["record_id"] for record in records
        if record["record_id"] in seen or seen.add(record["record_id"])  # type: ignore[func-returns-value]
    })
    if duplicates:
        raise ArchitectureProjectionError(
            f"Duplicate architecture node identity: {', '.join(duplicates)}."
        )


def _refuse_orphans(records: list[dict[str, Any]]) -> None:
    """Refuse a node whose parent is not in the same generation.

    Args:
        records: Every node record.

    Raises:
        ArchitectureProjectionError: If a parent_id names no node.
    """
    identities = {record["record_id"] for record in records}
    orphans = sorted({
        str(record["parent_id"]) for record in records
        if record["parent_id"] and record["parent_id"] not in identities
    })
    if orphans:
        raise ArchitectureProjectionError(
            f"Architecture node parents that no node answers for: {', '.join(orphans)}."
        )


def _header(
    architecture_path: Path,
    members: list[str],
    maps: dict[str, RepositoryMap],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the architecture_map header.

    Args:
        architecture_path: The authority this was joined from.
        members: The declared system membership.
        maps: The structural baselines it was joined against.
        records: Every node, for the content hash.

    Returns:
        The header. Both authorities are named with their hashes, so a reader
        can prove which meaning and which structure produced this artifact.
        ``generated_at`` is deliberately outside the content hash: the same
        inputs must hash the same however often they are regenerated.
    """
    return {
        "record_type": RECORD_TYPE_ARCHITECTURE_MAP,
        "record_id": RECORD_TYPE_ARCHITECTURE_MAP,
        "schema_version": 1,
        "node_count": len(records),
        "member_count": len(members),
        "generator_version": GENERATOR_VERSION,
        "architecture_source": {
            "artifact_path": ARCHITECTURE_SOURCE_NAME,
            "content_hash": sha256_text(read_text_file(architecture_path)),
        },
        "source_maps": [
            {
                "repository": repository,
                "artifact_path": f"03_repository_map.{repository}.generated.jsonl",
                "head_commit": maps[repository].header.get("head_commit", ""),
                "content_hash": maps[repository].header.get("content_hash", ""),
            }
            for repository in members
        ],
        "content_hash": sha256_text(
            "\n".join(render_jsonl_line(record) for record in records)
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _parsed_architecture(architecture_path: Path) -> dict[str, Any]:
    """Read the architecture context as plain data.

    Read literally: no merging, no environment resolution, no installation
    semantics. This is a contract document, not a configuration tree.

    Args:
        architecture_path: Path to the architecture context.

    Returns:
        The parsed document.

    Raises:
        ArchitectureProjectionError: If the path is absent.
    """
    if not architecture_path.is_file():
        raise ArchitectureProjectionError(
            f"Architecture context not found: {architecture_path}"
        )
    return parse_yaml(read_text_file(architecture_path)) or {}


def _is_source_evidence(value: Any) -> bool:
    """Return whether a statement describes a system that was migrated away from.

    Args:
        value: A canonical_modules value or canonical_symbols entry.

    Returns:
        True when it carries the source-capability role.
    """
    return (
        isinstance(value, dict)
        and value.get("role_in_migration") == SOURCE_CAPABILITY_ARCHITECTURE
    )


def _statement_of(value: Any) -> str:
    """Return the authored prose of one statement.

    Args:
        value: A canonical_modules value or canonical_symbols entry.

    Returns:
        The statement, verbatim.
    """
    if isinstance(value, dict):
        return str(value.get("statement") or value.get("summary") or "")
    return str(value)
