"""A procedure map is a routine contract; the caller chooses the connection.

A map declares which routines exist and how they are called. Which database
those routines run on is not its business, and binding one to a connection meant
the same bindings could not be run against a second control database -- a test
instance, another installation -- without duplicating the whole map under a
different connection name.

The guard is inverted rather than deleted. Requiring ``connection_name`` became
refusing it, so the coupling cannot creep back in as a convenience later and be
mistaken for the intended design.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.procedure_map import get_procedure_map, resolve_connection_config
from rey_lib.errors.error_utils import ConfigError


def _ctx(maps: list[Any] | None = None, connections: list[Any] | None = None) -> SimpleNamespace:
    """A context carrying procedure maps and named connections."""
    return SimpleNamespace(procedure_maps=maps or [], db_connections=connections or [])


def _map(name: str, **extra: Any) -> SimpleNamespace:
    """A procedure map record."""
    return SimpleNamespace(name=name, routine_bindings=[], **extra)


def _connection(name: str) -> SimpleNamespace:
    """A named connection record."""
    return SimpleNamespace(name=name, provider="postgres", database="rey_apps")


class TestAMapMayNotCarryAConnection:
    """The invariant, made durable rather than conventional."""

    def test_a_map_carrying_connection_name_is_a_configuration_error(self) -> None:
        ctx = _ctx(maps=[_map("control", connection_name="control")])

        with pytest.raises(ConfigError, match="carries connection_name"):
            get_procedure_map(ctx, "control")

    def test_the_refusal_says_what_to_do_instead(self) -> None:
        ctx = _ctx(maps=[_map("control", connection_name="control")])

        with pytest.raises(ConfigError, match="caller chooses the connection"):
            get_procedure_map(ctx, "control")

    def test_a_connection_agnostic_map_resolves(self) -> None:
        ctx = _ctx(maps=[_map("control")])

        assert get_procedure_map(ctx, "control").name == "control"

    def test_an_empty_connection_name_is_not_a_binding(self) -> None:
        # Absent and blank mean the same thing: the map binds no connection.
        ctx = _ctx(maps=[_map("control", connection_name="")])

        assert get_procedure_map(ctx, "control").name == "control"


class TestTheCallerNamesTheConnection:
    """Resolution is by connection name, never through a map."""

    def test_a_named_connection_resolves(self) -> None:
        ctx = _ctx(connections=[_connection("control"), _connection("rey_loader")])

        assert resolve_connection_config(ctx, "control").name == "control"

    def test_one_map_can_be_run_against_two_databases(self) -> None:
        # The point of the separation: the same routine contract, two targets.
        ctx = _ctx(
            maps=[_map("control")],
            connections=[_connection("control"), _connection("control_test")],
        )

        assert get_procedure_map(ctx, "control").name == "control"
        assert resolve_connection_config(ctx, "control").name == "control"
        assert resolve_connection_config(ctx, "control_test").name == "control_test"

    def test_no_connection_name_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no connection name"):
            resolve_connection_config(_ctx(connections=[_connection("control")]), "")

    def test_an_unknown_connection_is_refused(self) -> None:
        ctx = _ctx(connections=[_connection("control")])

        with pytest.raises(ConfigError, match="not found in"):
            resolve_connection_config(ctx, "nope")

    def test_missing_connections_config_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="db_connections is not configured"):
            resolve_connection_config(SimpleNamespace(), "control")
