"""A Workflow acquired by its id, and the snapshot it shows (backlog 970 follow-on).

The Console's Workflow panel opens with the workflow's control.workflow id and
holds the canonical Workflow for as long as it is open. ``from_governed`` takes
that id through the installation-wide governed read; ``projection`` answers from
the hydrated graph and reads nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import ConfigError
from rey_lib.workflow import workflow as workflow_module
from rey_lib.workflow.workflow import Workflow
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep


class _Control:
    """Answers the governed read and counts it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.calls = rows, []

    def workflow(self, installation: str, workflow_name: str | None = None) -> list[dict[str, Any]]:
        self.calls.append((installation, workflow_name))
        return [row for row in self.rows
                if workflow_name is None or row["workflow_name"] == workflow_name]


def _acquire(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]],
             *args: Any, **kwargs: Any) -> tuple[list[Any], _Control]:
    """Call from_governed with the read and the hydration stubbed; return what hydrated."""
    control = _Control(rows)
    hydrated: list[Any] = []
    monkeypatch.setattr(workflow_module, "open_shared_control",
                        lambda ctx: SimpleNamespace(shared_control=control))
    monkeypatch.setattr(Workflow, "hydrate", classmethod(
        lambda cls, given, **_: hydrated.append(list(given)) or "hydrated"))
    ctx = SimpleNamespace(installation=SimpleNamespace(name="admin"), applications=())
    assert Workflow.from_governed(ctx, *args, **kwargs) == "hydrated"
    return hydrated, control


ROWS = [
    {"workflow_id": 15, "workflow_name": "code_index", "workflow_step_name": "a"},
    {"workflow_id": 15, "workflow_name": "code_index", "workflow_step_name": "b"},
    {"workflow_id": 14, "workflow_name": "other", "workflow_step_name": "c"},
]


def test_by_id_hydrates_that_workflows_rows_from_the_installation_wide_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hydrated, control = _acquire(monkeypatch, ROWS, workflow_id=15)

    assert control.calls == [("admin", None)]
    assert [row["workflow_step_name"] for row in hydrated[0]] == ["a", "b"]


def test_an_id_as_text_names_the_same_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    hydrated, _ = _acquire(monkeypatch, ROWS, workflow_id="15")

    assert len(hydrated[0]) == 2


def test_by_name_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    hydrated, control = _acquire(monkeypatch, ROWS, "code_index")

    assert control.calls == [("admin", "code_index")]
    assert len(hydrated[0]) == 2


def test_an_unknown_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="has no workflow id 99"):
        _acquire(monkeypatch, ROWS, workflow_id=99)


@pytest.mark.parametrize("args, kwargs", [((), {}), (("code_index",), {"workflow_id": 15})],
                         ids=["neither", "both"])
def test_exactly_one_identity_is_required(args: tuple, kwargs: dict) -> None:
    with pytest.raises(ConfigError, match="exactly one"):
        Workflow.from_governed(SimpleNamespace(), *args, **kwargs)


def _workflow() -> Workflow:
    """A hydrated-shaped graph: two steps of one process."""
    application = Application(name="app", icon="app.app", workflow_operations=(
        ApplicationOperation(name="load", contract_definition={}),
        ApplicationOperation(name="check", contract_definition={}),
    ))
    steps = [
        WorkflowStep(id="s1", process_name="p", label="One", ordinal=1, application=application,
                     operation_name="load", approved={"load": lambda *_: None}, settings={},
                     apply_only=True),
        WorkflowStep(id="s2", process_name="p", label="Two", ordinal=2, application=application,
                     operation_name="check", approved={"check": lambda *_: None}, settings={}),
    ]
    process = WorkflowProcess(name="p", ordinal=1, application=application, config=None,
                              steps=steps)
    return Workflow(name="code_index", enabled=True, config=None,
                    processes=[process], control=object())


def test_the_projection_is_the_hydrated_hierarchy_and_reads_nothing() -> None:
    projection = _workflow().projection(object())
    actions = projection.pop("actions")
    assert set(actions) == {"workflow", "process:1", "step:1", "step:2"}
    assert projection == {
        "name": "code_index",
        "enabled": True,
        "state": "IDLE",
        "released": None,
        "processes": [
            {
                "ordinal": 1,
                "name": "p",
                "application": "app",
                "application_icon": "app.app",
                "steps": [
                    {"ordinal": 1, "id": "s1", "operation": "load",
                     "apply_only": True, "always_run": False,
                     "state": "PENDING", "run_id": None},
                    {"ordinal": 2, "id": "s2", "operation": "check",
                     "apply_only": False, "always_run": False,
                     "state": "PENDING", "run_id": None},
                ],
            },
        ],
    }


def test_the_projection_is_a_copy_not_a_path_into_the_workflow() -> None:
    workflow = _workflow()
    projection = workflow.projection(object())
    projection["processes"][0]["steps"].clear()
    projection["processes"].clear()

    assert len(workflow.projection(object())["processes"][0]["steps"]) == 2


def test_one_act_at_a_time() -> None:
    workflow = _workflow()

    with workflow.act():
        # Re-entrant, as the transport takes the projection inside the act.
        assert workflow.projection(object())["name"] == "code_index"


def test_workflow_supplies_existing_commands_for_every_display_subject() -> None:
    actions = _workflow().projection(object())["actions"]
    assert actions["workflow"] == [{"id": "workflow_run", "command": {"command": "direct"}}]
    for subject, name in [("process:1", "p"), ("step:1", "s1"), ("step:2", "s2")]:
        assert actions[subject] == [
            {"id": "workflow_run", "command": {"command": "direct"}},
            {"id": "workflow_run_to", "command": {"command": "direct", "to_step": name}},
            {"id": "workflow_run_from", "command": {"command": "direct", "from_step": name}},
            {"id": "workflow_run_only", "command": {"command": "direct", "step": name}},
        ]
