"""Focused tests for deterministic JSONL output and record_id diffing.

Contract: rey_repository_map_generator.sgc.yaml (INC-007).

Serialization and file IO belong to rey_lib.files.jsonl; these tests read the
generated map back through that same canonical reader rather than parsing it
independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.jsonl import read_jsonl_file
from rey_lib.repository_map.writer import (
    RepositoryMap,
    content_hash_of,
    compare_repository_maps,
    generate_repository_map,
    write_repository_map,
)

RULES_NAME = "repository_map.rules.yaml"

RULES = """
language_by_extension:
  ".py": Python
  ".js": JavaScript

architecture_rules:
  - rule_id: no_direct_host_mount
    forbidden_target_globs: ["*ReyEmbeddedHost.mount"]
    scope_path_globs: ["*"]

dispatcher_rules:
  - rule_id: no_dispatch_here
    forbidden_vocabulary_globs: ["*"]
    scope_path_globs: ["*"]
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Build a tiny repository with its own scan rules."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / RULES_NAME).write_text(RULES, encoding="utf-8")
    (root / "app.py").write_text("import os\n\n\ndef run():\n    os.getcwd()\n", encoding="utf-8")
    (root / "widget.js").write_text(
        "function go() { run(); }\n"
        "function bad() { window.ReyEmbeddedHost.mount(1); }\n"
        "function route(k) {\n"
        '  if (k === "a") { return 1; }\n'
        '  else if (k === "b") { return 2; }\n'
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def _map(path: Path) -> RepositoryMap:
    """Return a small map for building diff fixtures."""
    return RepositoryMap(
        header={"record_type": "repository_map", "record_id": "repository_map"},
        records=[
            {"record_type": "file", "record_id": "file:a.py", "size_bytes": 1},
            {"record_type": "file", "record_id": "file:b.py", "size_bytes": 2},
        ],
    )


def test_generated_map_is_strict_jsonl(repo: Path, tmp_path: Path) -> None:
    """Every line is one complete JSON object with identity fields (AC-011)."""
    output = tmp_path / "map.jsonl"

    generate_repository_map(repo, output, repo / RULES_NAME)
    rows = read_jsonl_file(output)

    assert rows
    assert rows[0].record["record_type"] == "repository_map"
    for row in rows:
        assert "record_type" in row.record
        assert "record_id" in row.record


def test_two_runs_are_byte_identical_except_the_timestamp(repo: Path, tmp_path: Path) -> None:
    """Same source, rules and version produce the same bytes (AC-001)."""
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"

    first = generate_repository_map(repo, first_path, repo / RULES_NAME)
    second = generate_repository_map(repo, second_path, repo / RULES_NAME)

    assert first.records == second.records
    assert first.header["content_hash"] == second.header["content_hash"]
    # Only the header line may differ, and only in generated_at.
    assert first_path.read_text().splitlines()[1:] == second_path.read_text().splitlines()[1:]


def test_generated_at_is_excluded_from_the_content_hash(repo: Path, tmp_path: Path) -> None:
    """A wall-clock field must not make the map look changed."""
    first = generate_repository_map(repo, tmp_path / "first.jsonl", repo / RULES_NAME)
    second = generate_repository_map(repo, tmp_path / "second.jsonl", repo / RULES_NAME)

    assert first.header["generated_at"] != second.header["generated_at"]
    assert first.header["content_hash"] == second.header["content_hash"]


def test_header_carries_provenance_and_fingerprints(repo: Path, tmp_path: Path) -> None:
    """A stale map is detectable from the header alone (REQ-114)."""
    header = generate_repository_map(repo, tmp_path / "map.jsonl", repo / RULES_NAME).header

    assert header["repository"] == "repo"
    assert header["generator_version"]
    assert header["rules_hash"]
    assert header["content_hash"]
    assert header["working_tree_status"] in {"clean", "dirty", "unknown"}


def test_changing_the_rules_changes_the_rules_hash(repo: Path, tmp_path: Path) -> None:
    """The rules a scan ran under are part of its provenance."""
    first = generate_repository_map(repo, tmp_path / "before.jsonl", repo / RULES_NAME)
    before = first.header["rules_hash"]
    (repo / RULES_NAME).write_text(
        RULES + 'test_path_globs:\n  - "tests/*"\n', encoding="utf-8"
    )

    second = generate_repository_map(repo, tmp_path / "after.jsonl", repo / RULES_NAME)
    after = second.header["rules_hash"]

    assert before != after


def test_reordering_records_is_not_a_change(tmp_path: Path) -> None:
    """Maps are compared by identity, never by line position (AC-012)."""
    original = _map(tmp_path)
    reordered = RepositoryMap(header=original.header, records=list(reversed(original.records)))

    diff = compare_repository_maps(original, reordered)

    assert diff.is_empty
    assert diff.unchanged_count == 2


def test_diff_reports_added_removed_and_changed_by_record_id(tmp_path: Path) -> None:
    """One changed fact reports exactly that record_id."""
    old = _map(tmp_path)
    new = RepositoryMap(
        header=old.header,
        records=[
            {"record_type": "file", "record_id": "file:a.py", "size_bytes": 99},
            {"record_type": "file", "record_id": "file:c.py", "size_bytes": 3},
        ],
    )

    diff = compare_repository_maps(old, new)

    assert diff.changed == ("file:a.py",)
    assert diff.added == ("file:c.py",)
    assert diff.removed == ("file:b.py",)
    assert diff.unchanged_count == 0


def test_written_map_round_trips_through_the_canonical_reader(tmp_path: Path) -> None:
    """What is written is what rey_lib.files.jsonl reads back."""
    original = _map(tmp_path)
    output = tmp_path / "map.jsonl"

    write_repository_map(original, output)
    rows = read_jsonl_file(output)

    assert [dict(row.record) for row in rows][1:] == original.records


def test_a_declared_rules_path_that_is_absent_is_refused(tmp_path: Path) -> None:
    """Naming a rules file that is not there is an error, and stays one."""
    root = tmp_path / "bare"
    root.mkdir()

    with pytest.raises(FileNotFoundError):
        generate_repository_map(root, tmp_path / "map.jsonl", root / RULES_NAME)


def test_a_repository_declaring_no_rules_still_produces_facts(tmp_path: Path) -> None:
    """No rules is a supported state, not a failure.

    This narrows the previous contract deliberately: refusal now means a path
    was declared and is missing, not that a repository was silent.
    """
    root = tmp_path / "unruled"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    report = generate_repository_map(root, tmp_path / "map.jsonl")

    assert report.header["rules_hash"] == "not_configured"
    assert report.header["architecture_policy_status"] == "not_configured"
    # A file inventory needs no language map.
    assert any(r["record_type"] == "file" for r in report.records)


def test_two_repositories_may_keep_rules_in_different_places(tmp_path: Path) -> None:
    """Rules location is caller input, so layout differences need no code change."""
    conventional = tmp_path / "conventional"
    conventional.mkdir()
    (conventional / RULES_NAME).write_text(RULES, encoding="utf-8")
    (conventional / "app.py").write_text("def a():\n    return 1\n", encoding="utf-8")

    unconventional = tmp_path / "unconventional"
    (unconventional / "config").mkdir(parents=True)
    (unconventional / "config" / "scan.yaml").write_text(RULES, encoding="utf-8")
    (unconventional / "app.py").write_text("def b():\n    return 2\n", encoding="utf-8")

    first = generate_repository_map(conventional, tmp_path / "a.jsonl", conventional / RULES_NAME)
    second = generate_repository_map(
        unconventional, tmp_path / "b.jsonl", unconventional / "config" / "scan.yaml"
    )

    assert first.header["rules_hash"] == second.header["rules_hash"]
    assert all(r.header["architecture_policy_status"] == "evaluated" for r in (first, second))


def test_the_map_carries_every_generated_record_type(repo: Path, tmp_path: Path) -> None:
    """A map missing a record type is incomplete relative to the authority.

    Dispatcher and architecture_violation records are produced during
    generation, so the map states what its own evidence supports rather than
    leaving a consumer to recompute it.
    """
    output = tmp_path / "map.jsonl"

    generate_repository_map(repo, output, repo / RULES_NAME)
    kinds = {row.record["record_type"] for row in read_jsonl_file(output)}

    assert {"repository_map", "file", "symbol", "dependency_edge"} <= kinds
    assert "dispatcher" in kinds
    assert "architecture_violation" in kinds


def test_a_violation_in_the_map_names_its_rule_and_evidence(repo: Path, tmp_path: Path) -> None:
    """A verdict in the stream is checkable against the facts beneath it."""
    output = tmp_path / "map.jsonl"

    generate_repository_map(repo, output, repo / RULES_NAME)
    violations = [
        row.record
        for row in read_jsonl_file(output)
        if row.record["record_type"] == "architecture_violation"
    ]

    # The fixture breaks both declared rules, so both verdicts must appear.
    assert {v["rule_id"] for v in violations} == {"no_direct_host_mount", "no_dispatch_here"}
    mount = next(v for v in violations if v["rule_id"] == "no_direct_host_mount")
    assert mount["callee"] == "window.ReyEmbeddedHost.mount"
    assert all(v["evidence_record_ids"] for v in violations)


def test_dispatcher_facts_reach_the_map_unreviewed(repo: Path, tmp_path: Path) -> None:
    """The map records decision points without classifying them."""
    output = tmp_path / "map.jsonl"

    generate_repository_map(repo, output, repo / RULES_NAME)
    dispatchers = [
        row.record
        for row in read_jsonl_file(output)
        if row.record["record_type"] == "dispatcher"
    ]

    assert dispatchers
    assert {d["classification"] for d in dispatchers} == {"unreviewed"}


NO_POLICY_RULES = """
language_by_extension:
  ".py": Python
"""


def test_a_repository_with_policy_reports_it_as_evaluated(repo: Path, tmp_path: Path) -> None:
    """Declared rules mean policy ran."""
    header = generate_repository_map(repo, tmp_path / "map.jsonl", repo / RULES_NAME).header

    assert header["architecture_policy_status"] == "evaluated"


def test_a_repository_without_policy_is_not_configured_not_clean(tmp_path: Path) -> None:
    """Zero violations without policy must not read as architecture accepted."""
    root = tmp_path / "bare"
    root.mkdir()
    (root / RULES_NAME).write_text(NO_POLICY_RULES, encoding="utf-8")
    (root / "app.py").write_text("import os\n\n\ndef run():\n    os.getcwd()\n", encoding="utf-8")
    output = tmp_path / "map.jsonl"

    report = generate_repository_map(root, output, root / RULES_NAME)
    kinds = [r["record_type"] for r in report.records]

    assert report.header["architecture_policy_status"] == "not_configured"
    assert "architecture_violation" not in kinds
    # Facts are still produced; only policy is absent.
    assert "symbol" in kinds and "file" in kinds


def test_the_policy_status_does_not_enter_the_content_hash(repo: Path, tmp_path: Path) -> None:
    """A header field must not disturb a fact hash or an existing baseline."""
    report = generate_repository_map(repo, tmp_path / "map.jsonl", repo / RULES_NAME)

    assert content_hash_of(report.records) == report.header["content_hash"]
