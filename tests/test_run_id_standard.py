"""
Tests for the run identity standard (SGC_Rey_Run_ID_Standard).

Cover the separated identity/display model: run_id is a UUID, run_timestamp is a
filename-safe YYYYMMDD_HHMMSS value, both are stable for the execution, and the
centralized artifact-naming helper embeds run_timestamp and never overwrites a
previous run.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.control.control_utils import ensure_run_id, ensure_run_timestamp
from rey_lib.files.file_utils import run_artifact_path
from rey_lib.logs import log_run_record, require_run_id
from rey_lib.logs.logging_setup import setup_logging
from rey_lib.run import establish_run_identity

# Filename-safe run timestamp pattern: YYYYMMDD_HHMMSS.
_TIMESTAMP_RE = re.compile(r"^\d{8}_\d{6}$")


def test_establish_run_identity_sets_uuid_and_timestamp() -> None:
    """run_id is a UUID, run_timestamp is YYYYMMDD_HHMMSS, run_started_at present."""
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    uuid.UUID(ctx.run_id)  # raises ValueError if run_id is not a valid UUID.
    assert _TIMESTAMP_RE.match(ctx.run_timestamp) is not None
    assert ctx.run_started_at


def test_establish_run_identity_is_stable() -> None:
    """A second call leaves an already-established identity unchanged."""
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    identity = (ctx.run_id, ctx.run_timestamp, ctx.run_started_at)
    establish_run_identity(ctx)
    assert (ctx.run_id, ctx.run_timestamp, ctx.run_started_at) == identity


def test_logging_requires_an_identity_it_did_not_create() -> None:
    """Logging reads the bound identity and refuses to mint one of its own."""
    with pytest.raises(ValueError, match="No run identity has been established"):
        require_run_id(SimpleNamespace())

    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    assert require_run_id(ctx) == ctx.run_id


def test_setup_logging_refuses_an_unidentified_context(tmp_path: Path) -> None:
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
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.run.jsonl"))

    assert log_run_record(ctx, "RUN_START") is None
    assert getattr(ctx, "run_id", None) is None


def test_ensure_helpers_share_one_identity() -> None:
    """ensure_run_id and ensure_run_timestamp return the shared ctx fields.

    Control reads the identity the launch boundary established; it does not
    reach upward to have one minted.
    """
    ctx = SimpleNamespace()
    establish_run_identity(ctx)
    run_id = ensure_run_id(ctx)
    run_timestamp = ensure_run_timestamp(ctx)
    assert run_id == ctx.run_id
    assert run_timestamp == ctx.run_timestamp
    uuid.UUID(run_id)


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
