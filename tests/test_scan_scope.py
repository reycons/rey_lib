"""The scan reads the estate it finds, and nothing authored (backlogs 639, 1029).

Which repositories are scanned is not configured anywhere: every directory
directly under the apps root that is its own git checkout is a repository of the
estate. No architecture document is read, so nothing declared can stop the facts
of the current code from being scanned; an estate with no checkout still
refuses, because it would publish nothing as if it were the whole.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.repository_map.snapshot import CodeIndexSnapshot, estate_repositories, scan


def _checkout(path: Path) -> None:
    """Make ``path`` a git checkout holding one module."""
    path.mkdir(parents=True)
    (path / "module.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(path)], check=True)


@pytest.fixture()
def apps(tmp_path: Path) -> Path:
    """Two checkouts, a plain directory, a hidden one, and no architecture anywhere."""
    _checkout(tmp_path / "alpha")
    _checkout(tmp_path / "beta")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "loose.zip").write_bytes(b"")
    return tmp_path


def test_every_checkout_of_the_estate_is_scanned(apps: Path) -> None:
    snapshot = scan(apps)

    assert isinstance(snapshot, CodeIndexSnapshot)
    assert sorted(snapshot.maps) == ["alpha", "beta"]


def test_only_checkouts_are_repositories(apps: Path) -> None:
    """A plain directory, a hidden one and a file are not repositories of the estate."""
    assert estate_repositories(apps) == ["alpha", "beta"]


def test_a_directory_inside_another_checkout_is_not_a_repository(tmp_path: Path) -> None:
    """Its top level is the enclosing checkout's, as for an installed package tree."""
    _checkout(tmp_path / "estate")
    (tmp_path / "estate" / "packages").mkdir()
    _checkout(tmp_path / "estate" / "alpha")

    assert estate_repositories(tmp_path / "estate") == ["alpha"]


def test_the_facts_need_no_architecture(apps: Path) -> None:
    """A map's facts come from code alone: the checkout holds no architecture."""
    records = scan(apps).maps["alpha"].records

    assert {"record_type": "file", "path": "module.py"}.items() <= next(
        record for record in records if record["record_type"] == "file"
    ).items()


def test_an_estate_with_no_checkout_refuses(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()

    with pytest.raises(ConfigError, match="No repository is checked out under"):
        scan(tmp_path)


def test_an_apps_root_that_is_not_a_directory_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="is not a directory"):
        scan(tmp_path / "absent")
