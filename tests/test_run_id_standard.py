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

from rey_lib.control.control_utils import ensure_run_id, ensure_run_timestamp
from rey_lib.files.file_utils import run_artifact_path
from rey_lib.logs import resolve_run_identity

# Filename-safe run timestamp pattern: YYYYMMDD_HHMMSS.
_TIMESTAMP_RE = re.compile(r"^\d{8}_\d{6}$")


def test_resolve_run_identity_sets_uuid_and_timestamp() -> None:
    """run_id is a UUID, run_timestamp is YYYYMMDD_HHMMSS, run_started_at present."""
    ctx = SimpleNamespace()
    resolve_run_identity(ctx)
    uuid.UUID(ctx.run_id)  # raises ValueError if run_id is not a valid UUID.
    assert _TIMESTAMP_RE.match(ctx.run_timestamp) is not None
    assert ctx.run_started_at


def test_resolve_run_identity_is_stable() -> None:
    """A second call leaves an already-established identity unchanged."""
    ctx = SimpleNamespace()
    resolve_run_identity(ctx)
    identity = (ctx.run_id, ctx.run_timestamp, ctx.run_started_at)
    resolve_run_identity(ctx)
    assert (ctx.run_id, ctx.run_timestamp, ctx.run_started_at) == identity


def test_ensure_helpers_share_one_identity() -> None:
    """ensure_run_id and ensure_run_timestamp return the shared ctx fields."""
    ctx = SimpleNamespace()
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
