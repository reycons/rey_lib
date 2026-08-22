"""A procedure map is a routine contract; the caller chooses the connection.

A map declares which routines exist and how they are called. Which database
those routines run on is not its business, and binding one to a connection
meant the same bindings could not be run against a second control database -- a
test instance, another installation -- without duplicating the whole map under
a different connection name.

The property is unchanged; its subject moved. The separation used to be shown
through ``resolve_connection_config``, which answered "which connection config
does this name mean". Connections are now shared objects built once at
composition, so the same statement is made against ``build_connections`` and
``shared_connection``: the caller takes a Connection, the map supplies the
routine, and neither knows about the other.

The guard on the map side is inverted rather than deleted. Requiring
``connection_name`` became refusing it, so the coupling cannot creep back in as
a convenience later and be mistaken for the intended design.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import make_run_log

from rey_lib.db import connection as connection_module
from rey_lib.db.connection import build_connections, shared_connection
from rey_lib.db.procedure_map import execute_mapped_routine, get_procedure_map
from rey_lib.errors.error_utils import ConfigError


def _ctx(maps: list[Any] | None = None, connections: list[Any] | None = None) -> SimpleNamespace:
    """A context carrying procedure maps and named connections."""
    ctx = SimpleNamespace(
        procedure_maps=maps or [],
        connections=connections or [],
        db_connections=connections or [],
    )
    ctx.shared_connections = build_connections(ctx)
    return ctx


def _map(name: str, **extra: Any) -> SimpleNamespace:
    """A procedure map record."""
    return SimpleNamespace(name=name, routine_bindings=[], **extra)


def _routine_map(name: str = "control") -> SimpleNamespace:
    """A map with one callable routine binding."""
    return SimpleNamespace(
        name=name,
        routine_bindings=[SimpleNamespace(
            name="start_batch",
            routine="control.mapped_function",
            result_mode="scalar_result",
            inputs={"p_batch_name": "batch_name"},
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
        )],
        sql_bindings=None,
    )


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

    def test_a_map_declares_no_connection_selection_at_all(self) -> None:
        """Nothing on a resolved map names, ranks or defaults a connection."""
        resolved = get_procedure_map(_ctx(maps=[_routine_map()]), "control")

        for field in ("connection", "connection_name", "connections",
                      "database", "provider", "host"):
            assert getattr(resolved, field, None) is None, field


class TestTheCallerTakesTheConnection:
    """Selection is by name, against the shared objects, never through a map."""

    def test_a_named_connection_resolves_to_its_shared_object(self) -> None:
        ctx = _ctx(connections=[_connection("control"), _connection("rey_loader")])

        assert shared_connection(ctx, "control") is ctx.shared_connections["control"]
        assert shared_connection(ctx, "control").name == "control"

    def test_no_connection_name_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no connection name"):
            shared_connection(_ctx(connections=[_connection("control")]), "")

    def test_an_unknown_connection_is_refused(self) -> None:
        ctx = _ctx(connections=[_connection("control")])

        with pytest.raises(ConfigError, match="not configured"):
            shared_connection(ctx, "nope")

    def test_missing_connections_config_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="not built on this context"):
            shared_connection(SimpleNamespace(), "control")

    def test_configured_names_are_unique(self) -> None:
        # One name must identify one database, or a caller naming it cannot
        # know which one it reached.
        with pytest.raises(ConfigError, match="more than once"):
            build_connections(SimpleNamespace(
                connections=[_connection("control"), _connection("control")]))


class TestOneMapRunsAgainstTwoConnections:
    """The point of the separation, stated as execution rather than lookup."""

    def test_the_same_map_executes_against_two_different_connections(
            self, tmp_path) -> None:
        ctx = _ctx(
            maps=[_routine_map()],
            connections=[_connection("control"), _connection("control_test")],
        )
        live = shared_connection(ctx, "control")
        test = shared_connection(ctx, "control_test")
        assert live is not test

        seen: list[tuple[str, Any]] = []

        def _execute_function(conn: Any, routine: str, named_params: Any) -> int:
            seen.append((routine, conn))
            return 1

        # Two adapters, two roles: Connection opens through its own, the
        # mapped executor runs the routine through the procedure map's.
        with patch.object(connection_module, "_db") as opener, \
             patch("rey_lib.db.procedure_map._db") as executor:
            opener.get_connection.side_effect = lambda cfg, ctx=None: f"handle:{cfg.name}"
            executor.execute_function.side_effect = _execute_function

            for connection in (live, test):
                execute_mapped_routine(
                    ctx=ctx, run_log=make_run_log(tmp_path),
                    conn=connection.handle(), procedure_map="control",
                    routine_name="start_batch", values={"batch_name": "B"},
                    run_ctx=SimpleNamespace(), map_cfg=ctx.procedure_maps[0],
                )

        # One routine contract, two databases, no duplicated bindings.
        assert [routine for routine, _ in seen] == [
            "control.mapped_function", "control.mapped_function"]
        assert [conn for _, conn in seen] == ["handle:control", "handle:control_test"]

    def test_the_map_is_unchanged_by_which_connection_ran_it(self) -> None:
        ctx = _ctx(maps=[_routine_map()],
                   connections=[_connection("control"), _connection("control_test")])
        resolved = get_procedure_map(ctx, "control")

        assert getattr(resolved, "connection_name", None) is None
        assert resolved.routine_bindings[0].routine == "control.mapped_function"
