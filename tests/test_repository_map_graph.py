"""Focused tests for the dependency graph and conservative reachability.

Contract: rey_repository_map_generator.sgc.yaml (INC-004).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.graph import build_dependency_graph, compute_reachability
from rey_lib.repository_map.records import (
    EDGE_KIND_IMPORT,
    ENTRY_POINT_KIND_CLASSIC_SCRIPT,
    ENTRY_POINT_KIND_TEMPLATE,
    REACHABILITY_DEFINITELY,
    REACHABILITY_POTENTIALLY,
    REACHABILITY_UNREFERENCED,
    EntryPointRecord,
    FileRecord,
    GlobalConsumerRecord,
    GlobalPublicationRecord,
    ReferenceEdge,
    RegistrationRecord,
    ScanRules,
)


def _file(path: str, language: str = "JavaScript") -> FileRecord:
    """Return a minimal inventoried file record."""
    return FileRecord(
        path=path,
        language=language,
        size_bytes=1,
        is_generated=False,
        is_vendor=False,
        is_test=False,
    )


def _rules(**overrides) -> ScanRules:
    """Return scan rules with graph resolution configured."""
    defaults = dict(
        ignored_directory_names=frozenset(),
        ignored_path_globs=(),
        language_by_extension={},
        generated_path_globs=(),
        vendor_path_globs=(),
        test_path_globs=(),
        module_extensions=(".ts", ".js"),
        module_index_files=("index.ts",),
        url_path_prefixes={"/static/": "app/static/"},
        backend_path_prefixes={"static/": "app/static/"},
    )
    defaults.update(overrides)
    return ScanRules(**defaults)


def _import(source: str, target: str) -> ReferenceEdge:
    """Return an import reference edge."""
    return ReferenceEdge(
        source_path=source,
        source_line=1,
        source_column=0,
        from_id=f"file:{source}",
        to=target,
        edge_kind=EDGE_KIND_IMPORT,
        evidence="import_statement",
    )


def _build(files, references=(), registrations=(), entry_points=(), pubs=(), cons=(), rules=None):
    """Build a graph from the supplied facts."""
    return build_dependency_graph(
        list(files),
        list(references),
        list(registrations),
        list(entry_points),
        list(pubs),
        list(cons),
        rules or _rules(),
    )


def test_relative_imports_resolve_to_inventoried_files() -> None:
    """A specifier resolves only to a file the inventory already contains."""
    files = [_file("src/a.ts", "TypeScript"), _file("src/b.ts", "TypeScript")]

    graph = _build(files, references=[_import("src/a.ts", "./b.helper")])

    assert any(edge.target == "file:src/b.ts" for edge in graph.edges)
    assert graph.unresolved_targets == []


def test_a_package_specifier_is_external_not_unresolved() -> None:
    """Failing to resolve a package is correct, not a finding."""
    graph = _build([_file("src/a.ts", "TypeScript")], references=[_import("src/a.ts", "react")])

    assert graph.unresolved_targets == []
    assert graph.external_targets == [("src/a.ts", "react")]


def test_a_broken_relative_import_is_unresolved() -> None:
    """A relative specifier that matches nothing is a repository finding."""
    graph = _build([_file("src/a.ts", "TypeScript")], references=[_import("src/a.ts", "./gone")])

    assert graph.unresolved_targets == [("src/a.ts", "./gone")]
    assert graph.external_targets == []


def test_a_dotted_python_module_is_not_collapsed_to_its_package() -> None:
    """rey_console.cli resolves to the module, never to the package init."""
    files = [
        _file("pkg/__init__.py", "Python"),
        _file("pkg/cli.py", "Python"),
        _file("main.py", "Python"),
    ]

    graph = _build(files, references=[_import("main.py", "pkg.cli")])

    targets = {edge.target for edge in graph.edges}
    assert "file:pkg/cli.py" in targets
    assert "file:pkg/__init__.py" not in targets


def test_template_loads_make_files_definitely_reachable() -> None:
    """A script a template loads is a direct dependency of that window."""
    files = [_file("app/templates/index.html", "HTML"), _file("app/static/util.js")]
    entry_points = [
        EntryPointRecord(
            source_path="app/templates/index.html",
            source_line=1,
            source_column=0,
            entry_point_kind=ENTRY_POINT_KIND_TEMPLATE,
            target="app/templates/index.html",
            window_or_host="app/templates/index.html",
        ),
        EntryPointRecord(
            source_path="app/templates/index.html",
            source_line=3,
            source_column=2,
            entry_point_kind=ENTRY_POINT_KIND_CLASSIC_SCRIPT,
            target="/static/util.js",
            window_or_host="app/templates/index.html",
        ),
    ]

    graph = _build(files, entry_points=entry_points)
    verdicts = {r.target: r for r in compute_reachability(graph, files)}

    assert verdicts["file:app/static/util.js"].status == REACHABILITY_DEFINITELY
    assert verdicts["file:app/static/util.js"].evidence_record_ids


def test_backend_string_reachability_is_only_potential() -> None:
    """A file reached solely by a backend string is not a direct dependency."""
    files = [_file("registry.py", "Python"), _file("app/static/menu.js")]
    registration = RegistrationRecord(
        source_path="registry.py",
        source_line=3,
        source_column=4,
        registry="OBJECT_REGISTRY.client_object",
        registered_id="action_menu",
        implementation="static/menu.js",
        registration_kind="provider",
    )

    graph = _build(files, registrations=[registration])
    verdicts = {r.target: r for r in compute_reachability(graph, files)}

    assert verdicts["file:app/static/menu.js"].status == REACHABILITY_POTENTIALLY
    assert verdicts["file:app/static/menu.js"].root == "registry:OBJECT_REGISTRY.client_object"


def test_direct_and_registry_reachability_stay_distinguishable() -> None:
    """The two reachability mechanisms produce different verdicts (REQ-073)."""
    files = [
        _file("app/templates/index.html", "HTML"),
        _file("app/static/loaded.js"),
        _file("registry.py", "Python"),
        _file("app/static/named.js"),
    ]
    entry_points = [
        EntryPointRecord(
            source_path="app/templates/index.html",
            source_line=1,
            source_column=0,
            entry_point_kind=ENTRY_POINT_KIND_CLASSIC_SCRIPT,
            target="/static/loaded.js",
            window_or_host="app/templates/index.html",
        )
    ]
    registration = RegistrationRecord(
        source_path="registry.py",
        source_line=1,
        source_column=0,
        registry="OBJECT_REGISTRY.client_object",
        registered_id="named",
        implementation="static/named.js",
        registration_kind="provider",
    )

    graph = _build(files, registrations=[registration], entry_points=entry_points)
    verdicts = {r.target: r.status for r in compute_reachability(graph, files)}

    assert verdicts["file:app/static/loaded.js"] == REACHABILITY_DEFINITELY
    assert verdicts["file:app/static/named.js"] == REACHABILITY_POTENTIALLY


def test_a_global_consumer_depends_on_its_publisher() -> None:
    """Publisher and consumer have no import, so this is the only evidence."""
    files = [_file("publisher.js"), _file("consumer.js")]
    publication = GlobalPublicationRecord(
        source_path="publisher.js",
        source_line=1,
        source_column=0,
        global_name="window.ReyTree",
        implementation="{}",
    )
    consumer = GlobalConsumerRecord(
        source_path="consumer.js",
        source_line=5,
        source_column=2,
        global_name="window.ReyTree.nodes",
        access_kind="property_access",
    )

    graph = _build(files, pubs=[publication], cons=[consumer])

    assert any(
        edge.source == "file:consumer.js" and edge.target == "file:publisher.js"
        for edge in graph.edges
    )


def test_nothing_is_ever_labelled_dead() -> None:
    """An unreached file is a candidate, never asserted dead (REQ-083)."""
    files = [_file("orphan.js")]

    verdicts = compute_reachability(_build(files), files)

    assert [r.status for r in verdicts] == [REACHABILITY_UNREFERENCED]
    assert all(r.status != "dead" for r in verdicts)


def test_a_declared_runtime_entry_path_is_a_root() -> None:
    """A process entry point no template declares is still a root."""
    files = [_file("main.py", "Python"), _file("pkg/service.py", "Python")]
    rules = _rules(runtime_entry_paths=("main.py",))

    graph = _build(files, references=[_import("main.py", "pkg.service")], rules=rules)
    verdicts = {r.target: r.status for r in compute_reachability(graph, files)}

    assert verdicts["file:main.py"] == REACHABILITY_DEFINITELY
    assert verdicts["file:pkg/service.py"] == REACHABILITY_DEFINITELY


def test_reachability_explains_itself(tmp_path: Path) -> None:
    """Every reachable verdict names a root and the facts proving the path."""
    files = [_file("a.js"), _file("b.js"), _file("c.js")]
    rules = _rules(runtime_entry_paths=("a.js",))
    references = [_import("a.js", "./b"), _import("b.js", "./c")]

    graph = _build(files, references=references, rules=rules)
    verdicts = {r.target: r for r in compute_reachability(graph, files)}

    chain = verdicts["file:c.js"]
    assert chain.root == "file:a.js"
    assert len(chain.evidence_record_ids) == 2
    assert chain.to_dict()["record_type"] == "reachability"


def test_graph_and_verdicts_are_deterministic() -> None:
    """Two builds agree, and edges are sorted."""
    files = [_file("a.js"), _file("b.js")]
    rules = _rules(runtime_entry_paths=("a.js",))
    references = [_import("a.js", "./b")]

    first = _build(files, references=references, rules=rules)
    second = _build(files, references=references, rules=rules)

    assert first.edges == second.edges
    assert compute_reachability(first, files) == compute_reachability(second, files)


def test_a_declared_entry_path_that_does_not_exist_is_not_a_root() -> None:
    """A stale rules entry cannot invent a root for a missing file."""
    files = [_file("a.js")]
    rules = _rules(runtime_entry_paths=("missing.js",))

    graph = _build(files, rules=rules)

    assert graph.roots == set()
