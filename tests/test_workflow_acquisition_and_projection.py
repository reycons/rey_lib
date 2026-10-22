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
    projection.pop("relationships")
    actions = projection.pop("actions")
    assert set(actions) == {"workflow", "process:1", "step:1", "step:2"}
    assert projection == {
        "panel_type": "workflow",
        "name": "code_index",
        "enabled": True,
        "state": "IDLE",
        "released": None,
        "focus": None,
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


def test_relationships_project_hierarchy_and_execution_order() -> None:
    assert _workflow().projection(object())["relationships"] == [
        {"source": "workflow", "target": "process:1", "kind": "contains"},
        {"source": "process:1", "target": "step:1", "kind": "contains"},
        {"source": "process:1", "target": "step:2", "kind": "contains"},
        {"source": "step:1", "target": "step:2", "kind": "precedes"},
    ]


CHILD_ROWS = [
    {"workflow_id": 15, "workflow_name": "code_index", "workflow_process_id": 150,
     "workflow_step_id": 1501, "workflow_step_name": "a"},
    {"workflow_id": 15, "workflow_name": "code_index", "workflow_process_id": 150,
     "workflow_step_id": 1502, "workflow_step_name": "b"},
    {"workflow_id": 14, "workflow_name": "other", "workflow_process_id": 140,
     "workflow_step_id": 1401, "workflow_step_name": "c"},
]


def _acquire_focused(
    monkeypatch: pytest.MonkeyPatch, **kwargs: Any,
) -> tuple[list[Any], list[dict]]:
    """from_governed by a child id: what hydrated, and the focus the Workflow was asked for."""
    control = _Control(CHILD_ROWS)
    hydrated: list[Any] = []
    focused: list[dict] = []
    monkeypatch.setattr(workflow_module, "open_shared_control",
                        lambda ctx: SimpleNamespace(shared_control=control))
    monkeypatch.setattr(Workflow, "hydrate", classmethod(
        lambda cls, given, **_: hydrated.append(list(given)) or SimpleNamespace(
            focus=lambda **child: focused.append(child))))
    ctx = SimpleNamespace(installation=SimpleNamespace(name="admin"), applications=())
    Workflow.from_governed(ctx, **kwargs)
    return hydrated, focused


class TestAProcessOrStepNamesItsWorkflow:
    """The governed read places a child in its workflow; nothing else restates it (1126)."""

    def test_a_process_id_hydrates_the_workflow_holding_it_and_focuses_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hydrated, focused = _acquire_focused(monkeypatch, workflow_process_id=150)

        assert [row["workflow_step_name"] for row in hydrated[0]] == ["a", "b"]
        assert focused == [{"workflow_process_id": 150, "workflow_step_id": None}]

    def test_a_step_id_hydrates_the_workflow_holding_it_and_focuses_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hydrated, focused = _acquire_focused(monkeypatch, workflow_step_id="1401")

        assert [row["workflow_step_name"] for row in hydrated[0]] == ["c"]
        assert focused == [{"workflow_process_id": None, "workflow_step_id": "1401"}]

    def test_an_unknown_child_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="has no workflow step id 9"):
            _acquire_focused(monkeypatch, workflow_step_id=9)

    @pytest.mark.parametrize("kwargs", [
        {"workflow_id": 15, "workflow_process_id": 150},
        {"workflow_process_id": 150, "workflow_step_id": 1501},
    ], ids=["workflow-and-process", "process-and-step"])
    def test_two_identities_are_refused_not_ranked(self, kwargs: dict) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            Workflow.from_governed(SimpleNamespace(), **kwargs)


def _focusable() -> Workflow:
    """The same graph, its process and steps carrying governed ids."""
    workflow = _workflow()
    (process,) = workflow.processes
    process.governed_id = 150
    for governed, step in zip((1501, 1502), process.steps):
        step.governed_id = governed
    return workflow


class TestTheWorkflowIsOpenedAtOneOfItsParts:
    def test_a_focused_step_projects_its_process_and_its_id(self) -> None:
        workflow = _focusable()

        workflow.focus(workflow_step_id=1502)

        assert workflow.projection(None)["focus"] == {"process": 1, "step": "s2"}

    def test_a_focused_process_projects_no_step(self) -> None:
        workflow = _focusable()

        workflow.focus(workflow_process_id="150")

        assert workflow.projection(None)["focus"] == {"process": 1, "step": None}

    def test_a_part_it_does_not_hold_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="holds no step 9"):
            _focusable().focus(workflow_step_id=9)

    def test_focus_names_exactly_one_part(self) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            _focusable().focus(workflow_process_id=150, workflow_step_id=1501)


# -- its definition (#1199 S3): the domain read, independent of presentation --


def test_the_definition_is_what_the_workflow_is_and_where_it_stands() -> None:
    assert _workflow().definition() == {
        "name": "code_index",
        "enabled": True,
        "state": "IDLE",
        "released": None,
        "processes": [
            {
                "ordinal": 1,
                "name": "p",
                "application": "app",
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


def test_the_definition_carries_no_presentation_and_no_configuration() -> None:
    workflow = _focusable()
    workflow.focus(workflow_step_id=workflow.steps["s2"].governed_id)
    definition = workflow.definition()

    assert not {"panel_type", "actions", "relationships", "focus"} & set(definition)
    step = definition["processes"][0]["steps"][0]
    assert not {"settings", "contract", "label", "process_name"} & set(step)


def test_the_definition_is_a_copy_read_under_one_act() -> None:
    workflow = _workflow()
    with workflow.act():
        definition = workflow.definition()
    definition["processes"][0]["steps"].clear()

    assert len(workflow.definition()["processes"][0]["steps"]) == 2
