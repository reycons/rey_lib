"""Focused tests for registration, entry-point and globals discovery.

Contract: rey_repository_map_generator.sgc.yaml (INC-003).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.repository_map import inventory_files, load_scan_rules
from rey_lib.repository_map.entry_points import extract_runtime_entry_points
from rey_lib.repository_map.globals_scan import extract_global_publications_and_consumers
from rey_lib.repository_map.registrations import extract_registrations

RULES = """
language_by_extension:
  ".js": JavaScript
  ".ts": TypeScript
  ".py": Python
  ".html": HTML

registrations:
  - registry: ReyEmbeddedHost
    registration_kind: embedded_object
    method: register
    receiver_globs: ["host", "*ReyEmbeddedHost"]
    id_argument: 0
  - registry: ReyViewers
    registration_kind: viewer
    method: register
    receiver_globs: ["*ReyViewers"]
    id_argument: 0
    id_property: id

declared_registrations:
  - registry: ActionRegistry
    registration_kind: action
    id_property: id
    path_globs: ["actions.ts"]

backend_registrations:
  - registry: OBJECT_REGISTRY
    registration_kind: provider
    symbol: OBJECT_REGISTRY
    id_key: name
    implementation_keys: [server_object, client_object]
    path_globs: ["registry.py"]

template_globs: ["templates/*.html"]
primary_template: templates/index.html
bundle_globs: ["/static/js/react/*"]
"""

HOST_JS = """\
window.ReyEmbeddedHost.register("pipeline_builder", { create() {} });
const host = globals().ReyEmbeddedHost;
host.register(objectId, descriptor);
window.ReyViewers.register({ id: "json_viewer", create() {} });
"""

ACTIONS_TS = """\
export const RUN_ACTIONS = [
  { id: "run_workflow", label: "Run" },
  { id: "close_runner", label: "Close" },
];
"""

REGISTRY_PY = '''\
OBJECT_REGISTRY = [
    {
        "name": "tree",
        "server_object": "rey_console.objects.tree.TreeServerObject",
        "client_object": "static/js/react/rey_console.js",
    },
]
'''

INDEX_HTML = """\
<html><head>
  <script>window.__REY_ASSET_V__ = "1";</script>
  <script id="bootstrapData" type="application/json">{"a":1}</script>
  <script src="/static/js/common/util.js?v=1"></script>
  <script src="/static/js/react/rey_console.js?v=1"></script>
  <script type="module" src="/static/src/main.js"></script>
  <!-- <script src="/static/js/commented_out.js"></script> -->
</head></html>
"""

OBJECT_WINDOW_HTML = """\
<html><head>
  <script src="/static/js/objects/action_menu.js?v=1"></script>
</head></html>
"""

GLOBALS_JS = """\
window.ReyTree = {};
window.ReyConsole.refresh();
const value = window.ReyTree.nodes;
if (typeof window.ReyMissing !== "undefined") { }
window.ReyTable["key"];
window.ReyMaybe?.load();
"""


@pytest.fixture()
def repo(tmp_path: Path):
    """Build a small repository exercising every detector, and its rules."""
    root = tmp_path / "repo"
    files = {
        "host.js": HOST_JS,
        "actions.ts": ACTIONS_TS,
        "registry.py": REGISTRY_PY,
        "globals.js": GLOBALS_JS,
        "templates/index.html": INDEX_HTML,
        "templates/object_window.html": OBJECT_WINDOW_HTML,
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text(RULES, encoding="utf-8")
    rules = load_scan_rules(rules_path)
    return root, rules, inventory_files(root, rules)


def test_call_site_registrations_are_detected(repo) -> None:
    """A registration call is found through any configured receiver spelling."""
    root, rules, files = repo

    records = extract_registrations(root, files, rules)
    embedded = {r.registered_id for r in records if r.registration_kind == "embedded_object"}

    assert "pipeline_builder" in embedded


def test_a_variable_id_is_recorded_as_unresolved(repo) -> None:
    """A non-literal id keeps the fact and admits the id is unknown."""
    root, rules, files = repo

    records = extract_registrations(root, files, rules)
    unresolved = [r for r in records if not r.registered_id_resolved]

    assert [r.registered_id for r in unresolved] == ["objectId"]
    assert unresolved[0].registry == "ReyEmbeddedHost"
    assert unresolved[0].source_line == 3


def test_a_viewer_id_is_read_from_the_configured_property(repo) -> None:
    """An id carried inside the argument object is resolved via id_property."""
    root, rules, files = repo

    records = extract_registrations(root, files, rules)
    viewers = [r for r in records if r.registration_kind == "viewer"]

    assert [r.registered_id for r in viewers] == ["json_viewer"]
    assert viewers[0].registered_id_resolved is True


def test_declared_literal_registrations_are_detected(repo) -> None:
    """Ids declared as object literals are registrations too."""
    root, rules, files = repo

    records = extract_registrations(root, files, rules)
    actions = sorted(r.registered_id for r in records if r.registration_kind == "action")

    assert actions == ["close_runner", "run_workflow"]


def test_backend_string_references_are_registrations(repo) -> None:
    """A backend entry naming a frontend file is a reachability fact."""
    root, rules, files = repo

    records = extract_registrations(root, files, rules)
    backend = {r.registry: r.implementation for r in records if r.registration_kind == "provider"}

    assert backend["OBJECT_REGISTRY.client_object"] == "static/js/react/rey_console.js"
    assert backend["OBJECT_REGISTRY.server_object"].endswith("TreeServerObject")


def test_registration_serializes_to_the_jsonl_shape(repo) -> None:
    """A registration is one complete JSON record."""
    root, rules, files = repo

    record = next(
        r for r in extract_registrations(root, files, rules) if r.registered_id == "json_viewer"
    ).to_dict()

    assert record["record_type"] == "registration"
    assert record["record_id"].startswith("registration:ReyViewers:json_viewer:")
    assert record["registration_kind"] == "viewer"
    assert record["registered_id_resolved"] is True
    assert "\n" not in json.dumps(record, separators=(",", ":"))


def test_templates_and_alternate_windows_are_entry_points(repo) -> None:
    """The primary template is a template; the others are alternate windows."""
    root, rules, files = repo

    records = extract_runtime_entry_points(root, files, rules)
    kinds = {(r.window_or_host, r.entry_point_kind) for r in records if r.target == r.source_path}

    assert ("templates/index.html", "template") in kinds
    assert ("templates/object_window.html", "alternate_window") in kinds


def test_classic_scripts_and_bundles_are_separate_kinds(repo) -> None:
    """A bundled entry point is not recorded as a classic script."""
    root, rules, files = repo

    records = extract_runtime_entry_points(root, files, rules)
    by_target = {r.target: r.entry_point_kind for r in records}

    assert by_target["/static/js/common/util.js"] == "classic_script"
    assert by_target["/static/js/react/rey_console.js"] == "bundle"
    assert by_target["/static/src/main.js"] == "module"


def test_cache_busting_queries_do_not_split_a_target(repo) -> None:
    """One asset is one target regardless of its version query."""
    root, rules, files = repo

    targets = {r.target for r in extract_runtime_entry_points(root, files, rules)}

    assert not any("?" in target for target in targets)


def test_a_commented_script_tag_is_not_an_entry_point(repo) -> None:
    """Templates are parsed, so a commented-out script is not a root."""
    root, rules, files = repo

    targets = {r.target for r in extract_runtime_entry_points(root, files, rules)}

    assert "/static/js/commented_out.js" not in targets


def test_inline_executable_scripts_are_entry_points_but_data_is_not(repo) -> None:
    """An inline script runs; an inline JSON payload does not."""
    root, rules, files = repo

    records = extract_runtime_entry_points(root, files, rules)
    inline = [r for r in records if r.entry_point_kind == "inline_call"]

    assert len(inline) == 1
    assert inline[0].window_or_host == "templates/index.html"


def test_global_publications_and_consumers_are_separated(repo) -> None:
    """Publication is a declaration; every other window reference is a use."""
    root, rules, files = repo

    report = extract_global_publications_and_consumers(root, files, rules)

    assert [p.global_name for p in report.publications] == ["window.ReyTree"]
    assert "window.ReyTree" not in {
        c.global_name for c in report.consumers if c.source_line == 1
    }


def test_consumer_access_kinds_are_distinguished(repo) -> None:
    """Call, property access, typeof and bracket access are separate kinds."""
    root, rules, files = repo

    report = extract_global_publications_and_consumers(root, files, rules)
    kinds = {(c.global_name, c.access_kind) for c in report.consumers}

    assert ("window.ReyConsole.refresh", "call") in kinds
    assert ("window.ReyTree.nodes", "property_access") in kinds
    assert ("window.ReyMissing", "typeof") in kinds
    assert ("window.ReyTable", "bracket_access") in kinds


def test_unmatched_globals_are_reported_on_both_sides(repo) -> None:
    """Publications with no consumer and consumers with no publication."""
    root, rules, files = repo

    report = extract_global_publications_and_consumers(root, files, rules)

    assert "window.ReyMissing" in report.consumed_without_publication
    assert "window.ReyTree" not in report.published_without_consumer


def test_root_records_are_deterministic(repo) -> None:
    """Two runs produce identical records with unique identities."""
    root, rules, files = repo

    first = extract_registrations(root, files, rules)
    second = extract_registrations(root, files, rules)
    entry_points = extract_runtime_entry_points(root, files, rules)

    assert first == second
    ids = [r.record_id for r in first] + [e.record_id for e in entry_points]
    assert len(ids) == len(set(ids))
