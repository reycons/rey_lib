"""A process is a declaration, and the contract it names is published.

The registry it replaces was a dict of process names an application passed the
coordinator, so adding a process meant editing Python. These assert what makes
that unnecessary: membership is declared, the contract is published, and the
callable is reached only through the application's own catalog.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import (
    Application,
    ApplicationCommand,
    ApplicationCommandParameter,
)
from rey_lib.workflow import StepResult, WorkflowError, run_workflow


def published(*operations: ApplicationCommand) -> SimpleNamespace:
    """A context whose application publishes these operations."""
    return SimpleNamespace(
        applications=(
            Application(name="loader", workflow_operations=tuple(operations)),
        )
    )


def operation(name: str, **parameters: bool) -> ApplicationCommand:
    """One published operation whose parameters are name -> required."""
    return ApplicationCommand(
        name=name,
        parameters=tuple(
            ApplicationCommandParameter(name=parameter, required=required)
            for parameter, required in parameters.items()
        ),
    )


def workflow(processes: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "w", "app": "loader", "processes": processes, "steps": steps}


def catalog(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """An implementation catalog recording what each call received."""
    def load(_ctx: Any, _run_log: Any, config: dict[str, Any], _run: Any) -> StepResult:
        calls.append(("load", dict(config)))
        return StepResult("load", "ok", "")
    return {"load": load}


class TestMembershipIsDeclared:
    """Adding a process is configuration."""

    def test_a_declared_process_runs_through_the_catalog(self, run_log) -> None:
        calls: list[Any] = []
        run = run_workflow(
            published(operation("load")),
            run_log,
            workflow({"ingest": {"implementation": "load"}},
                     [{"id": "s", "process": "ingest"}]),
            catalog(calls),
        )

        assert run.status == "success"
        assert [name for name, _config in calls] == ["load"]

    def test_two_processes_may_share_one_implementation(self, run_log) -> None:
        """The proof the process name has stopped being the dispatch key.

        Two declarations, different configuration, one approved callable, and no
        Python changed to add the second.
        """
        calls: list[Any] = []
        run = run_workflow(
            published(operation("load", source=False)),
            run_log,
            workflow(
                {
                    "daily": {"implementation": "load", "source": "a"},
                    "audit": {"implementation": "load", "source": "b"},
                },
                [{"id": "s1", "process": "daily"}, {"id": "s2", "process": "audit"}],
            ),
            catalog(calls),
        )

        assert run.status == "success"
        assert [config["source"] for _name, config in calls] == ["a", "b"]

    def test_a_process_naming_no_implementation_is_refused(self, run_log) -> None:
        with pytest.raises(WorkflowError, match="names no 'implementation'"):
            run_workflow(
                published(operation("load")),
                run_log,
                workflow({"ingest": {}}, [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )


class TestTheContractIsPublished:
    """What an application has not published cannot be invoked."""

    def test_an_unpublished_implementation_is_refused(self, run_log) -> None:
        with pytest.raises(WorkflowError, match="does not publish"):
            run_workflow(
                published(operation("load")),
                run_log,
                workflow({"ingest": {"implementation": "elsewhere"}},
                         [{"id": "s", "process": "ingest"}]),
                {"elsewhere": lambda *_: None},
            )

    def test_a_published_implementation_absent_from_the_catalog_is_refused(
        self, run_log
    ) -> None:
        """Publication is what an author reads; the catalog is what runs."""
        with pytest.raises(WorkflowError, match="approved implementation catalog"):
            run_workflow(
                published(operation("load")),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                {},
            )

    def test_an_application_that_publishes_nothing_approves_nothing(
        self, run_log
    ) -> None:
        with pytest.raises(WorkflowError, match="does not publish"):
            run_workflow(
                published(),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )


class TestValidationPrecedesDispatch:
    """An implementation never receives configuration it did not declare."""

    def test_undeclared_configuration_is_refused(self, run_log) -> None:
        """A typo becoming a setting nobody applied is what this prevents."""
        calls: list[Any] = []
        with pytest.raises(WorkflowError, match="'typo'"):
            run_workflow(
                published(operation("load", source=False)),
                run_log,
                workflow({"ingest": {"implementation": "load", "typo": 1}},
                         [{"id": "s", "process": "ingest"}]),
                catalog(calls),
            )

        assert calls == []

    def test_a_missing_required_parameter_is_refused(self, run_log) -> None:
        calls: list[Any] = []
        with pytest.raises(WorkflowError, match="missing required 'source'"):
            run_workflow(
                published(operation("load", source=True)),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog(calls),
            )

        assert calls == []

    def test_a_nested_setting_is_named_by_its_path(self, run_log) -> None:
        """target.connection is one setting whose value sits in a mapping."""
        calls: list[Any] = []
        run = run_workflow(
            published(ApplicationCommand(
                name="load",
                parameters=(ApplicationCommandParameter(
                    name="target.connection", required=True),),
            )),
            run_log,
            workflow(
                {"ingest": {"implementation": "load",
                            "target": {"connection": "rey_apps"}}},
                [{"id": "s", "process": "ingest"}],
            ),
            catalog(calls),
        )

        assert run.status == "success"
        assert calls[0][1]["target"]["connection"] == "rey_apps"

    def test_engine_vocabulary_is_not_the_applications_to_declare(
        self, run_log
    ) -> None:
        """apply_only says how the engine treats a step, not what to configure."""
        calls: list[Any] = []
        run = run_workflow(
            published(operation("load")),
            run_log,
            workflow({"ingest": {"implementation": "load", "apply_only": True}},
                     [{"id": "s", "process": "ingest"}]),
            catalog(calls),
            apply=True,
        )

        assert run.status == "success"
        assert "apply_only" not in calls[0][1]
        assert "implementation" not in calls[0][1]
