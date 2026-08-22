"""Control is the control database, and the only way to reach it.

The procedural surface it replaces was a module of functions taking ``ctx``,
with the runtime batch state living on ``ctx`` beside everything else. Two
things were wrong with that. The procedure map stayed on the context, so
anything holding a context could reach control routines without going through
the one module that was supposed to own them; and ``batch_id`` was an ordinary
context attribute, indistinguishable from configuration.

Control takes the map off the context at construction. That is the cut: after
it, the map exists in exactly one place, and reaching control means holding
this object.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import rey_lib.control as control_package
from rey_lib.control import Control
from rey_lib.errors.error_utils import ConfigError


def _map(name: str = "control", sql_bindings: Any = None) -> SimpleNamespace:
    """A resolved procedure map, shaped as the config loader yields it."""
    return SimpleNamespace(
        name=name,
        routine_bindings=[SimpleNamespace(
            name="start_batch", routine="control.mapped_function",
            result_mode="scalar_result",
            inputs={"p_batch_name": "batch_name"},
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
        )],
        sql_bindings=sql_bindings,
    )


def _ctx(**extra: Any) -> SimpleNamespace:
    """A context carrying the control map plus one unrelated map."""
    return SimpleNamespace(
        run_id="R1",
        app_name="rey_loader",
        control=SimpleNamespace(procedure_map="control", enabled=True),
        logging=SimpleNamespace(db_connection="control"),
        procedure_maps=[_map(), _map("rey_loader")],
        db_connections=[SimpleNamespace(name="control", provider="postgres",
                                        database="rey_apps")],
        **extra,
    )


class TestMapOwnership:
    """Construction moves the map; it does not copy it."""

    def test_control_retains_the_resolved_map(self) -> None:
        ctx = _ctx()
        original = ctx.procedure_maps[0]

        control = Control(ctx)

        assert control.procedure_map is original
        assert control.procedure_map_name == "control"

    def test_the_control_map_is_removed_from_ctx(self) -> None:
        ctx = _ctx()

        Control(ctx)

        assert [m.name for m in ctx.procedure_maps] == ["rey_loader"]

    def test_only_the_control_map_is_taken(self) -> None:
        # Other maps have their own owners and are none of Control's business.
        ctx = _ctx()

        Control(ctx)

        assert any(m.name == "rey_loader" for m in ctx.procedure_maps)

    def test_the_map_cannot_be_taken_twice(self) -> None:
        """Proof the removal is real: a second Control finds nothing to take."""
        ctx = _ctx()
        Control(ctx)

        with pytest.raises(ConfigError, match="not found"):
            Control(ctx)

    def test_an_unnamed_control_map_is_refused(self) -> None:
        ctx = _ctx()
        ctx.control = SimpleNamespace(enabled=True)

        with pytest.raises(ConfigError, match="procedure_map is not set"):
            Control(ctx)

    def test_a_map_with_sql_bindings_is_refused(self) -> None:
        ctx = _ctx()
        ctx.procedure_maps = [_map(sql_bindings=[SimpleNamespace(name="sneak_in")])]

        with pytest.raises(ConfigError, match="stored routines only"):
            Control(ctx)


class TestBatchStateLivesOnControl:
    """Runtime state moved off the context."""

    def test_batch_ids_are_control_attributes(self) -> None:
        control = Control(_ctx())

        assert control.batch_step_id is None
        control.batch_id = 5
        control.batch_step_id = 50
        assert (control.batch_id, control.batch_step_id) == (5, 50)

    def test_a_routine_result_is_placed_on_control_by_the_map(self) -> None:
        """load_to_ctx targets Control, because Control is the binding target."""
        ctx = _ctx()
        control = Control(ctx)
        captured: dict[str, Any] = {}

        def _execute(**kwargs: Any) -> dict:
            captured.update(kwargs)
            # Emulate _apply_output against whatever run_ctx was supplied.
            setattr(kwargs["run_ctx"], "batch_id", 7)
            return {"outputs": {"batch_id": 7}}

        with patch("rey_lib.control.control.execute_mapped_routine", _execute), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)):
            control.start_batch(batch_name="nightly")

        assert captured["run_ctx"] is control
        assert control.batch_id == 7
        assert not hasattr(ctx, "batch_id")

    def test_control_supplies_its_own_map_to_the_executor(self) -> None:
        # The map is no longer on ctx, so a lookup there would fail; Control
        # hands over the one it owns.
        ctx = _ctx()
        control = Control(ctx)
        captured: dict[str, Any] = {}

        def _execute(**kwargs: Any) -> dict:
            captured.update(kwargs)
            return {"outputs": {}}

        with patch("rey_lib.control.control.execute_mapped_routine", _execute), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)):
            control.start_batch(batch_name="nightly")

        assert captured["map_cfg"] is control.procedure_map
        assert captured["ctx"] is ctx  # logging evidence still uses the real ctx


class TestControlDoesNotOwnIdentity:
    """run_id is read from the context, never held or created here."""

    def test_run_id_is_read_from_the_context(self) -> None:
        control = Control(_ctx())

        assert control.run_id() == "R1"

    def test_a_missing_run_id_is_refused_not_minted(self) -> None:
        ctx = _ctx()
        del ctx.run_id
        control = Control(ctx)

        with pytest.raises(ConfigError, match="no run identity"):
            control.run_id()


class TestUnheldValuesFallThrough:
    """A binding may name any Rey value; Control holds only the batch state."""

    def test_an_unheld_attribute_resolves_on_the_context(self) -> None:
        ctx = _ctx(pipeline_name="daily")
        control = Control(ctx)

        assert control.pipeline_name == "daily"

    def test_batch_state_is_not_shadowed_by_the_context(self) -> None:
        ctx = _ctx(batch_step_id=999)
        control = Control(ctx)

        # Control answers for its own state rather than deferring.
        assert control.batch_step_id is None


class TestNoProceduralSurfaceRemains:
    """The clean cut, asserted rather than assumed."""

    def test_the_control_utils_module_is_gone(self) -> None:
        with pytest.raises(ImportError):
            __import__("rey_lib.control.control_utils")

    def test_the_package_exports_only_control(self) -> None:
        assert control_package.__all__ == ["Control"]

    def test_no_module_level_control_operations_survive(self) -> None:
        # A procedural wrapper reintroduced beside Control is the defect this
        # slice removes, so the absence is asserted at the package surface.
        operations = {
            "start_batch", "end_batch", "start_step", "end_step", "log_event",
            "save_config_snapshot", "get_or_create_artifact",
            "register_artifact_version", "register_batch_artifact",
            "get_or_create_contract", "register_contract_version",
            "start_contract_run", "end_contract_run", "save_contract_review",
            "run_logged_sql", "ensure_run_id", "ensure_run_timestamp",
        }
        surface = {name for name, value in vars(control_package).items()
                   if inspect.isfunction(value)}

        assert surface & operations == set()

    def test_every_operation_is_a_control_method(self) -> None:
        for name in ("start_batch", "end_batch", "start_step", "end_step",
                     "log_event", "save_config_snapshot", "get_or_create_artifact",
                     "register_artifact_version", "register_batch_artifact",
                     "get_or_create_contract", "register_contract_version",
                     "start_contract_run", "end_contract_run",
                     "save_contract_review", "run_logged_sql"):
            assert callable(getattr(Control, name)), name
            # ctx is not a parameter: the object already holds it.
            assert "ctx" not in inspect.signature(getattr(Control, name)).parameters


class TestControlEnabledDoesNotVetoRunLogging:
    """One switch decides where run logs go, and it is not this one.

    control.enabled governs the optional capabilities -- artifacts, contracts,
    config snapshots, run_logged_sql. logging.run_store is authoritative for
    run-log persistence. Two switches able to disagree about whether the
    database is written is the split this separation removes, so a required
    call proceeds regardless of the flag.
    """

    def test_a_required_call_proceeds_with_control_disabled(self) -> None:
        ctx = _ctx()
        ctx.control = SimpleNamespace(procedure_map="control", enabled=False)
        control = Control(ctx)
        reached: list[str] = []

        with patch("rey_lib.control.control.execute_mapped_routine",
                   side_effect=lambda **kw: reached.append(kw["routine_name"])
                   or {"outputs": {"batch_id": 1}}), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)):
            control.start_batch(batch_name="nightly", required=True)

        assert reached == ["start_batch"]

    def test_an_optional_call_still_respects_control_disabled(self) -> None:
        """The flag keeps its meaning for everything it does govern."""
        ctx = _ctx()
        ctx.control = SimpleNamespace(procedure_map="control", enabled=False)
        control = Control(ctx)
        reached: list[str] = []

        with patch("rey_lib.control.control.execute_mapped_routine",
                   side_effect=lambda **kw: reached.append(kw["routine_name"])):
            control.get_or_create_artifact(artifact_type="report",
                                           artifact_name="summary")

        assert reached == []
