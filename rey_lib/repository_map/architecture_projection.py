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

The invariant that gives the artifact its worth:

    every module or symbol node is an authored current-target statement
    resolved exactly to current structural evidence, and every repository node
    projects a repository system_membership.repositories declares

So resolution is exact -- zero matches or several is a failure, never a nearest
guess -- and a statement marked ``role_in_migration:
SOURCE_CAPABILITY_ARCHITECTURE`` is recognized and excluded before resolution
is attempted. Source evidence describes a system that no longer exists; asking
it to resolve against current code would enforce the opposite of why it is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.encryption import sha256_text
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.jsonl import render_jsonl_line, write_jsonl_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    RECORD_TYPE_ARCHITECTURE_MAP,
    RECORD_TYPE_ARCHITECTURE_NODE,
    RECORD_TYPE_FILE,
    RECORD_TYPE_REACHABILITY,
    RECORD_TYPE_SYMBOL,
)
from rey_lib.repository_map.writer import GENERATOR_VERSION, RepositoryMap

__all__ = [
    "ARCHITECTURE_ARTIFACT_NAME",
    "ARCHITECTURE_SOURCE_NAME",
    "ArchitectureProjection",
    "ArchitectureProjectionError",
    "CONSUMED_OWNERSHIP_SECTIONS",
    "NODE_TYPE_MODULE",
    "NODE_TYPE_REPOSITORY",
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

NODE_TYPE_REPOSITORY = "repository"
NODE_TYPE_MODULE = "module"
NODE_TYPE_SYMBOL = "symbol"

# The marker a statement carries when it describes a system that was migrated
# away from. Recognized, then excluded: it is authority about the source, and
# the projection is about the target.
SOURCE_CAPABILITY_ARCHITECTURE = "SOURCE_CAPABILITY_ARCHITECTURE"

# The ownership sections this projection consumes, named rather than discovered.
#
# `scripts` is deliberately not among them, and its absence is a decision
# rather than an oversight. It lists packaging console-script names with no
# statement beside them, and the name-to-target mapping lives in the
# repository's pyproject.toml -- so a script node would carry no architecture
# and would make packaging metadata a third authority in a two-authority join.
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
    """Join authored architecture to current structural evidence.

    Args:
        architecture_path: Path to the architecture context.
        maps: Repository name to its parsed map, for every declared member.

    Returns:
        The projection, deterministically ordered.

    Raises:
        ArchitectureProjectionError: If the declared membership and the
            supplied maps differ, or any statement cannot be resolved exactly.
    """
    # One read. Membership and ownership must come from the same file contents:
    # two reads would let a document edited between them contribute a member
    # list from one version and statements from another.
    parsed = _parsed_architecture(architecture_path)
    members = _declared_membership(parsed, architecture_path)
    _refuse_membership_drift(members, maps)

    ownership = (parsed.get("canonical_ownership") or {}).get("repositories") or {}

    records: list[dict[str, Any]] = []
    for repository in members:
        records.append(_repository_node(repository))
        evidence = _Evidence(maps[repository])
        block = ownership.get(repository) or {}
        consumed = {name: block.get(name) for name in CONSUMED_OWNERSHIP_SECTIONS}
        modules = _current_modules(repository, consumed, evidence)
        records.extend(modules.values())
        records.extend(_symbol_nodes(repository, consumed, evidence, modules))

    _refuse_duplicate_identity(records)
    _refuse_orphans(records)
    _set_has_children(records)

    header = _header(architecture_path, members, maps, records)
    return ArchitectureProjection(header=header, records=records)


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


def dotted_identity(source_path: str, qualified_name: str) -> str:
    """Return the dotted identity one structural record answers to.

    The file's path becomes its dotted module -- its extension dropped and a
    package ``__init__`` standing for the package itself -- and the symbol's
    qualified name follows it. ``Owner.member`` is already inside
    ``qualified_name``, so nothing here knows that a method has an owner.

    One rule for every language. It names no extension and tries no
    candidates, so a TypeScript identity resolves by the same arithmetic a
    Python one does and neither has a code path of its own.

    Args:
        source_path: The path the declaration is written in.
        qualified_name: The declaration's qualified name.

    Returns:
        The dotted identity, such as ``rey_lib.ai.ai.AI.execute``.
    """
    stem = PurePosixPath(source_path).with_suffix("")
    if stem.name == "__init__":
        stem = stem.parent
    return f"{str(stem).replace('/', '.')}.{qualified_name}"


def _current_modules(
    repository: str,
    consumed: dict[str, Any],
    evidence: _Evidence,
) -> dict[str, dict[str, Any]]:
    """Return one node per current canonical module, keyed by its resolved path.

    Args:
        repository: The repository the statements belong to.
        consumed: The ownership sections this projection consumes.
        evidence: Its structural facts.

    Returns:
        Resolved path to node record, in declared order.

    Raises:
        ArchitectureProjectionError: If a statement resolves to nothing.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for key, value in (consumed.get("canonical_modules") or {}).items():
        if _is_source_evidence(value):
            continue
        path = _resolved_module_path(repository, key, evidence)
        nodes[path] = {
            "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
            "record_id": f"{RECORD_TYPE_ARCHITECTURE_NODE}:{repository}:{path}",
            "parent_id": f"{RECORD_TYPE_ARCHITECTURE_NODE}:{repository}",
            "node_type": NODE_TYPE_MODULE,
            "label": path,
            "repository": repository,
            "architecture_key": key,
            "statement": _statement_of(value),
            "source_path": path,
            "source_line": None,
            "symbol_kind": None,
            "owner": None,
            "qualified_name": None,
            "exported": None,
            # Supplied for a file and not for a directory: reachability is a
            # fact about a file, and inventing one for a package would be the
            # projection deciding something no record says.
            "reachability": evidence.reachability.get(path),
            "has_children": False,
        }
    return nodes


def _resolved_module_path(repository: str, key: str, evidence: _Evidence) -> str:
    """Return the one path a canonical module key names.

    Args:
        repository: Repository the statement belongs to.
        key: The authored module key.
        evidence: The repository's structural facts.

    Returns:
        The resolved path. A directory keeps its trailing slash so a consumer
        can tell a package from a module without consulting the map.

    Raises:
        ArchitectureProjectionError: If the key names nothing the map records.
    """
    if key in evidence.files:
        return key
    if key.endswith("/") and key[:-1] in evidence.directories:
        return key
    raise ArchitectureProjectionError(
        f"{repository}: canonical module '{key}' names no file or directory the "
        f"repository map records. Either the statement is stale or the path moved."
    )


def _symbol_nodes(
    repository: str,
    consumed: dict[str, Any],
    evidence: _Evidence,
    modules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one node per symbol a current canonical statement names.

    Args:
        repository: The repository the statements belong to.
        consumed: The ownership sections this projection consumes.
        evidence: Its structural facts.
        modules: The repository's module nodes, for parent selection.

    Returns:
        The symbol nodes, in declared order.

    Raises:
        ArchitectureProjectionError: If a symbol does not resolve exactly.
    """
    nodes: list[dict[str, Any]] = []
    for entry in consumed.get("canonical_symbols") or []:
        if _is_source_evidence(entry):
            continue
        statement = _statement_of(entry)
        for dotted in entry.get("symbols") or []:
            record = _resolved_symbol(repository, str(dotted), evidence)
            path = record["source_path"]
            qualified = record.get("qualified_name", record["name"])
            parent = _containing_module(repository, path, modules)
            nodes.append({
                "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
                "record_id": f"{RECORD_TYPE_ARCHITECTURE_NODE}:{repository}:{path}:{qualified}",
                "parent_id": parent,
                "node_type": NODE_TYPE_SYMBOL,
                "label": qualified,
                "repository": repository,
                "architecture_key": str(dotted),
                "statement": statement,
                # Copied from the resolved record, so owner "" keeps meaning a
                # top-level declaration rather than a field that does not apply.
                "source_path": path,
                "source_line": record.get("source_line"),
                "symbol_kind": record.get("symbol_kind"),
                "owner": record.get("owner"),
                "qualified_name": qualified,
                "exported": record.get("exported"),
                # Reachability is recorded against a file, never a symbol, so
                # taking the file's verdict here would state something the map
                # does not.
                "reachability": None,
                "has_children": False,
            })
    return nodes


def _resolved_symbol(
    repository: str,
    dotted: str,
    evidence: _Evidence,
) -> dict[str, Any]:
    """Return the one symbol record an authored dotted reference names.

    An exact lookup against the identity each structural record answers to.
    Nothing is reconstructed from the reference and nothing is guessed at: a
    reference either names a declaration the map records or it does not.

    Args:
        repository: Repository the statement belongs to.
        dotted: The authored reference, such as ``rey_lib.ai.ai.AI.execute``.
        evidence: The repository's structural facts.

    Returns:
        The resolved symbol record.

    Raises:
        ArchitectureProjectionError: If nothing answers, or more than one does.
    """
    found = evidence.symbols.get(dotted, [])
    if not found:
        raise ArchitectureProjectionError(
            f"{repository}: canonical symbol '{dotted}' resolves to no symbol the "
            f"repository map records."
        )
    if len(found) > 1:
        where = ", ".join(sorted(record["record_id"] for record in found))
        raise ArchitectureProjectionError(
            f"{repository}: canonical symbol '{dotted}' resolves to several records: {where}."
        )
    return found[0]


def _containing_module(
    repository: str,
    path: str,
    modules: dict[str, dict[str, Any]],
) -> str:
    """Return the parent id for a symbol defined in ``path``.

    The deepest canonical module containing the defining file, and the
    repository when none does. Containment is on path boundaries: a file module
    contains only itself, and a directory module contains a descendant.

    The longest match is unambiguous rather than merely preferred. Two
    containing keys of equal length are both prefixes of one path, so they are
    the same string -- and a YAML mapping cannot hold that key twice. There is
    no tie to break.

    Args:
        repository: Repository the symbol belongs to.
        path: The defining file's path.
        modules: The repository's module nodes, keyed by resolved path.

    Returns:
        The parent record_id.
    """
    containing = [
        key for key in modules
        if key == path or (key.endswith("/") and path.startswith(key))
    ]
    if not containing:
        return f"{RECORD_TYPE_ARCHITECTURE_NODE}:{repository}"
    return modules[max(containing, key=len)]["record_id"]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _repository_node(repository: str) -> dict[str, Any]:
    """Return the node one declared system member contributes.

    The only node type that is not an authored statement. It exists to project
    the membership the authority declares, and carries no structural fields
    because a repository is not a file.

    Args:
        repository: The declared repository name.

    Returns:
        The record.
    """
    return {
        "record_type": RECORD_TYPE_ARCHITECTURE_NODE,
        "record_id": f"{RECORD_TYPE_ARCHITECTURE_NODE}:{repository}",
        "parent_id": None,
        "node_type": NODE_TYPE_REPOSITORY,
        "label": repository,
        "repository": repository,
        "architecture_key": None,
        "statement": None,
        "source_path": None,
        "source_line": None,
        "symbol_kind": None,
        "owner": None,
        "qualified_name": None,
        "exported": None,
        "reachability": None,
        "has_children": False,
    }


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
