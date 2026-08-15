"""Focused tests for the system repository-map index.

Contract: rey_system_repository_map_correction.sgc.yaml (COR-004).

The index binds repository baselines. It copies no fact, and it goes stale
loudly rather than quietly describing a map that has since changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.jsonl import read_jsonl_file
from rey_lib.repository_map.system_index import (
    build_system_index,
    load_repository_baselines,
    validate_system_index,
    write_system_index,
)
from rey_lib.repository_map.writer import RepositoryMap, generate_repository_map

RULES = """
language_by_extension:
  ".py": Python
module_extensions:
  - ".py"
module_index_files:
  - __init__.py
"""


def _repository(root: Path, name: str, source: str) -> Path:
    """Create a tiny repository with scan rules and one module."""
    path = root / name
    path.mkdir(parents=True)
    (path / "repository_map.rules.yaml").write_text(RULES, encoding="utf-8")
    (path / "main.py").write_text(source, encoding="utf-8")
    return path


@pytest.fixture()
def system(tmp_path: Path):
    """Build two repositories where one imports the other, and their maps."""
    apps = tmp_path / "apps"
    context = tmp_path / "context"
    context.mkdir()
    _repository(apps, "shared_lib", "def helper():\n    return 1\n")
    _repository(apps, "consumer", "import shared_lib.helpers\n\n\ndef run():\n    return 2\n")
    for name in ("shared_lib", "consumer"):
        generate_repository_map(apps / name, context / f"03_repository_map.{name}.generated.jsonl")
    maps = load_repository_baselines(context, ["shared_lib", "consumer"])
    return context, maps


def test_the_index_names_every_member_and_its_commit(system) -> None:
    """A reader can identify the exact commit of every repository."""
    context, maps = system

    index = build_system_index(context, maps)
    baselines = {r["repository"]: r for r in index.baselines()}

    assert set(baselines) == {"shared_lib", "consumer"}
    for record in baselines.values():
        assert record["content_hash"]
        assert record["record_count"]
        assert record["artifact_path"].endswith(".generated.jsonl")


def test_a_cross_repository_relationship_is_derived_from_facts(system) -> None:
    """The relationship exists because a dependency edge proved it."""
    context, maps = system

    index = build_system_index(context, maps)
    edges = [r for r in index.records if r["record_type"] == "cross_repository_edge"]

    assert [(e["from_repository"], e["to_repository"]) for e in edges] == [
        ("consumer", "shared_lib")
    ]
    assert edges[0]["evidence_record_ids"]


def test_every_evidence_id_resolves_into_its_own_repository(system) -> None:
    """A claim in the index resolves back to a fact in a repository map."""
    context, maps = system

    index = build_system_index(context, maps)
    known = {name: {r["record_id"] for r in report.records} for name, report in maps.items()}

    for record in index.records:
        if record["record_type"] != "cross_repository_edge":
            continue
        for evidence in record["evidence_record_ids"]:
            assert evidence in known[record["from_repository"]]


def test_a_third_party_target_stays_external(system) -> None:
    """Membership is what makes a name internal; nothing else does."""
    context, maps = system

    index = build_system_index(context, maps)
    targets = {r.get("to_repository") for r in index.records}

    # 'os' is imported by nothing here, but no non-member may ever appear.
    assert targets <= {"shared_lib", "consumer", None}


def test_no_repository_fact_is_copied_into_the_index(system, tmp_path: Path) -> None:
    """The index binds baselines; it does not restate their contents."""
    context, maps = system
    output = tmp_path / "system.jsonl"

    write_system_index(build_system_index(context, maps), output)
    kinds = {row.record["record_type"] for row in read_jsonl_file(output)}

    assert kinds == {"system_repository_map", "repository_baseline", "cross_repository_edge"}


def test_a_changed_baseline_invalidates_the_index(system) -> None:
    """A system baseline is only as current as its least current member."""
    context, maps = system
    index = build_system_index(context, maps)

    maps["shared_lib"].header["content_hash"] = "0" * 64
    problems = validate_system_index(index, maps)

    assert any("shared_lib baseline has changed" in problem for problem in problems)


def test_an_edited_index_is_detected(system) -> None:
    """The index carries its own content_hash and must match it."""
    context, maps = system
    index = build_system_index(context, maps)

    index.records[0]["head_commit"] = "tampered"

    assert any("own content_hash" in problem for problem in validate_system_index(index, maps))


def test_a_missing_member_baseline_is_refused(tmp_path: Path) -> None:
    """A system baseline cannot be assembled from an incomplete set."""
    context = tmp_path / "context"
    context.mkdir()

    with pytest.raises(FileNotFoundError, match="Canonical baseline missing"):
        load_repository_baselines(context, ["absent_repository"])


def test_an_index_of_an_intact_system_is_clean(system) -> None:
    """Nothing is reported when every referenced baseline is current."""
    context, maps = system

    assert validate_system_index(build_system_index(context, maps), maps) == []


def test_the_index_is_deterministic(system) -> None:
    """Two builds of one system agree."""
    context, maps = system

    first = build_system_index(context, maps)
    second = build_system_index(context, maps)

    assert first.records == second.records
    assert first.header["content_hash"] == second.header["content_hash"]


def test_a_baseline_without_a_review_says_so(system) -> None:
    """A missing review is a fact about completeness, not an omission."""
    context, maps = system

    index = build_system_index(context, maps)

    assert {r["review_artifact_path"] for r in index.baselines()} == {"not_present"}


def test_policy_status_is_carried_not_inferred(system) -> None:
    """The index reports what a baseline says about policy, nothing more."""
    context, maps = system

    index = build_system_index(context, maps)

    assert {r["architecture_policy_status"] for r in index.baselines()} == {"not_configured"}


def test_a_baseline_predating_the_policy_field_is_unrecorded(system) -> None:
    """Neither evaluated nor not_configured is a claim such a map makes."""
    context, maps = system
    del maps["consumer"].header["architecture_policy_status"]

    index = build_system_index(context, maps)
    consumer = next(r for r in index.baselines() if r["repository"] == "consumer")

    assert consumer["architecture_policy_status"] == "unrecorded"
