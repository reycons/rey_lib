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
from rey_lib.run import establish_run_identity

_NEXT_TEST_RUN_ID = [1]


def start_test_run(ctx: Any, run_id: int | None = None) -> Any:
    """Give ``ctx`` the identity a launched run would carry.

    In production identity comes from the manifest: ``Run.start`` inserts the
    row, the database generates ``run_manifest_id``, and the application carries
    it as ``run_id``. ``establish_run_identity`` only adds the display and
    filing timestamps.

    A test that needs a launched-looking context wants both halves without a
    database, so this supplies the id the manifest would have generated and then
    calls the real timestamp step. Ids are distinct across calls for the same
    reason real ones are: two runs are two runs.
    """
    if run_id is None:
        run_id = getattr(ctx, "run_id", None)
    if run_id is None:
        run_id = _NEXT_TEST_RUN_ID[0]
        _NEXT_TEST_RUN_ID[0] += 1
    ctx.run_id = run_id
    establish_run_identity(ctx)
    return ctx


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


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the launch boundary start a run without a control database.

    Recording the run is what creates its identity, so ``build_ctx_for_app``
    reaches the control database on every launch. A test about what the
    bootstrap does *around* that -- starting logging, installing the error
    boundary, collecting shared objects -- should not need a database standing
    up to say so.

    Patches the creation only. Everything after it, including the ordering that
    puts the run before logging, runs exactly as it does in production.
    """
    from rey_lib.config import bootstrap
    from rey_lib.run import Run

    def _start(control: Any, **kwargs: Any) -> Run:
        run_id = _NEXT_TEST_RUN_ID[0]
        _NEXT_TEST_RUN_ID[0] += 1
        return Run(run_id=run_id, control=control, **{
            k: v for k, v in kwargs.items() if k != "control"})

    monkeypatch.setattr(bootstrap, "_open_control", lambda ctx: object())
    monkeypatch.setattr(bootstrap.Run, "start", staticmethod(_start))


@pytest.fixture(autouse=True)
def _own_no_connections_between_tests() -> Any:
    """Give every test a runtime holding no connections.

    Connection objects belong to the runtime, not to a context, which is what
    lets any context identifying a configured connection reach the same object.
    Under test that same property makes one test's connections visible to the
    next, so the runtime is emptied between them -- the equivalent of a fresh
    process, which is what each test is pretending to be.
    """
    from rey_lib.db.connection import connection_owner

    connection_owner().close()
    yield
    connection_owner().close()
