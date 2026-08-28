"""
Tests for the run identity standard (SGC_Rey_Run_ID_Standard).

The identity/display split still holds, but identity moved: ``run_id`` is
``control.run_manifest.run_manifest_id``, generated when the run is recorded, so
``establish_run_identity`` no longer creates one. What it still owns is display
and filing -- ``run_timestamp`` as a filename-safe YYYYMMDD_HHMMSS value stable
for the execution, and the artifact-naming helper that embeds it and never
overwrites a previous run.

The identity half is covered where it now lives: ``Run.start`` records the run
and carries back the id, and logging refuses a context that has not started one.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import make_run_log, start_test_run

from rey_lib.run import establish_run_identity

from rey_lib.files.file_utils import run_artifact_path
from rey_lib.logs import log_run_record, require_run_id
from rey_lib.logs.logging_setup import setup_logging

# Filename-safe run timestamp pattern: YYYYMMDD_HHMMSS.
_TIMESTAMP_RE = re.compile(r"^\d{8}_\d{6}$")


def test_establish_run_identity_sets_timestamps_only() -> None:
    """run_timestamp is YYYYMMDD_HHMMSS and run_started_at is present."""
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    assert _TIMESTAMP_RE.match(ctx.run_timestamp) is not None
    assert ctx.run_started_at


def test_establish_run_identity_does_not_mint_a_run_id() -> None:
    """Identity comes from the manifest, so nothing here invents one.

    A second minting site is how a process handle and a durable record end up
    describing one execution under two names. There is now exactly one place a
    run_id comes from, and it is the row that records the run.
    """
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    assert not hasattr(ctx, "run_id")


def test_establish_run_identity_is_stable() -> None:
    """A second call leaves already-established timestamps unchanged."""
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    stamps = (ctx.run_timestamp, ctx.run_started_at)
    establish_run_identity(ctx)
    assert (ctx.run_timestamp, ctx.run_started_at) == stamps


def test_logging_requires_an_identity_it_did_not_create() -> None:
    """Logging reads the bound identity and refuses to mint one of its own."""
    with pytest.raises(ValueError, match="No run identity has been established"):
        require_run_id(SimpleNamespace())

    ctx = SimpleNamespace(run_id=42)
    start_test_run(ctx)
    assert require_run_id(ctx) == 42


def test_setup_logging_refuses_an_unidentified_context(run_log, tmp_path: Path) -> None:
    """The launch boundary fails loudly rather than inventing an identity."""
    ctx = SimpleNamespace(log_path=str(tmp_path / "app.{operation}.{timestamp}.log"))

    with pytest.raises(ValueError, match="No run identity has been established"):
        setup_logging(ctx, operation="run")


def test_the_write_path_neither_mints_nor_masks(tmp_path: Path) -> None:
    """The other half of the rule: the write path consumes identity only.

    An unidentified context degrades here exactly as any other write fault
    does -- warned and returning None -- because logging must never mask
    execution. It is the launch boundary above that refuses loudly, not this,
    and nothing on this path invents the identity it is missing.
    """
    from rey_lib.logs.run_log import RunLog

    # Identity is a constructor argument: a run log cannot exist without one,
    # so the write path has nothing left to mint.
    run_log = RunLog(app="probe", run_id="R1", run_timestamp="20260822_000000",
                     log_dir=str(tmp_path))
    assert run_log.run_id == "R1"

    # And a write that cannot land still degrades rather than raising.
    unwritable = RunLog(app="probe", run_id="R1", run_timestamp="20260822_000000")
    assert log_run_record(unwritable, "RUN_START") is None


def test_ensure_helpers_share_one_identity() -> None:
    """Control reads the identity the launch boundary established.

    It does not reach upward to have one minted, and it does not hold one of
    its own: both values are read from the context every time.
    """
    from rey_lib.control import Control

    ctx = SimpleNamespace(
        run_id=42,
        control=SimpleNamespace(procedure_map="control"),
        procedure_maps=[SimpleNamespace(name="control", routine_bindings=[])],
    )
    start_test_run(ctx)
    control = Control(ctx)
    assert control.run_id == ctx.run_id
    assert control.run_timestamp() == ctx.run_timestamp


def test_run_artifact_path_embeds_run_timestamp(tmp_path: Path) -> None:
    """The artifact filename is <artifact_name>.<run_timestamp>.<extension>."""
    path = run_artifact_path(tmp_path, "run_log", "20260706_091845", "jsonl")
    assert path.name == "run_log.20260706_091845.jsonl"
    assert path.parent == tmp_path.resolve()


def test_run_artifact_path_tolerates_leading_dot_extension(tmp_path: Path) -> None:
    """A leading dot on the extension does not double the separator."""
    path = run_artifact_path(tmp_path, "execution_summary", "20260706_091845", ".md")
    assert path.name == "execution_summary.20260706_091845.md"


def test_run_artifact_path_collision_never_overwrites(tmp_path: Path) -> None:
    """A same-timestamp file forces a suffixed name rather than overwriting it."""
    first = run_artifact_path(tmp_path, "run_log", "20260706_091845", "jsonl")
    first.write_text("existing run", encoding="utf-8")
    second = run_artifact_path(tmp_path, "run_log", "20260706_091845", "jsonl")
    assert second != first
    assert second.name.startswith("run_log.20260706_091845_")
    assert second.name.endswith(".jsonl")
