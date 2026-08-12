"""How a named configuration entry declared across several files is merged.

A named entry — a workflow, a connection, an LLM profile — may be declared in
more than one file so that one large definition can be split into readable
pieces. One workflow's processes, split one file per process, is the case this
was written for.

Two rules hold together:

* Complementary declarations merge. Keys only one file declares are added, and
  nested mappings and lists merge recursively, so the resolved entry is the
  same as if it had been written in a single file.
* Contradictory declarations still fail closed. Two files declaring the same
  key with different values is the accidental-duplicate mistake the guard was
  written to catch, and it remains an error.
"""

from __future__ import annotations

import pytest

from rey_lib.config.config_loader import _deep_merge
from rey_lib.errors.error_utils import ConfigError


def _workflow_file(process_name: str, config: dict) -> dict:
    """One split workflow file: the shared hierarchy plus a single process."""
    return {
        "workflows": [
            {
                "name": "inventory_and_prepare_files",
                "app": "file_operator",
                "processes": {process_name: config},
            }
        ]
    }


def test_processes_split_across_files_merge_into_one_workflow() -> None:
    """The split itself: two files, one workflow, both processes present."""
    first = _workflow_file("inventory_source_files", {"sources": [{"name": "feed_inbox"}]})
    second = _workflow_file("classify_source_files", {"sources": [{"name": "file_manifest"}]})

    merged = _deep_merge(first, second)

    workflows = merged["workflows"]
    assert len(workflows) == 1, "the workflow was duplicated rather than merged"
    assert workflows[0]["name"] == "inventory_and_prepare_files"
    assert list(workflows[0]["processes"]) == [
        "inventory_source_files",
        "classify_source_files",
    ]


def test_a_split_entry_keeps_the_keys_only_one_file_declares() -> None:
    """The skeleton file's fields survive alongside a process file's."""
    skeleton = {
        "workflows": [
            {
                "name": "inventory_and_prepare_files",
                "app": "file_operator",
                "tokens": {"ccc_data": "/data/ccc"},
                "steps": [{"id": "inventory_source_files"}],
            }
        ]
    }
    process = _workflow_file("inventory_source_files", {"sources": [{"name": "feed_inbox"}]})

    workflow = _deep_merge(skeleton, process)["workflows"][0]

    assert workflow["tokens"] == {"ccc_data": "/data/ccc"}
    assert workflow["steps"] == [{"id": "inventory_source_files"}]
    assert "inventory_source_files" in workflow["processes"]


def test_identical_repeated_declarations_are_not_a_conflict() -> None:
    """Every split file repeats name and app, so repetition must be free."""
    one = _workflow_file("excel_conversion", {"enabled": True})
    two = _workflow_file("sanitize_file", {"enabled": True})

    workflow = _deep_merge(one, two)["workflows"][0]

    assert workflow["app"] == "file_operator"


def test_a_section_declared_empty_is_where_the_split_files_merge() -> None:
    """The skeleton names the section; the process files fill it.

    Writing ``processes:`` with nothing under it is how a skeleton file says
    where the split pieces belong. YAML reads that as None, which must be
    treated as a placeholder rather than a value contradicting the files that
    supply the content.
    """
    skeleton = {
        "workflows": [
            {
                "name": "inventory_and_prepare_files",
                "app": "file_operator",
                "processes": None,
            }
        ]
    }
    process = _workflow_file("inventory_source_files", {"sources": [{"name": "feed_inbox"}]})

    workflow = _deep_merge(skeleton, process)["workflows"][0]

    assert workflow["processes"] == {"inventory_source_files": {"sources": [{"name": "feed_inbox"}]}}


def test_an_empty_declaration_never_erases_content_already_merged() -> None:
    """Order must not matter: a later empty section keeps what is there."""
    process = _workflow_file("inventory_source_files", {"sources": [{"name": "feed_inbox"}]})
    skeleton = {
        "workflows": [
            {"name": "inventory_and_prepare_files", "app": "file_operator", "processes": None}
        ]
    }

    workflow = _deep_merge(process, skeleton)["workflows"][0]

    assert "inventory_source_files" in workflow["processes"]


def test_two_files_contradicting_one_key_still_fail_closed() -> None:
    """The guard the merge was written for: a real duplicate definition."""
    mine = _workflow_file("excel_conversion", {"enabled": True})
    theirs = _workflow_file("excel_conversion", {"enabled": False})

    with pytest.raises(ConfigError, match="declared with different values"):
        _deep_merge(mine, theirs)


def test_a_contradiction_names_the_key_it_was_found_under() -> None:
    """A conflict deep in a split entry has to say where it is."""
    mine = _workflow_file("excel_conversion", {"outbox": {"overwrite": True}})
    theirs = _workflow_file("excel_conversion", {"outbox": {"overwrite": False}})

    with pytest.raises(ConfigError, match=r"processes\.excel_conversion\.outbox"):
        _deep_merge(mine, theirs)


def test_entries_with_different_names_still_concatenate() -> None:
    """The ordinary case is unchanged: separate names stay separate entries."""
    mine = {"workflows": [{"name": "one", "app": "file_operator"}]}
    theirs = {"workflows": [{"name": "two", "app": "file_operator"}]}

    assert [w["name"] for w in _deep_merge(mine, theirs)["workflows"]] == ["one", "two"]
