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

from rey_lib.config.config_namespace import Namespace
from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.workflow import StepResult, WorkflowError, run_workflow


def published(*operations: ApplicationOperation) -> SimpleNamespace:
    """A context whose application publishes these operations."""
    return SimpleNamespace(
        applications=(
            Application(name="loader", workflow_operations=tuple(operations)),
        )
    )


def contract(properties: dict[str, Any], required: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """A closed JSON Schema 2020-12 contract of these settings."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": list(required or ()),
        "additionalProperties": False,
        **extra,
    }


def operation(name: str, **settings: bool) -> ApplicationOperation:
    """One published operation whose settings are name -> required, any value."""
    return ApplicationOperation(
        name=name,
        contract_definition=contract(
            {setting: {} for setting in settings},
            [setting for setting, required in settings.items() if required],
        ),
        contract_sample={},
    )


def operation_with(name: str, definition: dict[str, Any]) -> ApplicationOperation:
    """One published operation with this contract_definition."""
    return ApplicationOperation(name=name, contract_definition=definition, contract_sample={})


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
    """An implementation never receives settings its contract refuses (backlog 939)."""

    def test_an_unsupported_key_is_tolerated_and_named(self, run_log, caplog) -> None:
        """A key the contract does not define is not refused, but it is said.

        Unsupported settings are not part of the canonical contract and are never
        generated from it; existing configuration may still carry them (user,
        2026-10-13), so the run goes on and the warning names the key.
        """
        calls: list[Any] = []
        run = run_workflow(
            published(operation("load", source=False)),
            run_log,
            workflow({"ingest": {"implementation": "load", "typo": 1}},
                     [{"id": "s", "process": "ingest"}]),
            catalog(calls),
        )

        assert run.status == "success"
        assert len(calls) == 1
        assert "'typo' was unexpected" in caplog.text

    def test_a_missing_required_setting_is_refused(self, run_log) -> None:
        calls: list[Any] = []
        with pytest.raises(WorkflowError, match="'source' is a required property"):
            run_workflow(
                published(operation("load", source=True)),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog(calls),
            )

        assert calls == []

    def test_a_wrong_type_is_refused_with_its_path(self, run_log) -> None:
        """The refusal names the exact path, which is what a reader has to fix."""
        calls: list[Any] = []
        definition = contract({"target": contract({"connection": {"type": "string"}}, ["connection"])})
        with pytest.raises(WorkflowError, match=r"target\.connection: 5 is not of type 'string'"):
            run_workflow(
                published(operation_with("load", definition)),
                run_log,
                workflow({"ingest": {"implementation": "load", "target": {"connection": 5}}},
                         [{"id": "s", "process": "ingest"}]),
                catalog(calls),
            )

        assert calls == []

    def test_a_value_outside_the_allowed_set_is_refused(self, run_log) -> None:
        definition = contract({"output": {"type": "string", "enum": ["clear", "redacted"]}})
        with pytest.raises(WorkflowError, match="'opaque' is not one of"):
            run_workflow(
                published(operation_with("load", definition)),
                run_log,
                workflow({"ingest": {"implementation": "load", "output": "opaque"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

    def test_settings_matching_no_variant_are_refused(self, run_log) -> None:
        """A dispatcher's operation value chooses its variant."""
        definition = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"operation": {"enum": ["move", "delete"]}},
            "required": ["operation"],
            "oneOf": [
                {"properties": {"operation": {"const": "move"}, "to": {"type": "string"}},
                 "required": ["operation", "to"], "additionalProperties": False},
                {"properties": {"operation": {"const": "delete"}},
                 "required": ["operation"], "additionalProperties": False},
            ],
        }
        with pytest.raises(WorkflowError, match="is not valid under any of the given schemas"):
            run_workflow(
                published(operation_with("load", definition)),
                run_log,
                workflow({"ingest": {"implementation": "load", "operation": "move"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

    def test_a_nested_setting_is_reached_through_its_mapping(self, run_log) -> None:
        """target.connection is one setting whose value sits in a mapping."""
        calls: list[Any] = []
        definition = contract({"target": contract({"connection": {"type": "string"}}, ["connection"])}, ["target"])
        run = run_workflow(
            published(operation_with("load", definition)),
            run_log,
            workflow({"ingest": {"implementation": "load", "target": {"connection": "rey_apps"}}},
                     [{"id": "s", "process": "ingest"}]),
            catalog(calls),
        )

        assert run.status == "success"
        assert calls[0][1]["target"]["connection"] == "rey_apps"

    def test_an_open_block_accepts_its_subtree(self, run_log) -> None:
        """A contract that declares a block without its interior accepts the interior."""
        calls: list[Any] = []
        run = run_workflow(
            published(operation("load", file_selection=True)),
            run_log,
            workflow({"ingest": {"implementation": "load",
                                 "file_selection": {"operation": "get_files", "source_field": "path"}}},
                     [{"id": "s", "process": "ingest"}]),
            catalog(calls),
        )

        assert run.status == "success"
        assert calls[0][1]["file_selection"]["operation"] == "get_files"

    def test_a_resolved_configuration_is_validated_as_plain_data(self, run_log) -> None:
        """A loaded configuration nests Namespace objects, not dicts.

        The settings go through the one namespace-to-plain-data conversion first,
        so a Namespace block is validated as the mapping it is.
        """
        definition = contract({"target": contract({"connection": {"type": "string"}}, ["connection"])}, ["target"])
        run = run_workflow(
            published(operation_with("load", definition)),
            run_log,
            workflow({"ingest": {"implementation": "load", "target": Namespace({"connection": "rey_apps"})}},
                     [{"id": "s", "process": "ingest"}]),
            catalog([]),
        )

        assert run.status == "success"

    def test_a_nested_namespace_still_refuses_a_violation(self, run_log) -> None:
        """Reaching the leaves must not become accepting everything."""
        definition = contract({"target": contract({"connection": {"type": "string"}}, ["connection"])}, ["target"])
        with pytest.raises(WorkflowError, match="'connection' is a required property"):
            run_workflow(
                published(operation_with("load", definition)),
                run_log,
                workflow({"ingest": {"implementation": "load", "target": Namespace({"typo": "rey_apps"})}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

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


class TestTheRuntimeInvariant:
    """Any path that runs a workflow must carry the canonical applications.

    Capability is resolved from the owning Application. There is no fallback
    registry and nothing is recovered by inspecting Python, so a context without
    them cannot run a workflow at all.
    """

    def test_a_context_without_applications_fails_at_the_boundary(
        self, run_log
    ) -> None:
        """Named for what is wrong, before the first step is considered."""
        with pytest.raises(WorkflowError, match="ctx.applications is missing"):
            run_workflow(
                SimpleNamespace(),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

    def test_it_is_not_reported_as_an_unpublished_operation(
        self, run_log
    ) -> None:
        """The two failures are different faults and must read differently.

        A missing context is a boundary failure. Reported as an application
        publishing nothing, it would send a reader to the workflow declaration,
        which is correct.
        """
        with pytest.raises(WorkflowError) as raised:
            run_workflow(
                SimpleNamespace(),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

        assert "does not publish" not in str(raised.value)

    def test_an_empty_applications_collection_is_not_a_missing_one(
        self, run_log
    ) -> None:
        """An installation with no applications is a state, not a fault."""
        with pytest.raises(WorkflowError, match="does not publish"):
            run_workflow(
                SimpleNamespace(applications=()),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog([]),
            )

    def test_no_step_runs_when_the_context_is_missing(self, run_log) -> None:
        calls: list[Any] = []
        with pytest.raises(WorkflowError):
            run_workflow(
                SimpleNamespace(),
                run_log,
                workflow({"ingest": {"implementation": "load"}},
                         [{"id": "s", "process": "ingest"}]),
                catalog(calls),
            )

        assert calls == []
