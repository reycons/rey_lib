"""A batch groups runs; it is not one of them.

The control schema encoded the opposite. ``batch.run_id`` was ``NOT NULL`` with
a unique index over it, so the database enforced one batch per run -- the
assumption stated as a constraint. Steps and events carried no run identity at
all, because under that model the batch already implied it.

Once a batch contains several runs, that implication is gone: two runs' steps
in one batch become indistinguishable. So execution identity moves down to the
rows that record execution, and the batch keeps only the grouping.

    batch B1
    |-- run R100 -> steps, events
    |-- run R101 -> steps, events
    +-- run R102 -> steps, events

These tests assert what this repository controls -- the arguments Rey sends.
The column and routine changes live in database_ddl, and the map binding is
checked against those signatures separately.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.control import control_utils


@pytest.fixture()
def sent() -> Any:
    """Capture the values dict each control call would send."""
    calls: list[tuple[str, dict]] = []

    def _fake(ctx: Any, action_name: str, variables: dict,
              required: bool = False) -> None:
        calls.append((action_name, variables))
        return None

    with patch.object(control_utils, "_call", _fake):
        yield calls


def _ctx() -> SimpleNamespace:
    """A context mid-execution: identity established, batch already bound."""
    return SimpleNamespace(
        run_id="R100",
        app_name="rey_loader",
        batch_id=1,
        batch_step_id=None,
    )


class TestTheBatchCarriesNoExecutionIdentity:
    """What start_batch must no longer send."""

    def test_starting_a_batch_sends_no_run_id(self, sent) -> None:
        control_utils.start_batch(_ctx(), batch_name="nightly")

        _, values = sent[0]
        assert "run_id" not in values

    def test_starting_a_batch_sends_no_pipeline_name(self, sent) -> None:
        # A pipeline is one kind of thing a batch may group, not a property of
        # grouping itself.
        control_utils.start_batch(_ctx(), batch_name="nightly")

        _, values = sent[0]
        assert "pipeline_name" not in values


class TestExecutionIdentityLivesOnTheRows:
    """What step and event persistence must now send."""

    def test_a_step_says_which_run_produced_it(self, sent) -> None:
        control_utils.start_step(_ctx(), step_name="extract", step_sequence=1)

        _, values = sent[0]
        assert values["run_id"] == "R100"
        assert values["batch_id"] == 1

    def test_an_event_says_which_run_produced_it(self, sent) -> None:
        control_utils.log_event(_ctx(), severity="INFO", event_name="started",
                                message="run begins")

        _, values = sent[0]
        assert values["run_id"] == "R100"

    def test_a_run_level_event_still_belongs_to_a_batch(self, sent) -> None:
        """batch_step_id is optional; batch_id is not.

        A persisted event belongs to a batch even when it belongs to no step,
        so a run-level event carries batch_id and run_id with batch_step_id
        left null -- rather than the batch link being relaxed.
        """
        control_utils.log_event(_ctx(), severity="INFO", event_name="started",
                                message="run begins")

        _, values = sent[0]
        assert values["batch_step_id"] is None
        assert values["batch_id"] == 1
        assert values["run_id"] == "R100"

    def test_two_runs_in_one_batch_stay_distinguishable(self, sent) -> None:
        """The whole point of the move: one batch, two runs, steps still attributable."""
        first, second = _ctx(), _ctx()
        second.run_id = "R101"

        control_utils.start_step(first, step_name="extract", step_sequence=1)
        control_utils.start_step(second, step_name="extract", step_sequence=1)

        batches = {values["batch_id"] for _, values in sent}
        runs = [values["run_id"] for _, values in sent]
        assert batches == {1}
        assert runs == ["R100", "R101"]
