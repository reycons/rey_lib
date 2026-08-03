"""Three named write tiers, and what each one actually promises.

These tests can prove which syscalls are made and in what order. They cannot
prove data survives power loss, and nothing here claims to -- that limit is why
the tier names matter more than usual: a caller reads the name, not the strace.

The rule the tiers exist to enforce is that none of them silently degrades. A
tier that quietly delivers a weaker guarantee is worse than one that refuses,
because the caller believes it succeeded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rey_lib.files import primitive_file_io as pio
from rey_lib.files.primitive_file_io import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_text,
    durable_write_bytes,
    flushed_write_bytes,
    stage_write_bytes,
    stage_stream_write,
)


@pytest.fixture
def syscalls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record fsync, F_FULLFSYNC and replace in the order they happen."""
    seen: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        seen.append("fsync")
        real_fsync(fd)

    def spy_replace(src: object, dst: object) -> None:
        seen.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(pio.os, "fsync", spy_fsync)
    monkeypatch.setattr(pio.os, "replace", spy_replace)

    if pio.fcntl is not None:
        real_fcntl = pio.fcntl.fcntl

        def spy_fcntl(fd: int, cmd: int, *args: object) -> object:
            if cmd == getattr(pio.fcntl, "F_FULLFSYNC", object()):
                seen.append("fullfsync")
            return real_fcntl(fd, cmd, *args)

        monkeypatch.setattr(pio.fcntl, "fcntl", spy_fcntl)
    return seen


# ---------------------------------------------------------------------------
# visibility-atomic makes no persistence claim
# ---------------------------------------------------------------------------

def test_the_default_tier_does_not_sync(tmp_path: Path, syscalls: list[str]) -> None:
    """The cost the default tier must not start paying.

    Fourteen regenerable artifact writers use this path. If it began syncing,
    every one of them would pay for a guarantee none of them asked for.
    """
    atomic_write_text(tmp_path / "a.json", "content")

    assert syscalls == ["replace"]


def test_the_default_tier_still_installs_atomically(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")

    assert target.read_bytes() == b"second"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# ---------------------------------------------------------------------------
# flushed
# ---------------------------------------------------------------------------

def test_flushed_syncs_the_file_before_installing_it(
    tmp_path: Path, syscalls: list[str]
) -> None:
    """Order matters: syncing after the rename would sync the wrong thing."""
    flushed_write_bytes(tmp_path / "m.jsonl", b"payload\n")

    assert syscalls[0] == "fsync"
    assert "replace" in syscalls
    assert syscalls.index("fsync") < syscalls.index("replace")


def test_flushed_syncs_the_directory_after_installing(
    tmp_path: Path, syscalls: list[str]
) -> None:
    """A rename is not persisted until its directory is."""
    flushed_write_bytes(tmp_path / "m.jsonl", b"payload\n")

    assert syscalls == ["fsync", "replace", "fsync"]


def test_flushed_never_uses_the_strongest_flush(
    tmp_path: Path, syscalls: list[str]
) -> None:
    """It must not accidentally deliver -- or charge for -- the top tier."""
    flushed_write_bytes(tmp_path / "m.jsonl", b"payload\n")

    assert "fullfsync" not in syscalls


def test_flushed_tolerates_a_directory_it_cannot_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It makes no power-loss claim, so it does not fail on a weak filesystem."""
    real_open = pio.os.open

    def refuse(path: object, *args: object, **kwargs: object) -> int:
        if str(path) == str(tmp_path):
            raise OSError("directory sync unsupported")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    target = tmp_path / "m.jsonl"
    monkeypatch.setattr(pio.os, "open", refuse)

    assert flushed_write_bytes(target, b"payload\n") == target
    assert target.read_bytes() == b"payload\n"


# ---------------------------------------------------------------------------
# maximum-durability
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS")
def test_maximum_durability_uses_full_fsync_on_macos(
    tmp_path: Path, syscalls: list[str]
) -> None:
    """os.fsync on macOS does not flush the drive cache; F_FULLFSYNC does."""
    durable_write_bytes(tmp_path / "m.jsonl", b"payload\n")

    assert "fullfsync" in syscalls
    assert syscalls.index("fullfsync") < syscalls.index("replace")


def test_maximum_durability_syncs_the_directory_too(
    tmp_path: Path, syscalls: list[str]
) -> None:
    durable_write_bytes(tmp_path / "m.jsonl", b"payload\n")

    assert syscalls[-1] == "fsync"
    assert syscalls.index("replace") < len(syscalls) - 1


def test_maximum_durability_refuses_rather_than_degrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the tiers exist for.

    Falling back to os.fsync here would tell a caller its data is safe on a
    platform where it is not. Refusing is the only honest outcome.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delattr(pio.fcntl, "F_FULLFSYNC", raising=False)

    with pytest.raises(OSError, match="F_FULLFSYNC"):
        durable_write_bytes(tmp_path / "m.jsonl", b"payload\n")


def test_maximum_durability_fails_when_the_directory_cannot_be_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently succeeding would overstate what happened."""
    real_open = pio.os.open

    def refuse(path: object, *args: object, **kwargs: object) -> int:
        if str(path) == str(tmp_path):
            raise OSError("directory sync unsupported")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pio.os, "open", refuse)

    with pytest.raises(OSError):
        durable_write_bytes(tmp_path / "m.jsonl", b"payload\n")


def test_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown write tier"):
        stage_write_bytes(tmp_path / "x", b"", tier="durable")


# ---------------------------------------------------------------------------
# staging: the gate between writing and installing
# ---------------------------------------------------------------------------

def test_the_destination_is_untouched_until_install(tmp_path: Path) -> None:
    target = tmp_path / "m.jsonl"
    target.write_bytes(b"original\n")

    staged = stage_write_bytes(target, b"replacement\n")

    assert staged.path.exists()
    assert staged.path != target
    assert target.read_bytes() == b"original\n"

    staged.install()
    assert target.read_bytes() == b"replacement\n"


def test_staged_content_can_be_read_back_before_installing(tmp_path: Path) -> None:
    """The gate: validate what was actually written, not what was intended."""
    target = tmp_path / "m.jsonl"

    with stage_write_bytes(target, b'{"a": 1}\n') as staged:
        assert staged.path.read_bytes() == b'{"a": 1}\n'
        staged.install()

    assert target.read_bytes() == b'{"a": 1}\n'


def test_rejecting_staged_content_leaves_nothing_behind(tmp_path: Path) -> None:
    target = tmp_path / "m.jsonl"
    target.write_bytes(b"original\n")

    with stage_write_bytes(target, b"rejected\n"):
        pass  # never installed

    assert target.read_bytes() == b"original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["m.jsonl"]


def test_an_exception_while_staged_discards_the_staged_file(tmp_path: Path) -> None:
    target = tmp_path / "m.jsonl"

    with pytest.raises(RuntimeError):
        with stage_write_bytes(target, b"content\n"):
            raise RuntimeError("validation failed")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_staged_file_is_a_sibling_of_its_destination(tmp_path: Path) -> None:
    """So installing is a rename, never a cross-device copy."""
    target = tmp_path / "nested" / "m.jsonl"

    with stage_write_bytes(target, b"content\n") as staged:
        assert staged.path.parent == target.parent


def test_streaming_stage_flushes_and_publishes_incremental_chunks(
    tmp_path: Path,
) -> None:
    target = tmp_path / "streamed.txt"

    with stage_stream_write(target, tier="flushed") as staged:
        staged.write(b"first")
        staged.write(b" second\n")
        staged.install()

    assert target.read_bytes() == b"first second\n"
    assert [path.name for path in tmp_path.iterdir()] == ["streamed.txt"]


def test_streaming_stage_collision_is_no_clobber_and_cleans_staging(
    tmp_path: Path,
) -> None:
    target = tmp_path / "streamed.txt"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        with stage_stream_write(target, tier="flushed") as staged:
            staged.write(b"replacement")
            staged.install()

    assert target.read_bytes() == b"existing"
    assert [path.name for path in tmp_path.iterdir()] == ["streamed.txt"]


def test_streaming_stage_overwrite_is_explicit(tmp_path: Path) -> None:
    target = tmp_path / "streamed.txt"
    target.write_bytes(b"existing")

    with stage_stream_write(target, tier="flushed") as staged:
        staged.write(b"replacement")
        staged.install(overwrite=True)

    assert target.read_bytes() == b"replacement"


# ---------------------------------------------------------------------------
# byte exactness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text", ["a\nb\n", "one line", "", "trailing\n\n", "Ünïcode\n字段\n"]
)
def test_every_write_path_produces_identical_bytes(tmp_path: Path, text: str) -> None:
    """No path may translate a newline. The bytes are what gets hashed."""
    expected = text.encode("utf-8")

    a, b, c, d = (tmp_path / n for n in ("a", "b", "c", "d"))
    atomic_write_text(a, text)
    atomic_write_bytes(b, expected)
    flushed_write_bytes(c, expected)
    durable_write_bytes(d, expected)

    for path in (a, b, c, d):
        assert path.read_bytes() == expected, path.name


def test_appending_writes_exactly_one_newline_byte(tmp_path: Path) -> None:
    """Regression: text mode without newline='' becomes CRLF on Windows.

    This file is append-only governed evidence whose bytes are hashed, so a
    platform-dependent line ending would be a platform-dependent digest.
    """
    target = tmp_path / "manifest.jsonl"
    append_jsonl(target, {"record_id": 1})
    append_jsonl(target, {"record_id": 2})

    raw = target.read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 2
    assert raw.endswith(b"\n")


def test_appending_makes_no_durability_claim(
    tmp_path: Path, syscalls: list[str]
) -> None:
    """Per-record syncing was measured at 89x, and F_FULLFSYNC at 11891x.

    Append synchronization boundaries are a separate decision. This asserts the
    current behaviour is unchanged rather than quietly made expensive.
    """
    append_jsonl(tmp_path / "manifest.jsonl", {"record_id": 1})

    assert syscalls == []
