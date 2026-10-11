"""The scan reads configured repositories and nothing authored (backlog 639).

Which repositories are scanned is configuration handed in by the caller. No
architecture document is read, so nothing declared can stop the facts of the
current code from being scanned; an incomplete scope still refuses, because it
would publish part of the estate as if it were the whole.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.repository_map.snapshot import CodeIndexSnapshot, scan


@pytest.fixture()
def apps(tmp_path: Path) -> Path:
    """Two checkouts and no architecture document anywhere."""
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "module.py").write_text(
            "def run() -> int:\n    return 1\n", encoding="utf-8",
        )
    return tmp_path


def test_every_configured_repository_is_scanned(apps: Path) -> None:
    snapshot = scan(apps, ["alpha", "beta"])

    assert isinstance(snapshot, CodeIndexSnapshot)
    assert sorted(snapshot.maps) == ["alpha", "beta"]


def test_only_the_configured_scope_is_scanned(apps: Path) -> None:
    assert sorted(scan(apps, ["beta"]).maps) == ["beta"]


def test_the_facts_need_no_architecture(apps: Path) -> None:
    """A map's facts come from code alone: the checkout holds no architecture."""
    records = scan(apps, ["alpha"]).maps["alpha"].records

    assert {"record_type": "file", "path": "module.py"}.items() <= next(
        record for record in records if record["record_type"] == "file"
    ).items()


def test_an_empty_scope_refuses(apps: Path) -> None:
    with pytest.raises(ConfigError, match="No repositories are configured"):
        scan(apps, [])


def test_a_configured_repository_not_checked_out_refuses(apps: Path) -> None:
    with pytest.raises(ConfigError, match="'gamma' is not checked out"):
        scan(apps, ["alpha", "gamma"])
