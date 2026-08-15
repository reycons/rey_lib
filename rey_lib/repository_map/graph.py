"""Dependency graph and conservative reachability for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-004, REQ-070 to REQ-083).

Two layers, kept apart on purpose. The graph merges evidence and resolves only
what the inventory can confirm; reachability classifies over that graph and
resolves nothing further.

Direct reachability and registry/string reachability stay distinguishable
(REQ-073). A file reached only because a backend string names it, or because a
global it publishes is consumed somewhere, is potentially reachable — not the
same claim as a file a template loads or a module imports.

Nothing is ever labelled dead. A scanner that has not checked every runtime
mechanism cannot prove absence of use, so the weakest verdict available is
'unreferenced_candidate' (REQ-083).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    EDGE_KIND_BACKEND_STRING_REFERENCE,
    EDGE_KIND_GLOBAL_REFERENCE,
    EDGE_KIND_IMPORT,
    EDGE_KIND_REGISTRATION,
    EDGE_KIND_RE_EXPORT,
    EDGE_KIND_TEMPLATE_LOAD,
    ENTRY_POINT_KIND_INLINE_CALL,
    REACHABILITY_DEFINITELY,
    REACHABILITY_POTENTIALLY,
    REACHABILITY_UNREFERENCED,
    RECORD_TYPE_FILE,
    EntryPointRecord,
    FileRecord,
    GlobalConsumerRecord,
    GlobalPublicationRecord,
    ReachabilityRecord,
    ReferenceEdge,
    RegistrationRecord,
    ScanRules,
)

__all__ = [
    "DIRECT_EDGE_KINDS",
    "GraphEdge",
    "RepositoryGraph",
    "build_dependency_graph",
    "compute_reachability",
]

logger = get_logger(__name__)

# Edge kinds that carry a direct, in-language dependency. Everything else is
# runtime-mechanism evidence: real, but a weaker claim about reachability.
DIRECT_EDGE_KINDS = frozenset(
    {EDGE_KIND_IMPORT, EDGE_KIND_RE_EXPORT, EDGE_KIND_TEMPLATE_LOAD}
)


@dataclass(frozen=True)
class GraphEdge:
    """One edge in the repository graph, carrying the fact that proves it.

    Attributes:
        source: record_id of the depending node.
        target: record_id of the depended-upon node.
        edge_kind: The kind of dependency.
        evidence_record_id: The generated fact this edge was derived from.
    """

    source: str
    target: str
    edge_kind: str
    evidence_record_id: str

    @property
    def is_direct(self) -> bool:
        """Return True when this edge is a direct in-language dependency."""
        return self.edge_kind in DIRECT_EDGE_KINDS


@dataclass
class RepositoryGraph:
    """Files, registries, ids and entry points, and the edges between them.

    Attributes:
        nodes: Every node's record_id.
        edges: Every edge, deterministically ordered.
        roots: Runtime roots execution can begin from.
        unresolved_targets: In-repository references no inventoried file
            matched. Kept as facts rather than dropped, because an import that
            should resolve and does not is evidence about the repository.
        external_targets: References to things outside the repository, such as
            a package specifier. Not resolving these is correct, so they are
            held apart from genuine resolution failures.
    """

    nodes: set[str] = field(default_factory=set)
    edges: list[GraphEdge] = field(default_factory=list)
    roots: set[str] = field(default_factory=set)
    unresolved_targets: list[tuple[str, str]] = field(default_factory=list)
    external_targets: list[tuple[str, str]] = field(default_factory=list)

    def add_edge(self, edge: GraphEdge) -> None:
        """Add one edge and both of its nodes.

        Args:
            edge: The edge to add.
        """
        self.nodes.add(edge.source)
        self.nodes.add(edge.target)
        self.edges.append(edge)

    def outgoing(self, node: str, *, direct_only: bool) -> list[GraphEdge]:
        """Return edges leaving a node.

        Args:
            node: record_id of the node.
            direct_only: Restrict to direct in-language edges.

        Returns:
            The matching edges.
        """
        return [
            edge
            for edge in self.edges
            if edge.source == node and (edge.is_direct or not direct_only)
        ]

    def finalize(self) -> "RepositoryGraph":
        """Sort edges and unresolved targets so output is deterministic.

        Returns:
            This graph.
        """
        self.edges.sort(
            key=lambda edge: (edge.source, edge.edge_kind, edge.target, edge.evidence_record_id)
        )
        self.unresolved_targets.sort()
        self.external_targets.sort()
        return self


def build_dependency_graph(
    files: list[FileRecord],
    references: list[ReferenceEdge],
    registrations: list[RegistrationRecord],
    entry_points: list[EntryPointRecord],
    publications: list[GlobalPublicationRecord],
    consumers: list[GlobalConsumerRecord],
    rules: ScanRules,
) -> RepositoryGraph:
    """Merge direct, registry and runtime-entry evidence into one graph.

    Args:
        files: The inventoried files.
        references: Executable reference edges from extraction.
        registrations: Registrations from root discovery.
        entry_points: Runtime entry points from root discovery.
        publications: Global publications from root discovery.
        consumers: Global consumers from root discovery.
        rules: The scanned repository's own scan rules.

    Returns:
        The graph, finalized and deterministically ordered.
    """
    known_paths = {record.path for record in files}
    graph = RepositoryGraph()
    for record in files:
        graph.nodes.add(f"{RECORD_TYPE_FILE}:{record.path}")
    for entry_path in rules.runtime_entry_paths:
        if entry_path in known_paths:
            graph.roots.add(f"{RECORD_TYPE_FILE}:{entry_path}")
        else:
            logger.warning("Declared runtime entry path is not inventoried: %s", entry_path)

    _add_import_edges(graph, references, known_paths, rules)
    _add_entry_point_edges(graph, entry_points, known_paths, rules)
    _add_registration_edges(graph, registrations, known_paths, rules)
    _add_global_edges(graph, publications, consumers)
    return graph.finalize()


def _add_import_edges(
    graph: RepositoryGraph,
    references: list[ReferenceEdge],
    known_paths: set[str],
    rules: ScanRules,
) -> None:
    """Add file-to-file edges for resolvable imports and re-exports.

    Args:
        graph: The graph being built.
        references: Executable reference edges.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.
    """
    for reference in references:
        if reference.edge_kind not in {EDGE_KIND_IMPORT, EDGE_KIND_RE_EXPORT}:
            continue
        specifier, resolved = _resolve_reference(reference, known_paths, rules)
        if resolved is None:
            # A relative specifier names something inside this repository, so
            # failing to resolve it is a finding. A bare specifier names a
            # package, and not resolving it is the correct answer.
            bucket = (
                graph.unresolved_targets
                if _is_in_repository(specifier)
                else graph.external_targets
            )
            bucket.append((reference.source_path, specifier))
            continue
        graph.add_edge(
            GraphEdge(
                source=f"{RECORD_TYPE_FILE}:{reference.source_path}",
                target=f"{RECORD_TYPE_FILE}:{resolved}",
                edge_kind=reference.edge_kind,
                evidence_record_id=reference.record_id,
            )
        )


def _add_entry_point_edges(
    graph: RepositoryGraph,
    entry_points: list[EntryPointRecord],
    known_paths: set[str],
    rules: ScanRules,
) -> None:
    """Add roots for entry points and edges to the assets they load.

    Args:
        graph: The graph being built.
        entry_points: Runtime entry points.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.
    """
    for entry_point in entry_points:
        host = f"{RECORD_TYPE_FILE}:{entry_point.window_or_host}"
        graph.nodes.add(host)
        graph.roots.add(host)
        if entry_point.entry_point_kind == ENTRY_POINT_KIND_INLINE_CALL:
            continue
        if entry_point.target == entry_point.source_path:
            continue
        resolved = _resolve_url(entry_point.target, known_paths, rules)
        if resolved is None:
            graph.unresolved_targets.append((entry_point.window_or_host, entry_point.target))
            continue
        graph.add_edge(
            GraphEdge(
                source=host,
                target=f"{RECORD_TYPE_FILE}:{resolved}",
                edge_kind=EDGE_KIND_TEMPLATE_LOAD,
                evidence_record_id=entry_point.record_id,
            )
        )


def _add_registration_edges(
    graph: RepositoryGraph,
    registrations: list[RegistrationRecord],
    known_paths: set[str],
    rules: ScanRules,
) -> None:
    """Add registry, registered-id and backend-string nodes and edges.

    Args:
        graph: The graph being built.
        registrations: Registrations from root discovery.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.
    """
    for registration in registrations:
        registry_node = f"registry:{registration.registry}"
        id_node = f"registered_id:{registration.registry}:{registration.registered_id}"
        declaring = f"{RECORD_TYPE_FILE}:{registration.source_path}"
        graph.nodes.update({registry_node, id_node, declaring})
        graph.add_edge(
            GraphEdge(
                source=registry_node,
                target=id_node,
                edge_kind=EDGE_KIND_REGISTRATION,
                evidence_record_id=registration.record_id,
            )
        )
        # The declaring file implements the id it registers.
        graph.add_edge(
            GraphEdge(
                source=id_node,
                target=declaring,
                edge_kind=EDGE_KIND_REGISTRATION,
                evidence_record_id=registration.record_id,
            )
        )
        # A backend entry naming a frontend file makes that file reachable
        # with no JavaScript caller anywhere. This is the edge that keeps such
        # a file from looking unreferenced.
        resolved = _resolve_backend(registration.implementation, known_paths, rules)
        if resolved is None:
            continue
        graph.add_edge(
            GraphEdge(
                source=id_node,
                target=f"{RECORD_TYPE_FILE}:{resolved}",
                edge_kind=EDGE_KIND_BACKEND_STRING_REFERENCE,
                evidence_record_id=registration.record_id,
            )
        )
        graph.roots.add(registry_node)


def _add_global_edges(
    graph: RepositoryGraph,
    publications: list[GlobalPublicationRecord],
    consumers: list[GlobalConsumerRecord],
) -> None:
    """Add consumer-to-publisher edges for globals.

    A classic script that publishes and a module that consumes have no import
    between them, so this is the only evidence linking them.

    Args:
        graph: The graph being built.
        publications: Global publications.
        consumers: Global consumers.
    """
    by_name: dict[str, list[GlobalPublicationRecord]] = {}
    for publication in publications:
        by_name.setdefault(publication.global_name, []).append(publication)

    for consumer in consumers:
        for publication in by_name.get(_matched_name(consumer.global_name, by_name), ()):
            if publication.source_path == consumer.source_path:
                continue
            graph.add_edge(
                GraphEdge(
                    source=f"{RECORD_TYPE_FILE}:{consumer.source_path}",
                    target=f"{RECORD_TYPE_FILE}:{publication.source_path}",
                    edge_kind=EDGE_KIND_GLOBAL_REFERENCE,
                    evidence_record_id=consumer.record_id,
                )
            )


def compute_reachability(
    graph: RepositoryGraph,
    files: list[FileRecord],
) -> list[ReachabilityRecord]:
    """Classify every file's reachability from the graph's runtime roots.

    Two traversals run: one over direct edges only, and one over all edges.
    A file reached by the first is definitely reachable; a file reached only by
    the second is potentially reachable, because at least one link in its path
    is a runtime mechanism rather than an in-language dependency. Anything
    unreached is an unreferenced candidate, never dead.

    Args:
        graph: The built graph.
        files: The inventoried files to classify.

    Returns:
        One verdict per file, sorted by target.
    """
    direct = _traverse(graph, direct_only=True)
    indirect = _traverse(graph, direct_only=False)

    records = []
    for file_record in files:
        node = f"{RECORD_TYPE_FILE}:{file_record.path}"
        if node in direct:
            root, evidence = direct[node]
            status = REACHABILITY_DEFINITELY
        elif node in indirect:
            root, evidence = indirect[node]
            status = REACHABILITY_POTENTIALLY
        else:
            root, evidence, status = "", (), REACHABILITY_UNREFERENCED
        records.append(
            ReachabilityRecord(
                target=node,
                status=status,
                root=root,
                evidence_record_ids=evidence,
            )
        )

    records.sort(key=lambda record: record.target)
    return records


def _traverse(
    graph: RepositoryGraph,
    *,
    direct_only: bool,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Return every node reachable from a root, with its evidence path.

    Breadth-first, so the recorded path is a shortest one and the explanation
    stays short enough to check by hand (REQ-074).

    Args:
        graph: The built graph.
        direct_only: Restrict traversal to direct in-language edges.

    Returns:
        Node to its root and the evidence record_ids proving the path.
    """
    reached: dict[str, tuple[str, tuple[str, ...]]] = {}
    queue: deque[tuple[str, str, tuple[str, ...]]] = deque()
    for root in sorted(graph.roots):
        reached[root] = (root, ())
        queue.append((root, root, ()))

    while queue:
        node, root, evidence = queue.popleft()
        for edge in graph.outgoing(node, direct_only=direct_only):
            if edge.target in reached:
                continue
            path = evidence + (edge.evidence_record_id,)
            reached[edge.target] = (root, path)
            queue.append((edge.target, root, path))
    return reached


def _resolve_reference(
    reference: ReferenceEdge,
    known_paths: set[str],
    rules: ScanRules,
) -> tuple[str, str | None]:
    """Resolve an import edge target to a file, trying the module then its parent.

    An import of a member is recorded as ``<module>.<member>``, but a module
    name is itself dotted in Python, so the whole target is tried first and
    only then the target minus its last segment. Guessing which segment is a
    member would mis-resolve rey_console.cli to the rey_console package.

    Args:
        reference: The import or re-export edge.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.

    Returns:
        The specifier that was attempted and the resolved path, or None.
    """
    target = reference.to
    candidates = [target]
    head, separator, _ = target.rpartition(".")
    if separator and head:
        candidates.append(head)
    for candidate in candidates:
        resolved = _resolve_module(candidate, reference.source_path, known_paths, rules)
        if resolved is not None:
            return candidate, resolved
    return candidates[-1], None


def _resolve_module(
    specifier: str,
    importer_path: str,
    known_paths: set[str],
    rules: ScanRules,
) -> str | None:
    """Resolve a written module specifier to an inventoried file.

    Only a file the inventory already contains can be the answer; nothing is
    guessed into existence, and an unresolvable specifier stays unresolved.

    Args:
        specifier: The specifier as written.
        importer_path: Path of the importing file.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.

    Returns:
        The resolved path, or None.
    """
    if importer_path.endswith(".py"):
        return _resolve_python_module(specifier, importer_path, known_paths)
    if not specifier.startswith("."):
        return None
    base = "/".join(importer_path.split("/")[:-1])
    parts = [part for part in (base + "/" + specifier).split("/") if part not in ("", ".")]
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(part)
    candidate = "/".join(resolved)

    if candidate in known_paths:
        return candidate
    for extension in rules.module_extensions:
        if candidate + extension in known_paths:
            return candidate + extension
    for index_file in rules.module_index_files:
        if f"{candidate}/{index_file}" in known_paths:
            return f"{candidate}/{index_file}"
    return None


def _resolve_python_module(
    specifier: str,
    importer_path: str,
    known_paths: set[str],
) -> str | None:
    """Resolve a Python import to an inventoried module.

    Relative imports are resolved against the importing package; absolute
    dotted names are resolved against the repository root. A name that is not
    an inventoried file belongs to another distribution and stays unresolved.

    Args:
        specifier: The dotted module name, with leading dots for relative
            imports.
        importer_path: Path of the importing module.
        known_paths: Every inventoried path.

    Returns:
        The resolved path, or None.
    """
    leading = len(specifier) - len(specifier.lstrip("."))
    name = specifier[leading:]
    if leading:
        package = importer_path.split("/")[:-1]
        # One dot is the current package; each further dot climbs one level.
        climb = leading - 1
        package = package[: len(package) - climb] if climb else package
        parts = package + (name.split(".") if name else [])
    else:
        parts = name.split(".")
    if not parts:
        return None

    base = "/".join(parts)
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in known_paths:
            return candidate
    return None


def _is_in_repository(specifier: str) -> bool:
    """Return True when a specifier names something inside this repository.

    Args:
        specifier: The specifier as written.

    Returns:
        True for relative specifiers, which must resolve locally. A bare
        package name is external and is not a resolution failure.
    """
    return specifier.startswith(".")


def _resolve_url(target: str, known_paths: set[str], rules: ScanRules) -> str | None:
    """Resolve a served URL to an inventoried file.

    Args:
        target: The URL as written in the template.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.

    Returns:
        The resolved path, or None.
    """
    for prefix, replacement in rules.url_path_prefixes.items():
        if target.startswith(prefix):
            candidate = replacement + target[len(prefix) :]
            if candidate in known_paths:
                return candidate
    return None


def _resolve_backend(implementation: str, known_paths: set[str], rules: ScanRules) -> str | None:
    """Resolve a backend implementation string to an inventoried file.

    Args:
        implementation: The implementation string as written.
        known_paths: Every inventoried path.
        rules: The scanned repository's scan rules.

    Returns:
        The resolved path, or None. A dotted Python object reference is not a
        file path and resolves to None here.
    """
    if implementation in known_paths:
        return implementation
    for prefix, replacement in rules.backend_path_prefixes.items():
        if implementation.startswith(prefix):
            candidate = replacement + implementation[len(prefix) :]
            if candidate in known_paths:
                return candidate
    return None


def _matched_name(name: str, published: dict[str, list[GlobalPublicationRecord]]) -> str:
    """Return the published global a consumed name depends on.

    Args:
        name: The consumed dotted name.
        published: Publications keyed by global name.

    Returns:
        The longest published prefix, or the name itself when none matches.
    """
    parts = name.split(".")
    for end in range(len(parts), 1, -1):
        candidate = ".".join(parts[:end])
        if candidate in published:
            return candidate
    return name
