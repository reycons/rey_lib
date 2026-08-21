"""The control database is written through stored routines and nothing else.

A procedure map may declare two kinds of binding. ``routine_bindings`` name a
stored function or procedure; ``sql_bindings`` carry SQL text that the generic
executor runs directly. The second is a legitimate mechanism for application
databases -- it is how a loader runs its own statements -- and is never
legitimate here, because it would put an INSERT into a control table in a
configuration file, outside the routines that own those tables.

Both installations already declare 24 routine bindings and no SQL bindings on
the control map. That held by convention: nothing refused one. The refusal is
what makes it a property of the system rather than of whoever wrote the YAML.

It is raised rather than routed through ``_mark_unavailable``. Degrading to
"control unavailable" would hide a misconfiguration behind what looks like a
database outage, and the run would continue writing nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.control import control_utils
from rey_lib.errors.error_utils import ConfigError


def _ctx(map_cfg: Any) -> SimpleNamespace:
    """A context with control enabled and one resolvable procedure map."""
    return SimpleNamespace(
        run_id="R1",
        app_name="rey_loader",
        batch_id=None,
        control=SimpleNamespace(procedure_map="control", enabled=True),
        logging=SimpleNamespace(db_connection="control"),
        procedure_maps=[map_cfg],
        db_connections=[SimpleNamespace(name="control", provider="postgres",
                                        database="rey_apps")],
    )


def _routine_map(sql_bindings: Any = None) -> SimpleNamespace:
    """A control map as both installations declare it: routines only.

    A namespace rather than a dict because that is what the config loader
    yields and what find_by_name matches on.
    """
    return SimpleNamespace(
        name="control",
        routine_bindings=[SimpleNamespace(
            name="start_batch",
            routine="control.f_start_batch",
            result_mode="scalar_result",
            inputs={"p_batch_name": "batch_name"},
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
        )],
        sql_bindings=sql_bindings,
    )


def _sql_binding(name: str, sql: str) -> SimpleNamespace:
    """One SQL-text binding, the kind the control map may not carry."""
    return SimpleNamespace(name=name, execution_target="mapped_sql", sql=sql,
                           result_mode="no_return")


class TestASqlBindingOnTheControlMapIsRefused:
    """The statement must live in a routine, not in configuration."""

    def test_a_control_map_with_a_sql_binding_is_a_configuration_error(self) -> None:
        bad = _routine_map(sql_bindings=[_sql_binding(
            "start_batch", "INSERT INTO control.batch (batch_name) VALUES (:batch_name)")])

        with pytest.raises(ConfigError, match="stored routines only"):
            control_utils.start_batch(_ctx(bad), batch_name="nightly")

    def test_the_refusal_names_the_offending_binding(self) -> None:
        bad = _routine_map(sql_bindings=[
            _sql_binding("sneak_in", "DELETE FROM control.batch")])

        with pytest.raises(ConfigError, match="sneak_in"):
            control_utils.start_batch(_ctx(bad), batch_name="nightly")

    def test_it_is_refused_rather_than_degraded_to_unavailable(self) -> None:
        # Marking control unavailable would let the run continue writing
        # nothing, with a misconfiguration reported as an outage.
        bad = _routine_map(sql_bindings=[_sql_binding("x", "DELETE FROM control.batch")])
        ctx = _ctx(bad)

        with pytest.raises(ConfigError):
            control_utils.start_batch(ctx, batch_name="nightly")

        assert getattr(ctx, "control_available", True) is True


class TestARoutineOnlyMapIsAccepted:
    """The refusal must not cost the sanctioned path."""

    def test_a_routine_binding_reaches_the_database(self) -> None:
        ctx = _ctx(_routine_map())

        with patch.object(control_utils, "_open_connection", return_value=SimpleNamespace(
                close=lambda: None)), \
             patch.object(control_utils, "execute_mapped_routine",
                          return_value={"outputs": {"batch_id": 42}}) as call:
            result = control_utils.start_batch(ctx, batch_name="nightly")

        assert result == 42
        assert call.call_args.kwargs["routine_name"] == "start_batch"
