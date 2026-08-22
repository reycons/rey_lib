"""Shared fixtures for tests that write run-log records.

Run logging is owned by ``RunLog``. Tests that write records take one, rather
than constructing a context by hand and relying on logging to read fields off
it — that fixture pattern is what produced the ctx-shaped write API in the
first place, so repairing it would re-cement what this migration removes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.logs.run_log import RunLog


def make_run_log(
    tmp_path: Path | str,
    *,
    app: str = "rey_loader",
    run_id: str = "00000000-0000-4000-8000-000000000001",
    run_timestamp: str = "20260822_000000",
    destination: str = "jsonl",
    control: Any = None,
    workflow: str | None = None,
    pipeline: str | None = None,
    path: str | None = None,
) -> RunLog:
    """Build a RunLog writing into ``tmp_path``.

    ``path`` supplies an already-resolved run log, which is how a second writer
    joins an existing run — the cross-process continuation case.
    """
    return RunLog(
        app=app,
        run_id=run_id,
        run_timestamp=run_timestamp,
        log_dir=None if path else str(tmp_path),
        path=path,
        destination=destination,
        control=control,
        workflow=workflow,
        pipeline=pipeline,
    )


@pytest.fixture()
def run_log(tmp_path: Path) -> RunLog:
    """A JSONL run log writing into the test's tmp_path."""
    return make_run_log(tmp_path)
