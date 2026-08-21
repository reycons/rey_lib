"""Bootstrap owns the last close of the objects it composed.

Every consumer of a shared Connection correctly refuses to close it: closing a
shared object takes the handle from every other holder. That leaves the
boundary that created them as the only place the final close can happen, and
until now nothing did it -- connections opened during a run stayed open until
the process exited.

Registration is explicit throughout. The collector never looks at the context
for things that happen to have a ``close`` method: a context carries
configuration and application state, and closing things because they look
closeable shuts down what nobody meant to own.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.db import connection as connection_module
from rey_lib.errors.error_utils import StateError
from rey_lib.runtime import (
    collect_runtime,
    register_runtime_object,
    registered_runtime_objects,
)

REPO = Path(__file__).resolve().parents[1]


class _Closeable:
    """A registered object that records how often it was collected."""

    def __init__(self, name: str = "obj", fails: bool = False) -> None:
        self.name = name
        self.fails = fails
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        if self.fails:
            raise RuntimeError(f"{self.name} could not close")


def _ctx(*objects: Any) -> SimpleNamespace:
    """A context with the given objects registered for collection."""
    ctx = SimpleNamespace()
    for obj in objects:
        register_runtime_object(ctx, obj)
    return ctx


class TestRegistration:
    """Explicit, ordered, and never inferred."""

    def test_objects_are_collected_because_they_were_registered(self) -> None:
        first, second = _Closeable("a"), _Closeable("b")

        assert [name for name, _ in registered_runtime_objects(_ctx(first, second))] \
            == ["a", "b"]

    def test_an_unregistered_closeable_is_not_collected(self) -> None:
        """Looking closeable is not the same as being owned."""
        stray = _Closeable("stray")
        ctx = _ctx()
        ctx.something_with_close = stray

        collect_runtime(ctx)

        assert stray.closes == 0

    def test_registering_twice_collects_once(self) -> None:
        obj = _Closeable()
        ctx = _ctx(obj)
        register_runtime_object(ctx, obj)

        collect_runtime(ctx)

        assert obj.closes == 1

    def test_the_collector_does_not_introspect_the_context(self) -> None:
        """Asserted on the source: no scan for close-shaped attributes."""
        import rey_lib.runtime as runtime

        tree = ast.parse(Path(runtime.__file__).read_text(encoding="utf-8"))
        looked_up = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "__dict__" not in looked_up
        assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "vars"
                       for n in ast.walk(tree))


class TestCollection:
    """What collection guarantees."""

    def test_every_registered_object_is_closed(self) -> None:
        first, second, third = _Closeable("a"), _Closeable("b"), _Closeable("c")

        collect_runtime(_ctx(first, second, third))

        assert (first.closes, second.closes, third.closes) == (1, 1, 1)

    def test_each_object_is_collected_at_most_once(self) -> None:
        obj = _Closeable()
        ctx = _ctx(obj)

        collect_runtime(ctx)
        collect_runtime(ctx)
        collect_runtime(ctx)

        assert obj.closes == 1

    def test_an_already_closed_object_is_harmless(self) -> None:
        """A consumer may have closed early; the final close is still safe."""
        connection = connection_module.Connection(
            SimpleNamespace(name="control", provider="postgres"))
        connection.close()

        collect_runtime(_ctx(connection))  # must not raise

        assert connection.is_open is False

    def test_one_failure_does_not_strand_the_rest(self) -> None:
        first = _Closeable("first", fails=True)
        second, third = _Closeable("second"), _Closeable("third")

        with pytest.raises(StateError):
            collect_runtime(_ctx(first, second, third))

        assert (second.closes, third.closes) == (1, 1)

    def test_a_failure_is_surfaced_when_the_application_succeeded(self) -> None:
        with pytest.raises(StateError, match="could not close"):
            collect_runtime(_ctx(_Closeable("broken", fails=True)))

    def test_a_failure_is_reported_but_not_raised_when_suppressed(self) -> None:
        failures = collect_runtime(_ctx(_Closeable("broken", fails=True)), suppress=True)

        assert failures and "broken" in failures[0]


class TestTheBootstrapLifecycle:
    """The boundary every standard launch gets."""

    def _shared(self, monkeypatch, *names: str) -> Any:
        """Patch build_ctx_for_app to compose a ctx with shared connections."""
        from rey_lib.config import bootstrap
        from rey_lib.db.connection import build_connections

        def _build(*_a: Any, **_k: Any) -> SimpleNamespace:
            ctx = SimpleNamespace(
                connections=[SimpleNamespace(name=n, provider="postgres") for n in names])
            ctx.shared_connections = build_connections(ctx)
            for connection in ctx.shared_connections.values():
                register_runtime_object(ctx, connection)
            return ctx

        monkeypatch.setattr(bootstrap, "build_ctx_for_app", _build)
        return bootstrap.app_runtime

    def test_every_launch_collects_at_the_end(self, monkeypatch) -> None:
        app_runtime = self._shared(monkeypatch, "control")

        with patch.object(connection_module, "_db") as backend:
            backend.get_connection.return_value = SimpleNamespace(close=lambda: None)
            with app_runtime("cfg", "rey_loader", "run") as ctx:
                ctx.shared_connections["control"].handle()
                assert ctx.shared_connections["control"].is_open is True

        assert ctx.shared_connections["control"].is_open is False
        assert registered_runtime_objects(ctx) == []

    def test_multiple_shared_connections_are_all_collected(self, monkeypatch) -> None:
        app_runtime = self._shared(monkeypatch, "control", "rey_loader", "reporting")

        with patch.object(connection_module, "_db") as backend:
            backend.get_connection.side_effect = (
                lambda cfg, ctx=None: SimpleNamespace(close=lambda: None))
            with app_runtime("cfg", "rey_loader", "run") as ctx:
                for connection in ctx.shared_connections.values():
                    connection.handle()

        assert [c.is_open for c in ctx.shared_connections.values()] == [False, False, False]

    def test_no_shared_connection_remains_open_after_teardown(self, monkeypatch) -> None:
        app_runtime = self._shared(monkeypatch, "control", "reporting")

        with patch.object(connection_module, "_db") as backend:
            backend.get_connection.side_effect = (
                lambda cfg, ctx=None: SimpleNamespace(close=lambda: None))
            with app_runtime("cfg", "rey_loader", "run") as ctx:
                ctx.shared_connections["control"].handle()

        assert not any(c.is_open for c in ctx.shared_connections.values())

    def test_cleanup_failure_does_not_mask_an_application_exception(
            self, monkeypatch) -> None:
        """The error that ended the run is the one that reaches the caller."""
        from rey_lib.config import bootstrap

        broken = _Closeable("broken", fails=True)
        monkeypatch.setattr(bootstrap, "build_ctx_for_app",
                            lambda *a, **k: _ctx(broken))

        with pytest.raises(ValueError, match="the real failure"):
            with bootstrap.app_runtime("cfg", "app", "run"):
                raise ValueError("the real failure")

        # Cleanup was still attempted, and said so through the logger.
        assert broken.closes == 1

    def test_cleanup_failure_is_raised_when_the_application_succeeded(
            self, monkeypatch) -> None:
        from rey_lib.config import bootstrap

        monkeypatch.setattr(bootstrap, "build_ctx_for_app",
                            lambda *a, **k: _ctx(_Closeable("broken", fails=True)))

        with pytest.raises(StateError, match="runtime cleanup failed"):
            with bootstrap.app_runtime("cfg", "app", "run"):
                pass


class TestAppsDoNotTearDownConnections:
    """Final teardown is not each application's business."""

    def test_no_app_enumerates_shared_connections_to_close_them(self) -> None:
        apps = REPO.parent
        offenders = []
        for path in apps.rglob("*.py"):
            if any(p.startswith(".") or p in {"venv", "build", "dist", "tests"}
                   for p in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "shared_connections" in text and ".close()" in text:
                for line in text.splitlines():
                    if "shared_connections" in line and "close" in line:
                        offenders.append(f"{path.name}: {line.strip()}")

        assert offenders == []


class TestExitCodesAreNotFailures:
    """Entry points end with sys.exit(code), which unwinds as an exception."""

    def _broken(self, monkeypatch) -> Any:
        from rey_lib.config import bootstrap

        monkeypatch.setattr(bootstrap, "build_ctx_for_app",
                            lambda *a, **k: _ctx(_Closeable("broken", fails=True)))
        return bootstrap.app_runtime

    def test_a_zero_exit_is_a_success_so_cleanup_failure_surfaces(
            self, monkeypatch) -> None:
        app_runtime = self._broken(monkeypatch)

        with pytest.raises(StateError, match="runtime cleanup failed"):
            with app_runtime("cfg", "app", "run"):
                raise SystemExit(0)

    def test_a_nonzero_exit_is_the_failure_and_is_not_replaced(
            self, monkeypatch) -> None:
        app_runtime = self._broken(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            with app_runtime("cfg", "app", "run"):
                raise SystemExit(2)

        assert exc.value.code == 2

    def test_a_bare_exit_is_treated_as_success(self, monkeypatch) -> None:
        app_runtime = self._broken(monkeypatch)

        with pytest.raises(StateError):
            with app_runtime("cfg", "app", "run"):
                raise SystemExit()
