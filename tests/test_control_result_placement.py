"""Where a control routine's result lands on ctx, and who decides it.

The procedure map is the installation adapter between Rey's internal
vocabulary and one installation's database contract. Both directions belong to
it: ``input`` binds Rey fields to routine parameters, and ``output.load_to_ctx``
binds the routine's result back to a Rey field. An installation whose routine
returns ``job_group_key`` binds it to ``batch_id`` in YAML, with no Python
change -- which only holds while YAML is the single place that decision is
written.

``control_utils._call`` used to write the same field a second time through a
``set_ctx`` argument. The two agreed, so nothing failed; that is what made it
worth removing rather than leaving. A second writer for one decision is only
invisible until the day the two disagree, and here the field in question is
``batch_id``, which the whole batch-reuse contract turns on.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from rey_lib.control import control_utils
from rey_lib.db.procedure_map import execute_mapped_routine


def _binding(name: str, routine: str, variable: str, load_to_ctx: str) -> dict:
    """One routine binding, shaped as the loader yields it."""
    return {
        "name": name,
        "routine": routine,
        "result_mode": "scalar_result",
        "routine_type": "function",
        "inputs": {"p_run_id": "run_id"},
        "output": {"variable": variable, "load_to_ctx": load_to_ctx},
    }


def _map(binding: dict) -> dict:
    """A procedure map carrying one binding."""
    return {"name": "control", "routine_bindings": [binding]}


class TestTheMapPlacesTheResult:
    """One writer, declared in YAML."""

    def test_the_binding_places_the_result_on_ctx(self) -> None:
        run_ctx = SimpleNamespace()
        mapping = _map(_binding("start_batch", "control.f_start_batch", "batch_id", "batch_id"))

        with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=mapping), \
             patch("rey_lib.db.procedure_map._db") as db:
            db.execute_function.return_value = 77
            execute_mapped_routine(object(), object(), "control", "start_batch",
                                   {"run_id": "R1"}, run_ctx=run_ctx)

        assert run_ctx.batch_id == 77

    def test_an_installation_may_rename_the_routine_and_its_result(self) -> None:
        """The adapter's whole point: a different DB contract, unchanged Rey code."""
        run_ctx = SimpleNamespace()
        mapping = _map(
            _binding("start_batch", "ops.create_job_group", "job_group_key", "batch_id")
        )

        with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=mapping), \
             patch("rey_lib.db.procedure_map._db") as db:
            db.execute_function.return_value = "JG-9"
            execute_mapped_routine(object(), object(), "control", "start_batch",
                                   {"run_id": "R1"}, run_ctx=run_ctx)

        # Rey reads batch_id, whatever the installation's routine called it.
        assert run_ctx.batch_id == "JG-9"


class TestControlDoesNotPlaceResults:
    """The removed second writer, pinned so it cannot come back."""

    def test_the_dispatcher_takes_no_placement_argument(self) -> None:
        parameters = inspect.signature(control_utils._call).parameters

        assert "set_ctx" not in parameters
        # required chooses a failure contract, not where a result lands.
        assert list(parameters) == ["ctx", "action_name", "variables", "required"]

    def test_no_control_routine_asks_the_dispatcher_to_place_a_result(self) -> None:
        # Reading the module's own source: a placement argument reintroduced at
        # any call site is the defect, not only one on _call's signature.
        source = inspect.getsource(control_utils)

        assert "set_ctx" not in source
