"""One Connection object per configured connection, shared by every consumer.

Before this, each consumer resolved the connection config itself and asked the
adapter for a handle, so two subsystems naming ``control`` opened two
connections to the same database and neither knew about the other. The object
is what makes "the control connection" a thing there is one of.

Identity is the property under test throughout: not that two consumers get
equivalent connections, but that they get *the same object*, so a handle opened
by one is the handle the other uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.control import Control
from rey_lib.db import connection as connection_module
from rey_lib.db.connection import Connection, build_connections
from rey_lib.errors.error_utils import ConfigError


def _config(name: str, provider: str = "postgres") -> SimpleNamespace:
    """One resolved connections[] record."""
    return SimpleNamespace(name=name, provider=provider, database="rey_apps")


def _ctx(**extra: Any) -> SimpleNamespace:
    """A context carrying two configured connections."""
    ctx = SimpleNamespace(
        run_id="R1",
        app_name="rey_loader",
        connections=[_config("control"), _config("rey_loader")],
        db_connections=[_config("control"), _config("rey_loader")],
        control=SimpleNamespace(procedure_map="control", connection="control",
                                enabled=True),
        logging=SimpleNamespace(db_connection="control"),
        procedure_maps=[SimpleNamespace(name="control", routine_bindings=[],
                                        sql_bindings=None)],
        **extra,
    )
    ctx.shared_connections = build_connections(ctx)
    return ctx


class TestOneObjectPerConfiguredConnection:
    """Construction, and what it produces."""

    def test_each_configured_connection_becomes_one_object(self) -> None:
        shared = _ctx().shared_connections

        assert sorted(shared) == ["control", "rey_loader"]
        assert all(isinstance(c, Connection) for c in shared.values())

    def test_different_connections_are_different_objects(self) -> None:
        shared = _ctx().shared_connections

        assert shared["control"] is not shared["rey_loader"]

    def test_a_duplicate_name_is_a_configuration_error(self) -> None:
        # Two records under one name is how a consumer ends up on a different
        # database from the one it asked for.
        ctx = SimpleNamespace(connections=[_config("control"), _config("control")])

        with pytest.raises(ConfigError, match="more than once"):
            build_connections(ctx)

    def test_identity_metadata_is_retained(self) -> None:
        control = _ctx().shared_connections["control"]

        assert control.name == "control"
        assert control.provider == "postgres"


class TestTwoConsumersShareOneObject:
    """The invariant the whole slice exists for."""

    def test_the_same_name_yields_the_same_object(self) -> None:
        shared = _ctx().shared_connections

        assert shared["control"] is shared["control"]

    def test_control_holds_the_shared_object(self) -> None:
        ctx = _ctx()

        control = Control(ctx)

        assert control.connection is ctx.shared_connections["control"]

    def test_control_does_not_open_its_own(self) -> None:
        """Control references; it does not construct or resolve a config."""
        ctx = _ctx()

        with patch.object(connection_module, "_db") as adapter:
            control = Control(ctx)

        assert control.connection is ctx.shared_connections["control"]
        adapter.get_connection.assert_not_called()

    def test_another_consumer_sees_the_handle_control_opened(self) -> None:
        ctx = _ctx()
        control = Control(ctx)

        with patch.object(connection_module, "_db") as adapter:
            adapter.get_connection.return_value = "live-handle"
            opened = control.connection.handle()
            also = ctx.shared_connections["control"].handle()

        assert opened == also == "live-handle"
        assert adapter.get_connection.call_count == 1


class TestLifetime:
    """Lazy open, reuse, idempotent close."""

    def test_nothing_is_opened_at_construction(self) -> None:
        with patch.object(connection_module, "_db") as adapter:
            ctx = _ctx()

        assert adapter.get_connection.call_count == 0
        assert ctx.shared_connections["control"].is_open is False

    def test_the_handle_opens_once_and_is_reused(self) -> None:
        control = _ctx().shared_connections["control"]

        with patch.object(connection_module, "_db") as adapter:
            adapter.get_connection.return_value = "live-handle"
            first, second, third = control.handle(), control.handle(), control.handle()

        assert first is second is third
        assert adapter.get_connection.call_count == 1

    def test_repeated_operations_reuse_one_handle(self) -> None:
        control = _ctx().shared_connections["control"]

        with patch.object(connection_module, "_db") as adapter:
            adapter.get_connection.return_value = "live-handle"
            control.execute("SELECT 1", {}, "scalar_result")
            control.call_routine("control.mapped_function", {},
                                 routine_type="function",
                                 result_mode="scalar_result")
            control.call_routine("control.mapped_procedure", {},
                                 routine_type="procedure",
                                 result_mode="no_return")

        assert adapter.get_connection.call_count == 1

    def test_close_is_idempotent(self) -> None:
        handle = SimpleNamespace(closed=0)
        handle.close = lambda: setattr(handle, "closed", handle.closed + 1)
        shared = _ctx().shared_connections["control"]

        with patch.object(connection_module, "_db") as adapter:
            adapter.get_connection.return_value = handle
            shared.handle()

        shared.close()
        shared.close()
        shared.close()

        assert handle.closed == 1
        assert shared.is_open is False

    def test_closing_an_unopened_connection_does_nothing(self) -> None:
        _ctx().shared_connections["control"].close()  # must not raise

    def test_a_control_call_does_not_close_the_shared_connection(self) -> None:
        """A consumer must not pull the handle from under other holders."""
        ctx = _ctx()
        control = Control(ctx)
        handle = SimpleNamespace(closed=0)
        handle.close = lambda: setattr(handle, "closed", handle.closed + 1)

        with patch.object(connection_module, "_db") as adapter, \
             patch("rey_lib.control.control.execute_mapped_routine",
                   return_value={"outputs": {}}):
            adapter.get_connection.return_value = handle
            control.start_batch(batch_name="nightly")
            control.start_batch(batch_name="nightly again")

        assert handle.closed == 0
        assert ctx.shared_connections["control"].is_open is True
        assert adapter.get_connection.call_count == 1


class TestProviderBehaviourStaysUnderneath:
    """Connection delegates; it does not reimplement a provider."""

    def test_operations_delegate_to_the_adapter(self) -> None:
        shared = _ctx().shared_connections["control"]

        with patch.object(connection_module, "_db") as adapter:
            adapter.get_connection.return_value = "h"
            shared.execute("SELECT 1", {"a": 1}, "scalar_result")
            shared.call_routine("f", {"b": 2},
                                routine_type="function", result_mode="scalar_result")
            shared.call_routine("p", {"c": 3},
                                routine_type="procedure", result_mode="no_return")

        adapter.execute_sql.assert_called_once_with("h", "SELECT 1", {"a": 1},
                                                    "scalar_result")
        # One entry for every routine, whatever its shape. The adapter is given
        # a decided call and never asked which statement form to write.
        assert adapter.call_routine.call_count == 2
        shapes = [c.args[1].shape.value for c in adapter.call_routine.call_args_list]
        assert shapes == ["scalar_function", "procedure"]
        assert [c.args[1].routine for c in adapter.call_routine.call_args_list] == ["f", "p"]

    def test_the_module_imports_no_provider_backend(self) -> None:
        """Dispatch on provider belongs to DBAdapter, and only there.

        Asserted on imports rather than on the source text: the module
        docstring names the providers precisely to say they are not
        reimplemented here, so a text scan reports its own explanation as a
        violation.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path(connection_module.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        backends = ("postgres_utils", "sqlserver_utils", "mysql_utils", "duckdb_utils")
        assert [m for m in imported if m.endswith(backends)] == []
        assert "rey_lib.db.db_adapter" in imported


class TestProcedureMapsStayConnectionAgnostic:
    """Unchanged by this slice, and asserted so it stays that way."""

    def test_a_map_carrying_a_connection_is_still_refused(self) -> None:
        from rey_lib.db.procedure_map import get_procedure_map

        ctx = SimpleNamespace(procedure_maps=[
            SimpleNamespace(name="control", routine_bindings=[],
                            connection_name="control")])

        with pytest.raises(ConfigError, match="carries connection_name"):
            get_procedure_map(ctx, "control")

    def test_control_selects_the_connection_not_the_map(self) -> None:
        ctx = _ctx()
        control = Control(ctx)

        # The name came from logging.db_connection, never from the map.
        assert control.connection.name == "control"
        assert not hasattr(control.procedure_map, "connection_name")
