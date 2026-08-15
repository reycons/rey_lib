"""Focused tests for dispatcher and switch discovery.

Contract: rey_repository_map_generator.sgc.yaml (INC-006).

The generator measures decision points; it never classifies them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.repository_map.dispatchers import inventory_dispatchers_and_switches
from rey_lib.repository_map.records import (
    EDGE_KIND_CALL,
    FileRecord,
    ReferenceEdge,
    ScanRules,
)

JS_SWITCH = """\
export function handleAction(actionId) {
  switch (actionId) {
    case "run_workflow": return run();
    case "close_runner": return close();
    default: return null;
  }
}
"""

JS_IF_CHAIN = """\
export function route(nodeType) {
  if (nodeType === "app") { return app(); }
  else if (nodeType === "pipeline") { return pipeline(); }
  else if (nodeType === "step") { return step(); }
  return null;
}
"""

JS_NOISE = """\
// switch (actionId) { case "fake": }
const text = 'switch (actionId) { case "alsoFake": }';
export function single(kind) {
  if (kind === "only") { return 1; }
  return 0;
}
export function unrelated(a, b) {
  if (a === "x") { return 1; }
  if (b === "y") { return 2; }
  return 0;
}
export function nestedChain(flag, key) {
  if (flag === "on") { return 0; }
  else if (key === "Up") { return 1; }
  else if (key === "Down") { return 2; }
  return 3;
}
"""

PY_MATCH = '''\
def resolve(node_type):
    match node_type:
        case "app":
            return 1
        case "pipeline":
            return 2
        case _:
            return 0
'''

PY_IF_CHAIN = '''\
def dispatch(action_id):
    """Docstring mentioning if action_id == "ghost"."""
    if action_id == "run":
        return 1
    elif action_id == "stop":
        return 2
    elif action_id == "reset":
        return 3
    return 0
'''


def _rules() -> ScanRules:
    """Return minimal scan rules."""
    return ScanRules(
        ignored_directory_names=frozenset(),
        ignored_path_globs=(),
        language_by_extension={},
        generated_path_globs=(),
        vendor_path_globs=(),
        test_path_globs=(),
    )


def _file(path: str, language: str) -> FileRecord:
    """Return one inventoried file record."""
    return FileRecord(
        path=path,
        language=language,
        size_bytes=1,
        is_generated=False,
        is_vendor=False,
        is_test=False,
    )


@pytest.fixture()
def repo(tmp_path: Path):
    """Write the fixture sources and return the root with file records."""
    sources = {
        "switch.js": (JS_SWITCH, "JavaScript"),
        "chain.js": (JS_IF_CHAIN, "JavaScript"),
        "noise.js": (JS_NOISE, "JavaScript"),
        "match_case.py": (PY_MATCH, "Python"),
        "chain.py": (PY_IF_CHAIN, "Python"),
    }
    files = []
    for name, (content, language) in sources.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
        files.append(_file(name, language))
    return tmp_path, files


def _by_path(repo) -> dict[str, list]:
    """Return dispatchers grouped by source path."""
    root, files = repo
    grouped: dict[str, list] = {}
    for record in inventory_dispatchers_and_switches(root, files, _rules()):
        grouped.setdefault(record.source_path, []).append(record)
    return grouped


def test_a_switch_is_a_dispatcher(repo) -> None:
    """A switch over a named vocabulary is recorded with its branches."""
    record = _by_path(repo)["switch.js"][0]

    assert record.vocabulary == "actionId"
    assert record.branch_count == 2
    assert record.branch_values == ("close_runner", "run_workflow")
    assert record.symbol == "handleAction"


def test_an_if_chain_over_one_subject_is_the_same_decision_point(repo) -> None:
    """Chained else-if over one subject is a dispatcher wearing other syntax."""
    records = _by_path(repo)["chain.js"]

    assert len(records) == 1
    assert records[0].vocabulary == "nodeType"
    assert records[0].branch_values == ("app", "pipeline", "step")


def test_a_python_match_is_a_dispatcher(repo) -> None:
    """match/case is recorded, wildcard excluded."""
    record = _by_path(repo)["match_case.py"][0]

    assert record.vocabulary == "node_type"
    assert record.branch_values == ("app", "pipeline")
    assert record.symbol == "resolve"


def test_a_python_elif_chain_is_a_dispatcher(repo) -> None:
    """elif chains over one subject count, docstring text does not."""
    record = _by_path(repo)["chain.py"][0]

    assert record.vocabulary == "action_id"
    assert record.branch_values == ("reset", "run", "stop")
    assert "ghost" not in record.branch_values


def test_comments_and_strings_are_not_dispatchers(repo) -> None:
    """A switch written in a comment or a string is not a decision point."""
    records = _by_path(repo).get("noise.js", [])

    assert all("fake" not in value for record in records for value in record.branch_values)


def test_a_single_comparison_is_not_a_vocabulary(repo) -> None:
    """One comparison is a condition; a dispatcher needs at least two."""
    symbols = {record.symbol for record in _by_path(repo).get("noise.js", [])}

    assert "single" not in symbols


def test_unrelated_conditions_are_not_one_dispatcher(repo) -> None:
    """Two ifs over different subjects are not a vocabulary."""
    symbols = {record.symbol for record in _by_path(repo).get("noise.js", [])}

    assert "unrelated" not in symbols


def test_every_dispatcher_is_left_unreviewed(repo) -> None:
    """REQ-102: the generator measures, review classifies."""
    root, files = repo

    records = inventory_dispatchers_and_switches(root, files, _rules())

    assert records
    assert {record.classification for record in records} == {"unreviewed"}


def test_callers_come_from_executable_references(repo) -> None:
    """Callers are attributed from reference facts, never guessed."""
    root, files = repo
    references = [
        ReferenceEdge(
            source_path="caller.js",
            source_line=3,
            source_column=2,
            from_id="file:caller.js",
            to="handleAction",
            edge_kind=EDGE_KIND_CALL,
            evidence="call_expression",
        )
    ]

    records = inventory_dispatchers_and_switches(root, files, _rules(), references)
    handler = next(record for record in records if record.symbol == "handleAction")

    assert handler.callers == ("caller.js",)


def test_callers_are_empty_without_reference_evidence(repo) -> None:
    """No evidence means no callers, not an invented one."""
    root, files = repo

    records = inventory_dispatchers_and_switches(root, files, _rules())

    assert all(record.callers == () for record in records)


def test_dispatcher_serializes_to_the_jsonl_shape(repo) -> None:
    """A dispatcher is one complete JSON record."""
    record = _by_path(repo)["switch.js"][0].to_dict()

    assert record["record_type"] == "dispatcher"
    assert record["record_id"] == "dispatcher:switch.js:handleAction:2:2"
    assert record["branch_count"] == 2
    assert record["classification"] == "unreviewed"
    assert "\n" not in json.dumps(record, separators=(",", ":"))


def test_inventory_is_deterministic(repo) -> None:
    """Two runs agree and identities are unique."""
    root, files = repo

    first = inventory_dispatchers_and_switches(root, files, _rules())
    second = inventory_dispatchers_and_switches(root, files, _rules())

    assert first == second
    assert len({record.record_id for record in first}) == len(first)


def test_a_chain_inside_an_unrelated_else_is_its_own_decision_point(repo) -> None:
    """Nesting is not continuation: only a same-subject arm is suppressed.

    A key-handling chain that happens to sit in the else-arm of an unrelated
    check is still a decision point, and suppressing it would silently lose it.
    """
    records = [r for r in _by_path(repo).get("noise.js", []) if r.symbol == "nestedChain"]

    assert [r.vocabulary for r in records] == ["key"]
    assert records[0].branch_values == ("Down", "Up")
