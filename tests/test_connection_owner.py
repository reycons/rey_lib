"""A configured connection has one runtime Connection object.

The invariant, stated once:

    A configured connection has one runtime Connection object. Any context
    capable of identifying that configured connection resolves to that same
    object. The context never owns or carries it.

This exists because the objectification was implemented against the transport it
replaced. ``shared_connection`` looked the object up in a dict on the context,
so sharing was scoped to a context rather than to the runtime. The Console
resolves a fresh context per request, so every installation-scoped request found
no dict and reported a configured connection as unconfigured -- the database
tree stopped opening, and the message named the wrong cause.

What is under test is therefore *identity across contexts*: not that two
contexts produce equivalent connections, but that they produce the same object,
and that a context which never went through a composition step still reaches it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.connection import (
    Connection,
    build_connections,
    connection_owner,
    shared_connection,
)
from rey_lib.errors.error_utils import ConfigError


def _config(name: str, database: str = "rey_apps") -> SimpleNamespace:
    """One resolved connections[] record."""
    return SimpleNamespace(name=name, provider="postgres", database=database)


def _ctx(config_path: str, *names: str, database: str = "rey_apps") -> SimpleNamespace:
    """A context as ``build_ctx_from_path`` leaves one: config, and its source.

    No composition step, no shared objects -- which is exactly the shape the
    Console resolves per request and the shape that used to resolve to nothing.
    """
    return SimpleNamespace(
        config_path=config_path,
        connections=[_config(name, database) for name in (names or ("control",))],
    )


class TestAnyContextReachesTheSameObject:
    """The property whose absence broke the database tree."""

    def test_a_context_that_composed_nothing_still_resolves(self) -> None:
        ctx = _ctx("/installations/wolff_popper/config/config.yaml")

        resolved = shared_connection(ctx, "control")

        assert isinstance(resolved, Connection)
        assert resolved.name == "control"

    def test_two_contexts_for_one_installation_share_the_object(self) -> None:
        path = "/installations/wolff_popper/config/config.yaml"

        first = shared_connection(_ctx(path), "control")
        second = shared_connection(_ctx(path), "control")

        # Two contexts, resolved independently, one object -- so a handle opened
        # through one is the handle the other works through.
        assert first is second

    def test_a_composed_context_and_a_bare_one_agree(self) -> None:
        """Composition is a check, not the thing that makes sharing work."""
        path = "/installations/wolff_popper/config/config.yaml"
        composed = build_connections(_ctx(path))

        assert shared_connection(_ctx(path), "control") is composed["control"]


class TestIdentityIsTheConfiguredConnection:
    """Not the endpoint, and not the bare name."""

    def test_two_installations_configuring_one_name_stay_separate(self) -> None:
        """Two configured connections are two objects, whatever they point at.

        Both name ``Rey Apps`` and both reach the same database. Collapsing them
        would be deduplication the configuration model never asked for: they are
        two configured connections, declared separately, and either may be
        edited without the other changing.
        """
        wolff = shared_connection(_ctx("/installations/wolff_popper/config.yaml"), "control")
        ccc = shared_connection(_ctx("/installations/ccc/config.yaml"), "control")

        assert wolff is not ccc

    def test_two_names_in_one_installation_are_two_objects(self) -> None:
        path = "/installations/wolff_popper/config.yaml"
        ctx = _ctx(path, "control", "rey_loader")

        assert shared_connection(ctx, "control") is not shared_connection(ctx, "rey_loader")

    def test_an_unconfigured_name_says_what_is_configured(self) -> None:
        ctx = _ctx("/installations/wolff_popper/config.yaml", "control")

        with pytest.raises(ConfigError, match="Known connections: control"):
            shared_connection(ctx, "analytics")


class TestTheRuntimeOwnsTheLifetime:
    """Held until the runtime ends, then closed once."""

    def test_collecting_the_owner_closes_what_it_built(self) -> None:
        ctx = _ctx("/installations/wolff_popper/config.yaml", "control", "rey_loader")
        opened: list[str] = []

        class _Handle:
            def __init__(self, name: str) -> None:
                self.name = name

            def close(self) -> None:
                opened.remove(self.name)

        for name in ("control", "rey_loader"):
            connection = shared_connection(ctx, name)
            opened.append(name)
            connection._handle = _Handle(name)

        connection_owner().close()

        assert opened == []

    def test_the_owner_holds_nothing_after_collection(self) -> None:
        path = "/installations/wolff_popper/config.yaml"
        before = shared_connection(_ctx(path), "control")

        connection_owner().close()

        # A new runtime, so a new object. Nothing survives the boundary.
        assert shared_connection(_ctx(path), "control") is not before

    def test_closing_an_empty_owner_is_harmless(self) -> None:
        connection_owner().close()
        connection_owner().close()  # must not raise


class TestTheContextNeverHoldsIt:
    """The coupling this replaced, asserted as absent."""

    def test_resolving_puts_nothing_on_the_context(self) -> None:
        ctx = _ctx("/installations/wolff_popper/config.yaml")

        shared_connection(ctx, "control")

        assert not hasattr(ctx, "shared_connections")

    def test_no_production_module_reads_a_context_for_connections(self) -> None:
        """One reader would be enough to scope sharing to a context again."""
        from pathlib import Path

        production = Path(__file__).resolve().parents[1] / "rey_lib"
        offenders = [
            path.relative_to(production)
            for path in production.rglob("*.py")
            if path.name != "connection.py"
            and "shared_connections" in path.read_text(encoding="utf-8")
        ]

        assert offenders == []
