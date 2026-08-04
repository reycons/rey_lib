"""Focused tests for redacted companions owned by the publication primitive."""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files import (
    FileSetCollisionError,
    FileSetMember,
    publish_file_set,
    redacted_companion_path,
)


@pytest.mark.parametrize(
    ("artifact", "companion"),
    [
        ("/w/MissGEDCPTranMay26.csv", "/w/MissGEDCPTranMay26.redacted.csv"),
        (
            "/w/MissGEDCPTranMay26.kickouts.jsonl",
            "/w/MissGEDCPTranMay26.kickouts.redacted.jsonl",
        ),
        ("/w/report", "/w/report.redacted"),
    ],
)
def test_the_marker_is_inserted_before_the_final_extension(
    artifact: str,
    companion: str,
) -> None:
    """One naming rule for every artifact, whatever its suffixes."""
    assert redacted_companion_path(artifact) == Path(companion)


def test_a_member_without_a_companion_publishes_one_file(tmp_path: Path) -> None:
    published = publish_file_set(
        [FileSetMember(destination=tmp_path / "a.csv", text="x\n")]
    )

    assert published.committed == (tmp_path / "a.csv",)
    assert not (tmp_path / "a.redacted.csv").exists()


def test_a_companion_joins_the_same_publication_set(tmp_path: Path) -> None:
    published = publish_file_set([FileSetMember(
        destination=tmp_path / "a.csv",
        text="name\nreal\n",
        redacted_text="name\nmasked\n",
    )])

    assert set(published.committed) == {
        tmp_path / "a.csv", tmp_path / "a.redacted.csv"
    }
    assert (tmp_path / "a.csv").read_text(encoding="utf-8") == "name\nreal\n"
    assert (tmp_path / "a.redacted.csv").read_text(encoding="utf-8") == "name\nmasked\n"


def test_a_companion_collision_publishes_neither_artifact(tmp_path: Path) -> None:
    """Either both land or neither does — the companion is not a side effect."""
    existing = tmp_path / "a.redacted.csv"
    existing.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileSetCollisionError):
        publish_file_set([FileSetMember(
            destination=tmp_path / "a.csv",
            text="real\n",
            redacted_text="masked\n",
        )])

    assert existing.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "a.csv").exists()


def test_the_set_collision_policy_governs_the_companion(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("old", encoding="utf-8")
    (tmp_path / "a.redacted.csv").write_text("old", encoding="utf-8")

    published = publish_file_set(
        [FileSetMember(
            destination=tmp_path / "a.csv",
            text="new\n",
            redacted_text="masked\n",
        )],
        on_collision="replace",
    )

    assert set(published.replaced) == {
        tmp_path / "a.csv", tmp_path / "a.redacted.csv"
    }
    assert (tmp_path / "a.redacted.csv").read_text(encoding="utf-8") == "masked\n"
