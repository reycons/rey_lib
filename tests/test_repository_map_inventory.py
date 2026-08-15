"""Focused tests for the deterministic repository-map file inventory.

Contract: rey_repository_map_generator.sgc.yaml (INC-001).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.repository_map import (
    ENTRY_POINT_LOAD_UNKNOWN,
    LANGUAGE_UNKNOWN,
    ScanRules,
    inventory_files,
    load_scan_rules,
)
from rey_lib.repository_map.records import matches_any_glob

RULES_TEXT = """
ignored_directory_names:
  - node_modules
  - __pycache__

ignored_path_globs:
  - "*.min.js"

language_by_extension:
  ".py": Python
  ".ts": TypeScript
  ".js": JavaScript

generated_path_globs:
  - "static/js/react/*"

vendor_path_globs: []

test_path_globs:
  - "tests/*"
"""


@pytest.fixture()
def rules(tmp_path: Path) -> ScanRules:
    """Return scan rules parsed from a temporary rules file."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text(RULES_TEXT, encoding="utf-8")
    return load_scan_rules(rules_path)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Build a small repository tree covering every classification branch."""
    root = tmp_path / "repo"
    for relative in (
        "app.py",
        "widget.ts",
        "README.md",
        "static/js/react/bundle.js",
        "tests/test_app.py",
        "node_modules/left-pad/index.js",
        "pkg/__pycache__/app.cpython-311.pyc",
        "vendor.min.js",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    # Hidden file and hidden directory must never be inventoried.
    (root / ".secret").write_text("x", encoding="utf-8")
    (root / ".hidden_dir").mkdir()
    (root / ".hidden_dir" / "app.py").write_text("x", encoding="utf-8")
    return root


def _paths(root: Path, rules: ScanRules) -> list[str]:
    """Return inventoried paths for a repository root."""
    return [record.path for record in inventory_files(root, rules)]


def test_ignored_directories_are_pruned(repo: Path, rules: ScanRules) -> None:
    """Configured directory names are never inventoried."""
    paths = _paths(repo, rules)

    assert not any(path.startswith("node_modules/") for path in paths)
    assert not any("__pycache__" in path for path in paths)


def test_hidden_entries_are_excluded(repo: Path, rules: ScanRules) -> None:
    """Hidden files and hidden directories are excluded unconditionally."""
    paths = _paths(repo, rules)

    assert ".secret" not in paths
    assert not any(path.startswith(".hidden_dir") for path in paths)


def test_ignored_path_globs_exclude_files(repo: Path, rules: ScanRules) -> None:
    """A file matching an ignore glob is not inventoried."""
    assert "vendor.min.js" not in _paths(repo, rules)


def test_inventory_is_sorted_and_repeatable(repo: Path, rules: ScanRules) -> None:
    """Two runs over the same tree produce identical, sorted records."""
    first = inventory_files(repo, rules)
    second = inventory_files(repo, rules)

    assert first == second
    assert [record.path for record in first] == sorted(record.path for record in first)


def test_language_is_mapped_from_configured_extensions(repo: Path, rules: ScanRules) -> None:
    """Configured suffixes map to languages; anything else is unknown."""
    languages = {record.path: record.language for record in inventory_files(repo, rules)}

    assert languages["app.py"] == "Python"
    assert languages["widget.ts"] == "TypeScript"
    assert languages["README.md"] == LANGUAGE_UNKNOWN


def test_classification_flags_follow_configured_globs(repo: Path, rules: ScanRules) -> None:
    """Generated and test globs classify files; unmatched files stay unflagged."""
    records = {record.path: record for record in inventory_files(repo, rules)}

    assert records["static/js/react/bundle.js"].is_generated is True
    assert records["tests/test_app.py"].is_test is True
    assert records["app.py"].is_generated is False
    assert records["app.py"].is_test is False
    assert records["app.py"].is_vendor is False


def test_glob_star_crosses_directory_separators(tmp_path: Path) -> None:
    """A '*' spans separators — these are fnmatch globs, not shell globs."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text('generated_path_globs:\n  - "static/*"\n', encoding="utf-8")
    root = tmp_path / "repo"
    (root / "static" / "js" / "react").mkdir(parents=True)
    (root / "static" / "js" / "react" / "bundle.js").write_text("x", encoding="utf-8")

    records = inventory_files(root, load_scan_rules(rules_path))

    assert [record.is_generated for record in records] == [True]


def test_globs_are_anchored_at_the_repository_root(tmp_path: Path) -> None:
    """'tests/*' matches only a top-level tests directory, not a nested one."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text('test_path_globs:\n  - "tests/*"\n', encoding="utf-8")
    root = tmp_path / "repo"
    for relative in ("tests/test_top.py", "pkg/tests/test_nested.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    records = inventory_files(root, load_scan_rules(rules_path))
    flags = {record.path: record.is_test for record in records}

    assert flags == {"pkg/tests/test_nested.py": False, "tests/test_top.py": True}


def test_glob_matching_is_case_sensitive(tmp_path: Path) -> None:
    """Case sensitivity does not vary with the underlying filesystem."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text('generated_path_globs:\n  - "*.JS"\n', encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bundle.js").write_text("x", encoding="utf-8")

    records = inventory_files(root, load_scan_rules(rules_path))

    assert [record.is_generated for record in records] == [False]


def test_entry_point_state_is_unknown_before_entry_point_discovery(
    repo: Path,
    rules: ScanRules,
) -> None:
    """Entry-point load state is never fabricated ahead of INC-003."""
    states = {record.entry_point_load_state for record in inventory_files(repo, rules)}

    assert states == {ENTRY_POINT_LOAD_UNKNOWN}


def test_size_is_recorded_from_the_filesystem(repo: Path, rules: ScanRules) -> None:
    """Recorded size matches the file on disk."""
    records = {record.path: record for record in inventory_files(repo, rules)}

    assert records["app.py"].size_bytes == (repo / "app.py").stat().st_size


def test_symlinks_are_skipped(repo: Path, rules: ScanRules) -> None:
    """A symlinked file is not inventoried, so scans cannot follow links out."""
    (repo / "link.py").symlink_to(repo / "app.py")

    assert "link.py" not in _paths(repo, rules)


def test_file_serializes_to_the_jsonl_file_record_shape(repo: Path, rules: ScanRules) -> None:
    """FileRecord serializes as a 'file' record, not as a YAML fragment."""
    records = {record.path: record for record in inventory_files(repo, rules)}

    assert records["tests/test_app.py"].to_dict() == {
        "record_type": "file",
        "record_id": "file:tests/test_app.py",
        "path": "tests/test_app.py",
        "language": "Python",
        "size_bytes": 1,
        "classification": {"generated": False, "vendor": False, "test": True},
        "entry_point_load_state": ENTRY_POINT_LOAD_UNKNOWN,
    }


def test_file_record_is_one_json_line(repo: Path, rules: ScanRules) -> None:
    """Every record serializes to exactly one line of complete JSON."""
    for record in inventory_files(repo, rules):
        line = json.dumps(record.to_dict(), separators=(",", ":"))

        assert "\n" not in line
        assert json.loads(line)["record_id"] == f"file:{record.path}"


def test_fact_extraction_defaults_to_on(rules: ScanRules) -> None:
    """Omitting the policy never silently drops facts."""
    assert rules.extract_facts_from_generated is True
    assert rules.extract_facts_from_vendor is True


def test_suppressed_classifications_keep_their_file_records(tmp_path: Path) -> None:
    """A suppressed file still exists in the map; only its facts are skipped."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text(
        'generated_path_globs:\n  - "build/*"\n'
        'vendor_path_globs:\n  - "vendors/*"\n'
        "fact_extraction:\n  generated: false\n  vendor: false\n",
        encoding="utf-8",
    )
    root = tmp_path / "repo"
    for relative in ("app.py", "build/bundle.js", "vendors/lib.js"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    loaded = load_scan_rules(rules_path)

    records = inventory_files(root, loaded)
    extracted = {record.path: loaded.extracts_facts_from(record) for record in records}

    # Every file is still inventoried.
    assert set(extracted) == {"app.py", "build/bundle.js", "vendors/lib.js"}
    assert extracted == {"app.py": True, "build/bundle.js": False, "vendors/lib.js": False}


def test_fact_extraction_section_must_be_booleans(tmp_path: Path) -> None:
    """A near-miss like the string 'false' is rejected, not read as true."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text('fact_extraction:\n  vendor: "false"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="fact_extraction"):
        load_scan_rules(rules_path)


def test_missing_repository_root_is_rejected(tmp_path: Path, rules: ScanRules) -> None:
    """A non-directory root fails loudly rather than returning an empty map."""
    with pytest.raises(NotADirectoryError):
        inventory_files(tmp_path / "absent", rules)


def test_missing_rules_file_is_rejected(tmp_path: Path) -> None:
    """A missing rules path is an error; it is never guessed or defaulted."""
    with pytest.raises(FileNotFoundError):
        load_scan_rules(tmp_path / "absent.yaml")


def test_malformed_rules_section_is_rejected(tmp_path: Path) -> None:
    """A rules section with the wrong shape is rejected with its file named."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text("ignored_directory_names: node_modules\n", encoding="utf-8")

    with pytest.raises(ValueError, match="ignored_directory_names"):
        load_scan_rules(rules_path)


def test_absent_rules_sections_default_to_empty(tmp_path: Path) -> None:
    """A rules file may omit sections; omission means 'no rule', not an error."""
    rules_path = tmp_path / "repository_map.rules.yaml"
    rules_path.write_text('language_by_extension:\n  ".py": Python\n', encoding="utf-8")

    loaded = load_scan_rules(rules_path)

    assert loaded.ignored_directory_names == frozenset()
    assert loaded.test_path_globs == ()


def test_the_shared_matcher_owns_glob_semantics() -> None:
    """One implementation, so the documented behaviour is what consumers get."""
    assert matches_any_glob("static/js/react/bundle.js", ("static/*",))
    # '*' crosses separators; there is no '**'.
    assert matches_any_glob("a/b/c.ts", ("a/*",))
    # Case-sensitive on every platform.
    assert not matches_any_glob("bundle.js", ("*.JS",))
    # Anchored at the repository root.
    assert not matches_any_glob("pkg/tests/x.py", ("tests/*",))


def test_an_empty_pattern_list_means_what_the_caller_declares() -> None:
    """The four consumers did not agree, so the difference is now explicit.

    Three treat no configuration as matching nothing. Registration rules treat
    an unscoped rule as applying everywhere. Consolidating without saying which
    is which would silently change one of them.
    """
    assert matches_any_glob("anything", ()) is False
    assert matches_any_glob("anything", (), when_unconfigured=True) is True
