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
from rey_lib.errors.error_utils import ConfigError, StateError


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


#: What ``p_batch_start`` hands back, as the map's dataset_result yields it.
_BATCH_ROWS = [{"o_batch_id": 1, "o_batch_step_id": 10}]


def _built(ctx: Any) -> Control:
    """Construct a Control with its batch start stubbed.

    Construction starts the batch, so every test that is about something else
    would otherwise have to reach a database to get an object at all. The stub
    returns what ``p_batch_start`` returns; ``TestConstructionStartsTheBatch``
    is the one place that exercises the real path.

    It is given a recording run log because destruction closes the batch, and
    a stub batch cannot be closed against a connection that was never opened.
    ``_record_close_failure`` writes that to the run log when there is one and
    to the module logger when there is not, so without this every test using
    the helper prints a real teardown error it is not about.
    """
    with patch.object(Control, "_call_rows", return_value=list(_BATCH_ROWS)):
        control = Control(ctx)
    control.run_log = SimpleNamespace(append=lambda *args, **kwargs: None)
    return control


def _ctx(**extra: Any) -> SimpleNamespace:
    """A context carrying the control map plus one unrelated map."""
    return SimpleNamespace(
        run_id="R1",
        app_name="rey_loader",
        control=SimpleNamespace(procedure_map="control", connection="control",
                                enabled=True),
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

        control = _built(ctx)

        assert control.procedure_map is original
        assert control.procedure_map_name == "control"

    def test_the_control_map_is_removed_from_ctx(self) -> None:
        ctx = _ctx()

        _built(ctx)

        assert [m.name for m in ctx.procedure_maps] == ["rey_loader"]

    def test_only_the_control_map_is_taken(self) -> None:
        # Other maps have their own owners and are none of Control's business.
        ctx = _ctx()

        _built(ctx)

        assert any(m.name == "rey_loader" for m in ctx.procedure_maps)

    def test_the_map_cannot_be_taken_twice(self) -> None:
        """Proof the removal is real: a second Control finds nothing to take."""
        ctx = _ctx()
        _built(ctx)

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
        control = _built(_ctx())

        control.batch_id = 5
        control.batch_step_id = 50
        assert (control.batch_id, control.batch_step_id) == (5, 50)
        assert not hasattr(_ctx(), "batch_step_id")

    def test_a_batch_id_on_the_context_is_not_adopted(self) -> None:
        """A Control starts its own batch; it never continues someone else's.

        Adopting one was how a Control came to hold a batch_id with no root
        step of its own, which is the state a workflow step cannot hang from.
        Parent and child are related through parent_batch_step_id.
        """
        ctx = _ctx()
        ctx.batch_id = 99
        control = _built(ctx)

        assert control.batch_id == 1            # its own, from p_batch_start
        assert control.owns_batch is True


class TestDestructionClosesTheBatchItOwns:
    """Control.close is the shutdown path, and the only one.

    finish_batch has exactly one caller. Nothing in the run boundary ends a
    batch; it sets the outcome and lets collection do the rest.
    """

    @staticmethod
    def _owning(**outcome: Any) -> Any:
        """A Control holding an open batch it started, with calls recorded."""
        control = _built(_ctx())
        control.batch_id = 5
        control.batch_root_step_id = 50
        control.batch_step_id = 50
        control.owns_batch = True
        for key, value in outcome.items():
            setattr(control, key, value)
        control.calls = []
        control._call = lambda name, values, required=False: (
            control.calls.append((name, values.get("status"))))
        return control

    def test_it_closes_the_root_then_the_batch(self) -> None:
        control = self._owning(run_outcome="success")

        control.close()

        assert [name for name, _ in control.calls] == ["end_step", "end_batch"]
        assert all(status == "success" for _, status in control.calls)
        assert control.batch_id is None
        assert control.owns_batch is False

    def test_an_open_child_step_is_closed_before_the_root(self) -> None:
        control = self._owning(run_outcome="success")
        control.batch_step_id = 77          # a child, not the root

        control.close()

        # Two end_step calls: the child, then the root. A parent closed first
        # would carry a completed_at earlier than the step beneath it.
        assert [name for name, _ in control.calls] == [
            "end_step", "end_step", "end_batch"]

    def test_no_outcome_closes_as_unknown(self) -> None:
        """Collected without being told, the batch says so rather than guessing."""
        control = self._owning()

        control.close()

        assert {status for _, status in control.calls} == {"unknown"}

    def test_a_batch_it_does_not_own_is_left_alone(self) -> None:
        """A batch may group several runs; one Control being collected ends none."""
        control = self._owning(run_outcome="success")
        control.owns_batch = False

        control.close()

        assert control.calls == []

    def test_a_close_failure_is_recorded_and_teardown_continues(self) -> None:
        """Recorded through the run log, not raised out of destruction."""
        recorded: list[dict] = []

        control = self._owning(run_outcome="success")
        control.run_log = SimpleNamespace(
            append=lambda record_type, **fields: recorded.append(
                {"record_type": record_type, **fields}))

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("control unreachable")

        control._call = _boom

        control.close()          # must not raise

        assert [r["record_type"] for r in recorded] == ["ERROR"]
        assert "could not be closed" in recorded[0]["message"]
        # Teardown still released everything.
        assert control.batch_id is None
        assert control.run_log is None

    def test_closing_twice_is_safe(self) -> None:
        control = self._owning(run_outcome="success")

        control.close()
        calls_after_first = list(control.calls)
        control.close()

        assert control.calls == calls_after_first


class TestObjectDestructionClosesTheBatch:
    """Nothing has to remember. Destroying the object is what closes it.

    close() had one production caller -- app_runtime, through collect_runtime
    -- so the four sites that build a Control outside the launch boundary left
    a batch and its root step RUNNING for ever. __del__ delegates to close()
    rather than duplicating it.

    Timing is ordinary Python object lifetime, not a lifecycle guarantee: a
    hard-killed process runs no destructor at all.
    """

    def test_deleting_an_owning_control_closes_its_batch(self) -> None:
        """Deleted, never close()d -- the child step, the root, then the batch."""
        calls: list[tuple[str, Any]] = []
        control = _built(_ctx())
        control.batch_id = 5
        control.batch_root_step_id = 50
        control.batch_step_id = 77          # a child is still open
        control.owns_batch = True
        control.run_outcome = "success"
        control._call = lambda name, values, required=False: (
            calls.append((name, values.get("status"))))

        del control

        assert [name for name, _ in calls] == ["end_step", "end_step", "end_batch"]
        assert {status for _, status in calls} == {"success"}

    def test_deleting_an_already_closed_control_closes_nothing_twice(self) -> None:
        calls: list[tuple[str, Any]] = []
        control = _built(_ctx())
        control.batch_id = 5
        control.batch_root_step_id = 50
        control.batch_step_id = 50
        control.owns_batch = True
        control.run_outcome = "success"
        control._call = lambda name, values, required=False: (
            calls.append((name, values.get("status"))))

        control.close()
        after_close = list(calls)
        del control

        assert calls == after_close

    def test_a_control_whose_construction_failed_destructs_silently(self) -> None:
        """__init__ raises now, and __del__ still runs on the half-built object.

        ConfigError here, from a context naming no procedure map: the batch
        attributes exist but the connection and map do not. Nothing may escape.
        """
        ctx = _ctx()
        ctx.control = SimpleNamespace(enabled=True)

        with pytest.raises(ConfigError):
            Control(ctx)
        # The failed instance is unreferenced now; its destructor already ran.
        # Reaching here at all is the assertion.

    def test_an_exception_escaping_del_cannot_be_handled_so_it_is_not_raised(
            self) -> None:
        """The whole reason for the BaseException catch, stated as a test.

        There is no caller to handle it: Python prints and discards whatever
        leaves __del__, and at interpreter shutdown it would obscure the error
        that ended the run. close() still records ORDINARY close failures
        through the run log; this is only what that path could not route.
        """
        control = _built(_ctx())
        control.batch_id = 5
        control.owns_batch = True

        def _unclosable() -> None:
            raise RuntimeError("even the run log is gone")

        control.close = _unclosable

        del control     # must not raise, and must not print a handled error


class TestBatchResultPlacement:
    """Kept beside the state tests: the map is what binds a routine's ids."""

    def test_a_routine_result_is_placed_on_control_by_the_map(self) -> None:
        """load_to_ctx targets Control, because Control is the binding target."""
        ctx = _ctx()
        control = _built(ctx)
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
        control = _built(ctx)
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
        control = _built(_ctx())

        assert control.run_id == "R1"

    def test_a_missing_run_id_is_refused_not_minted(self) -> None:
        ctx = _ctx()
        del ctx.run_id
        control = _built(ctx)

        with pytest.raises(ConfigError, match="no run identity"):
            control.run_id


class TestUnheldValuesFallThrough:
    """A binding may name any Rey value; Control holds only the batch state."""

    def test_an_unheld_attribute_resolves_on_the_context(self) -> None:
        ctx = _ctx(pipeline_name="daily")
        control = _built(ctx)

        assert control.pipeline_name == "daily"

    def test_batch_state_is_not_shadowed_by_the_context(self) -> None:
        ctx = _ctx(batch_step_id=999)
        control = _built(ctx)

        # Control answers for its own state rather than deferring: the step it
        # bound at construction, not the one the context happens to carry.
        assert control.batch_step_id == 10


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


class TestConstructionStartsTheBatch:
    """A Control that exists has a batch. The one place this is not stubbed.

    It was a separate call the caller made afterwards, and a workflow step
    reached f_batch_step_start with a null batch_id because that call was
    vetoed by control.enabled and nothing noticed until the insert refused.
    """

    @staticmethod
    def _real(ctx: Any) -> tuple[Control, list[str]]:
        """Construct through the real batch start, recording what it called."""
        reached: list[str] = []

        def _execute(**kwargs: Any) -> dict:
            reached.append(kwargs["routine_name"])
            return {"rows": list(_BATCH_ROWS)}

        with patch("rey_lib.control.control.execute_mapped_routine", _execute), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)):
            return Control(ctx), reached

    def test_the_batch_is_bound_when_construction_returns(self) -> None:
        control, reached = self._real(_ctx())

        assert reached == ["start_batch"]
        assert control.batch_id is not None
        assert control.batch_root_step_id is not None
        assert control.batch_step_id == control.batch_root_step_id

    def test_control_enabled_false_does_not_veto_it(self) -> None:
        """The flag governs the optional capabilities, and the batch is not one.

        This is the regression. `enabled` is false in every installation, and
        the batch start took the optional path, so `_call_rows` returned before
        any SQL was sent and left every id None.
        """
        ctx = _ctx()
        ctx.control = SimpleNamespace(procedure_map="control",
                                      connection="control", enabled=False)
        control, reached = self._real(ctx)

        assert reached == ["start_batch"]
        assert control.batch_id is not None
        assert control.batch_root_step_id is not None
        assert control.batch_step_id == control.batch_root_step_id

    def test_construction_fails_when_the_batch_does_not_start(self) -> None:
        """No Control is handed back without one, rather than one that fails later."""
        with patch("rey_lib.control.control.execute_mapped_routine",
                   return_value={"rows": []}), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)), \
             pytest.raises(StateError, match="the batch was not started"):
            Control(_ctx())

    def test_start_batch_cannot_be_made_optional(self) -> None:
        """No `required` parameter: there is nothing for a caller to decide."""
        assert "required" not in inspect.signature(Control.start_batch).parameters
        assert "required" not in inspect.signature(Control.finish_batch).parameters
        assert "required" not in inspect.signature(Control.end_batch).parameters


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
        control = _built(ctx)
        reached: list[str] = []

        with patch("rey_lib.control.control.execute_mapped_routine",
                   side_effect=lambda **kw: reached.append(kw["routine_name"])
                   or {"outputs": {"batch_id": 1}}), \
             patch.object(Control, "_handle",
                          return_value=SimpleNamespace(close=lambda: None)):
            control.start_batch(batch_name="nightly")

        assert reached == ["start_batch"]

    def test_an_optional_call_still_respects_control_disabled(self) -> None:
        """The flag keeps its meaning for everything it does govern."""
        ctx = _ctx()
        ctx.control = SimpleNamespace(procedure_map="control", enabled=False)
        control = _built(ctx)
        reached: list[str] = []

        with patch("rey_lib.control.control.execute_mapped_routine",
                   side_effect=lambda **kw: reached.append(kw["routine_name"])):
            control.get_or_create_artifact(artifact_type="report",
                                           artifact_name="summary")

        assert reached == []
