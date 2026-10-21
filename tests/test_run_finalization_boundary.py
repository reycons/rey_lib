"""A run, once created, always reaches a terminal status (#1182).

Its creator holds it until the context is handed on, and app_runtime holds it from
there: a failure on either side of that hand-over ends the run FAILED rather than
leaving it RUNNING with nobody left to finish it. A run this process did not start
is never finished here.
"""

from __future__ import annotations

import os
import signal
import time

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config import bootstrap
from rey_lib.errors.error_utils import AppError, ConfigError


class _Run:
    """A started run: records the terminal status it is given."""

    def __init__(self) -> None:
        self.run_id = 41
        self.finished: list[str] = []
        self.log: Any = None

    def finish(self, status: str, finished_at: Any = None) -> None:
        self.finished.append(status)


def _started(monkeypatch: pytest.MonkeyPatch, run: _Run) -> None:
    """build_ctx_for_app's collaborators, up to and including starting ``run``."""
    monkeypatch.setattr(bootstrap, "build_connections", lambda ctx: None)
    monkeypatch.setattr(bootstrap, "register_runtime_object", lambda ctx, obj: None)
    monkeypatch.setattr(bootstrap, "connection_owner", lambda: object())
    monkeypatch.setattr(bootstrap, "_open_control", lambda ctx: SimpleNamespace())
    monkeypatch.setattr(bootstrap.Run, "start", staticmethod(lambda *a, **k: run))


def _refuse(*_a: Any, **_k: Any) -> None:
    raise RuntimeError("could not settle the run identity")


class TestTheCreatorHoldsTheRun:
    def test_a_failure_after_the_run_starts_finishes_it_failed(self, monkeypatch) -> None:
        run = _Run()
        _started(monkeypatch, run)
        monkeypatch.setattr(bootstrap, "establish_run_identity", _refuse)

        with pytest.raises(RuntimeError, match="could not settle"):
            bootstrap.build_ctx_for_app(ctx=SimpleNamespace(), app_name="app")

        assert run.finished == ["FAILED"]

    def test_a_run_this_process_adopted_is_not_finished(self, monkeypatch) -> None:
        run = _Run()
        _started(monkeypatch, run)
        monkeypatch.setattr(bootstrap, "establish_run_identity", _refuse)

        with pytest.raises(RuntimeError):
            bootstrap.build_ctx_for_app(ctx=SimpleNamespace(run_id=7), app_name="app")

        assert run.finished == []

    def test_a_run_that_cannot_be_closed_never_replaces_the_failure(
            self, monkeypatch) -> None:
        run = _Run()
        run.finish = lambda *_a, **_k: (_ for _ in ()).throw(OSError("manifest gone"))
        _started(monkeypatch, run)
        monkeypatch.setattr(bootstrap, "establish_run_identity", _refuse)

        with pytest.raises(RuntimeError, match="could not settle"):
            bootstrap.build_ctx_for_app(ctx=SimpleNamespace(), app_name="app")


class TestAppRuntimeHoldsItFromThere:
    def _handed(self, monkeypatch, run: _Run) -> SimpleNamespace:
        ctx = SimpleNamespace(run=run, run_id=run.run_id,
                              run_timestamp="20261021_000000")
        monkeypatch.setattr(bootstrap, "build_ctx_for_app", lambda *a, **k: ctx)
        return ctx

    @pytest.mark.parametrize("failing", ["open_run_log", "open_shared_ai"])
    def test_a_failure_before_the_block_runs_finishes_the_run_failed(
            self, monkeypatch, failing: str) -> None:
        run = _Run()
        self._handed(monkeypatch, run)
        if failing == "open_shared_ai":
            monkeypatch.setattr(bootstrap, "open_run_log",
                                lambda ctx, phase_started=None: SimpleNamespace(close=lambda: None))
            monkeypatch.setattr(bootstrap, "finalize_run_phases", lambda run_log: None)
        monkeypatch.setattr(bootstrap, failing, _refuse)

        with pytest.raises(AppError, match="unexpected RuntimeError"):
            with bootstrap.app_runtime("cfg", "app", "run"):
                pytest.fail("the block must not run")

        assert run.finished == ["FAILED"]


class TestTerminationPolicy:
    """SIGTERM ends a run as its application's entry point declared (#1182)."""

    def _handed(self, monkeypatch, run: _Run, **ctx_values: Any) -> None:
        ctx = SimpleNamespace(run=run, run_id=run.run_id,
                              run_timestamp="20261021_000000", **ctx_values)
        monkeypatch.setattr(bootstrap, "build_ctx_for_app", lambda *a, **k: ctx)
        monkeypatch.setattr(bootstrap, "open_run_log",
                            lambda ctx, phase_started=None: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(bootstrap, "finalize_run_phases", lambda run_log: None)
        monkeypatch.setattr(bootstrap, "open_shared_ai", lambda ctx: None)

    @pytest.mark.parametrize("policy, code", [("ABORTED", 143), ("SUCCEEDED", 0)])
    def test_sigterm_finishes_the_run_as_the_policy_says(
            self, monkeypatch, policy: str, code: int) -> None:
        run = _Run()
        self._handed(monkeypatch, run)

        with pytest.raises(bootstrap.Terminated) as ended:
            with bootstrap.app_runtime("cfg", "app", "run", on_terminate=policy):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)

        assert run.finished == [policy]
        assert ended.value.code == code

    def test_a_failure_the_application_reported_never_overrides_the_termination(
            self, monkeypatch) -> None:
        run = _Run()
        self._handed(monkeypatch, run, run_failed=True)

        with pytest.raises(bootstrap.Terminated):
            with bootstrap.app_runtime("cfg", "app", "run", on_terminate="ABORTED"):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)

        assert run.finished == ["ABORTED"]

    def test_the_mechanism_is_in_place_before_the_run_exists(self, monkeypatch) -> None:
        # A SIGTERM while the run is being composed ends it with the policy's status.
        run = _Run()
        _started(monkeypatch, run)

        def _terminated_while_composing(*_a: Any, **_k: Any) -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)

        monkeypatch.setattr(bootstrap, "establish_run_identity", _terminated_while_composing)

        with pytest.raises(bootstrap.Terminated):
            with bootstrap.app_runtime(ctx=SimpleNamespace(), app_name="app",
                                       on_terminate="ABORTED"):
                pytest.fail("the block must not run")

        assert run.finished == ["ABORTED"]

    def test_what_it_replaced_is_restored(self, monkeypatch) -> None:
        before = signal.getsignal(signal.SIGTERM)
        self._handed(monkeypatch, _Run())

        with bootstrap.app_runtime("cfg", "app", "run", on_terminate="ABORTED"):
            assert signal.getsignal(signal.SIGTERM) is not before

        assert signal.getsignal(signal.SIGTERM) is before

    def test_no_policy_installs_nothing(self, monkeypatch) -> None:
        before = signal.getsignal(signal.SIGTERM)
        self._handed(monkeypatch, _Run())

        with bootstrap.app_runtime("cfg", "app", "run"):
            assert signal.getsignal(signal.SIGTERM) is before

    def test_an_unknown_policy_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="on_terminate"):
            with bootstrap.app_runtime("cfg", "app", "run", on_terminate="CANCELLED"):
                pytest.fail("the block must not run")
