"""Provenance validation for canonical generated artifacts.

Contract: rey_architecture_enforcement_layer.sgc.yaml

Content integrity and provenance integrity are separate guarantees, and the
existing gates only prove the first. content_hash covers fact records and
deliberately excludes the header, so a map generated from a scratch copy of a
repository has a perfect hash, matching review anchors and a clean
verify_generated_map_unedited — while its header names a directory that is not
the repository.

That is not hypothetical. It happened on 2026-08-16: a rey_console baseline
generated from a detached worktree recorded repository "rc_main",
repository_root under a scratchpad, and branch "HEAD", and every existing check
passed.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map import validate_map_provenance
from rey_lib.repository_map.writer import RepositoryMap

ROOT = Path("/repos/rey_console")


def _map(**header) -> RepositoryMap:
    """Return a map whose header can be varied per case."""
    base = {
        "record_type": "repository_map",
        "record_id": "repository_map",
        "repository": "rey_console",
        "repository_root": str(ROOT),
        "branch": "main",
        "head_commit": "6e8b7b7",
        "content_hash": "0" * 64,
    }
    base.update(header)
    return RepositoryMap(header=base, records=[])


def test_a_canonical_map_passes() -> None:
    """The header describes the repository it was generated from."""
    assert validate_map_provenance(_map(), "rey_console", ROOT) == []


def test_a_worktree_generated_map_is_rejected() -> None:
    """The exact artifact that passed every other gate."""
    problems = validate_map_provenance(
        _map(
            repository="rc_main",
            repository_root="/private/tmp/scratchpad/rc_main",
            branch="HEAD",
        ),
        "rey_console",
        ROOT,
    )

    assert len(problems) == 3
    assert any("rc_main" in p for p in problems)
    assert any("scratchpad" in p for p in problems)
    assert any("detached" in p for p in problems)


def test_a_wrong_repository_name_is_rejected() -> None:
    """A worktree generates under its own directory name."""
    assert validate_map_provenance(_map(repository="rc_main"), "rey_console", ROOT)


def test_a_scratch_root_is_rejected_even_with_the_right_name() -> None:
    """Name alone is not provenance; the checkout must be the canonical one."""
    problems = validate_map_provenance(
        _map(repository_root="/private/tmp/scratchpad/rey_console"), "rey_console", ROOT
    )

    assert problems and "repository_root" in problems[0]


def test_a_detached_checkout_is_rejected() -> None:
    """A canonical artifact must name the branch it describes."""
    problems = validate_map_provenance(_map(branch="HEAD"), "rey_console", ROOT)

    assert problems and "detached" in problems[0]


def test_provenance_is_independent_of_content() -> None:
    """The guarantees are separate: perfect content, wrong provenance.

    This is why the check is needed at all. Nothing about the fact records
    changes when generation moves to a copy of the repository.
    """
    good, bad = _map(), _map(repository="rc_main")

    assert good.header["content_hash"] == bad.header["content_hash"]
    assert validate_map_provenance(good, "rey_console", ROOT) == []
    assert validate_map_provenance(bad, "rey_console", ROOT) != []
