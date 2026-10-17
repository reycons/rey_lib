"""Tests for the governed workflow read and the public config-resolution access point.

Backlog 963 (R8 of 950). Workflow definitions are read the way HTTP operations
are: control.workflow_flat_vw through control.f_workflow_get, bound as the
procedure-map action ``get_workflow``, reached by one ``Control.workflow`` call.
Their path references are resolved by the existing context-build resolution,
exposed as ``config_paths.resolve_config_paths`` -- nothing is resolved a
second way. Fixture-based; no database and no Console.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.workflow_runs import RecordingControl

from rey_lib.config import config_paths
from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import (
    _apply_path_resolver,
    _build_path_resolver,
    resolve_config_paths,
)
from rey_lib.config.inventory import to_plain_data
from rey_lib.control.control import Control
from rey_lib.errors.error_utils import AppError
from rey_lib.workflow import workflow as workflow_module
from rey_lib.workflow.workflow import Workflow
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep

_PATHS = [
    {"name": "root", "path": "/installations/rey"},
    {"name": "data", "path": "{root}/data"},
    {"name": "run_root", "path": "{data}/runs/{run_id}"},
    {"name": "file_operator_root", "path": "{data}/file_operator"},
]


def _settings() -> dict[str, Any]:
    """Step settings as stored: path references and run-time placeholders."""
    return {
        "output_root": "{root}/db_backups/ddl",
        "sources": [{"source": {"path": "{data}/*/source/inbox"}}],
        "run_workspace": "{run_root}",
        "output": "{file_operator_root}/output/{source_subfolder}",
        "rule_set_name_pattern": "bmo_{group_slug}_structural_redaction",
        "message_template": "Export {engine} DDL: {object_count} objects",
    }


def _control(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any]]]]:
    """A Control whose one routine call is recorded and answers ``rows``."""
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)
    control._call_rows = lambda action, variables, required=False: (  # type: ignore[method-assign]
        calls.append((action, dict(variables))) or rows
    )
    return control, calls


# --- the governed read ---------------------------------------------------------


class TestTheControlRead:
    def test_workflow_dispatches_the_one_binding(self) -> None:
        row = {"workflow_name": "wf", "workflow_step_name": "s1"}
        control, calls = _control([row])

        rows = control.workflow("local", "wf")

        assert calls == [("get_workflow", {"installation": "local", "workflow_name": "wf"})]
        assert rows == [row]

    def test_every_workflow_when_no_name_is_given(self) -> None:
        control, calls = _control([])

        control.workflow("local")

        assert calls == [("get_workflow", {"installation": "local", "workflow_name": None})]

    def test_the_rows_are_returned_as_the_view_shaped_them(self) -> None:
        rows = [{"workflow_name": "wf", "step_settings": {"output_root": "{root}/x"}}]
        control, _ = _control(rows)

        assert control.workflow("local") == rows


# --- the public access point ---------------------------------------------------


class TestTheConfigResolutionAccessPoint:
    def test_it_resolves_exactly_as_the_context_build_does(self) -> None:
        through_access_point = Namespace({"step": _settings()})
        through_context_build = Namespace({"step": _settings()})

        resolve_config_paths(through_access_point, _PATHS)
        _apply_path_resolver(through_context_build, _build_path_resolver(_PATHS, None))

        assert to_plain_data(through_access_point) == to_plain_data(through_context_build)

    def test_path_references_resolve_through_their_chain(self) -> None:
        tree = Namespace({"step": _settings()})

        resolve_config_paths(tree, _PATHS)

        resolved = to_plain_data(tree)["step"]
        assert resolved["output_root"] == "/installations/rey/db_backups/ddl"
        assert resolved["sources"] == [{"source": {"path": "/installations/rey/data/*/source/inbox"}}]
        assert resolved["output"] == "/installations/rey/data/file_operator/output/{source_subfolder}"

    def test_run_time_placeholders_survive(self) -> None:
        tree = Namespace({"step": _settings()})

        resolve_config_paths(tree, _PATHS)

        resolved = to_plain_data(tree)["step"]
        assert resolved["run_workspace"] == "/installations/rey/data/runs/{run_id}"
        assert resolved["rule_set_name_pattern"] == "bmo_{group_slug}_structural_redaction"
        assert resolved["message_template"] == "Export {engine} DDL: {object_count} objects"

    def test_it_returns_the_resolver_it_built(self) -> None:
        resolver = resolve_config_paths(Namespace({}), _PATHS)

        assert resolver.resolve("data") == Path("/installations/rey/data")

    def test_the_access_point_adds_no_resolution_of_its_own(self) -> None:
        """Its body calls the two existing resolvers and nothing else."""
        source = Path(config_paths.__file__).read_text(encoding="utf-8")
        function = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "resolve_config_paths"
        )
        called = sorted(
            node.func.id for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        )

        assert called == ["_apply_path_resolver", "_build_path_resolver"]


# --- the canonical hydration ---------------------------------------------------

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _application(name: str = "fixture_app", *operations: str) -> Application:
    """An application publishing ``operations`` (``load`` and ``commit`` by default)."""
    return Application(name=name, workflow_operations=tuple(
        ApplicationOperation(name=one, contract_definition={"$schema": _SCHEMA, "type": "object"})
        for one in (operations or ("load", "commit"))
    ))


def _handler(*_args: Any) -> None:
    return None


_APPROVED = {"fixture_app": {"load": _handler, "commit": _handler}}


def _row(workflow_id: int, workflow: str, step: str, ordinal: Any, operation: str,
         settings: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
    """One control.workflow_flat_vw row: a step of process ``fixture_app`` of ``workflow``."""
    row: dict[str, Any] = {
        "workflow_id": workflow_id, "workflow_name": workflow, "workflow_enabled": True,
        "workflow_config": {"paths": [dict(one) for one in _PATHS],
                            "execution": {"full": True, "dry_run": True, "steps": {
                                "export": {"apply_only": True, "always_run": False},
                                "commit": {"apply_only": False, "always_run": True},
                                "only": {"apply_only": False, "always_run": False},
                                "s": {"apply_only": False, "always_run": False},
                                "bad": {"apply_only": False, "always_run": False},
                            }}},
        "installation_id": 1, "installation_key": "local",
        "workflow_process_id": workflow_id * 10, "process_name": "fixture_app",
        "process_ordinal": 1, "application_id": 7, "application_name": "fixture_app",
        "process_config": {"app": {"records_path": "{data}/records"}},
        "workflow_step_id": workflow_id * 100 + (ordinal or 0), "workflow_step_name": step,
        "configured_process_name": operation, "step_label": step.title(),
        "step_ordinal": ordinal, "application_operation_id": 70,
        "application_operation_name": operation, "step_settings": settings,
    }
    row.update(overrides)
    return row


def _rows() -> list[dict[str, Any]]:
    """Workflow ``wf``: export then commit."""
    return [
        _row(1, "wf", "export", 1, "load", _settings()),
        _row(1, "wf", "commit", 2, "commit", {"repo_root": "{root}/db_backups/ddl"}),
    ]


def _hydrate(rows: list[dict[str, Any]]) -> tuple[Workflow, RecordingControl]:
    """Hydrate one workflow's ``rows``, with the Control a Run would be created through."""
    control = RecordingControl()
    return (Workflow.hydrate(rows, applications=[_application()], approved=_APPROVED,
                             control=control), control)


class TestTheWorkflowHoldsItsApplicationRun:
    """The creating application passes its Application Run id; the Workflow holds it."""

    def test_hydration_holds_the_run_id_it_is_given(self) -> None:
        workflow = Workflow.hydrate(_rows(), applications=[_application()],
                                    approved=_APPROVED, control=RecordingControl(),
                                    run_id=4711)

        assert workflow.run_id == 4711

    def test_a_workflow_no_application_created_holds_none(self) -> None:
        assert _hydrate(_rows())[0].run_id is None

    def test_from_governed_hands_the_run_id_to_hydration(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handed: dict[str, Any] = {}
        monkeypatch.setattr(workflow_module, "open_shared_control",
                            lambda ctx: SimpleNamespace(shared_control=SimpleNamespace(
                                workflow=lambda installation, name=None: _rows())))
        monkeypatch.setattr(Workflow, "hydrate", classmethod(
            lambda cls, rows, **kwargs: handed.update(kwargs) or "hydrated"))
        ctx = SimpleNamespace(installation=SimpleNamespace(name="admin"), applications=())

        Workflow.from_governed(ctx, "wf", run_id=4711)

        assert handed["run_id"] == 4711


class TestTheCanonicalObjectsHydrateThemselves:
    def test_the_graph_is_workflow_process_steps_in_order(self) -> None:
        workflow = _hydrate(_rows())[0]

        assert [p.name for p in workflow.processes] == ["fixture_app"]
        assert [s.id for s in workflow.steps.values()] == ["export", "commit"]
        assert [s.process for s in workflow.steps.values()] == [workflow.processes[0]] * 2
        assert workflow.processes[0].workflow is workflow
        assert workflow.enabled is True

    def test_hydration_creates_no_run(self) -> None:
        _, control = _hydrate(_rows())

        assert control.rows == [] and control.finished == {}

    def test_a_disabled_workflow_is_refused(self) -> None:
        rows = [_row(1, "wf", "export", 1, "load", {}, workflow_enabled=False)]

        with pytest.raises(AppError, match="is disabled"):
            _hydrate(rows)

    def test_a_bad_step_refuses_its_whole_workflow(self) -> None:
        rows = _rows() + [_row(1, "wf", "bad", 3, "unpublished", {})]

        with pytest.raises(AppError):
            _hydrate(rows)

    def test_step_settings_are_resolved_and_run_time_placeholders_survive(self) -> None:
        export, commit = _hydrate(_rows())[0].steps.values()

        assert export.settings["output_root"] == "/installations/rey/db_backups/ddl"
        assert export.settings["run_workspace"] == "/installations/rey/data/runs/{run_id}"
        assert export.settings["output"] == (
            "/installations/rey/data/file_operator/output/{source_subfolder}")
        assert export.settings["rule_set_name_pattern"] == "bmo_{group_slug}_structural_redaction"
        assert export.settings["message_template"] == "Export {engine} DDL: {object_count} objects"
        assert commit.settings == {"repo_root": "/installations/rey/db_backups/ddl"}

    def test_process_and_workflow_config_are_resolved_the_same_way(self) -> None:
        workflow = _hydrate(_rows())[0]

        assert workflow.processes[0].config == {
            "app": {"records_path": "/installations/rey/data/records"}}
        assert workflow.config is not None
        assert (workflow.config["execution"]["full"], workflow.config["execution"]["dry_run"]) == (
            True, True)

    def test_engine_controls_come_from_workflow_execution(self) -> None:
        export, commit = _hydrate(_rows())[0].steps.values()

        assert (export.apply_only, export.always_run) == (True, False)
        assert (commit.apply_only, commit.always_run) == (False, True)

    def test_operation_settings_carry_no_engine_control(self) -> None:
        export, commit = _hydrate(_rows())[0].steps.values()

        for step in (export, commit):
            assert not {"apply_only", "always_run"} & set(step.settings)

    @pytest.mark.parametrize("name", ["apply_only", "always_run"])
    def test_a_step_execution_does_not_state_is_refused_not_defaulted(self, name: str) -> None:
        row = _row(1, "wf", "export", 1, "load", {})
        del row["workflow_config"]["execution"]["steps"]["export"][name]

        with pytest.raises(AppError,
                           match=f"workflow_config.execution.steps states no boolean '{name}'"):
            WorkflowStep.hydrate(row, application=_application(),
                                 approved=_APPROVED["fixture_app"])

    @pytest.mark.parametrize("value", ["true", 1, None])
    @pytest.mark.parametrize("name", ["apply_only", "always_run"])
    def test_a_non_boolean_engine_control_is_refused(self, name: str, value: Any) -> None:
        row = _row(1, "wf", "export", 1, "load", {})
        row["workflow_config"]["execution"]["steps"]["export"][name] = value

        with pytest.raises(AppError,
                           match=f"workflow_config.execution.steps states no boolean '{name}'"):
            WorkflowStep.hydrate(row, application=_application(),
                                 approved=_APPROVED["fixture_app"])

    def test_a_step_hydrates_from_its_row_alone_with_the_injected_binding(self) -> None:
        step = WorkflowStep.hydrate(_row(1, "wf", "export", 1, "load", {"a": "{root}"}),
                                    application=_application(),
                                    approved=_APPROVED["fixture_app"])

        assert (step.id, step.label, step.ordinal, step.process_name) == (
            "export", "Export", 1, "load")
        assert step.implementation is _handler
        assert step.settings == {"a": "{root}"}

    def test_a_process_without_steps_has_none(self) -> None:
        row = _row(1, "wf", "x", None, "load", None, workflow_step_id=None)

        process = WorkflowProcess.hydrate([row], application=_application(),
                                          approved=_APPROVED["fixture_app"])

        assert process.steps == ()

    def test_an_absent_application_refuses_the_workflow(self) -> None:
        with pytest.raises(AppError, match="which is not present"):
            Workflow.hydrate(_rows(), applications=[], approved=_APPROVED,
                             control=RecordingControl())


def _with_data_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` whose governed workflow config declares one workflow-scoped data source."""
    for row in rows:
        row["workflow_config"]["data_sources"] = [{
            "name": "proposals", "enabled": True, "file_pattern": "*.analysis.json",
            "analysis_config": "infer", "paths": {
                "inbox_path": "{run_root}/Analysis/Inbox",
                "results_path": "{data}/results",
            },
        }]
    return rows


class TestTheWorkflowOwnsItsDataSources:
    """A data source is workflow-scoped: governed in the Workflow, hydrated with it (#1032)."""

    def test_it_is_answered_by_name_and_read_by_attribute(self) -> None:
        source = _hydrate(_with_data_source(_rows()))[0].data_source("proposals")

        assert (source.name, source.file_pattern, source.analysis_config) == (
            "proposals", "*.analysis.json", "infer")

    def test_its_paths_are_the_workflows_paths_with_run_time_placeholders_kept(self) -> None:
        source = _hydrate(_with_data_source(_rows()))[0].data_source("proposals")

        assert source.paths.inbox_path == "/installations/rey/data/runs/{run_id}/Analysis/Inbox"
        assert source.paths.results_path == "/installations/rey/data/results"

    def test_an_undeclared_name_is_refused(self) -> None:
        workflow = _hydrate(_with_data_source(_rows()))[0]

        with pytest.raises(AppError, match="declares no data source 'other'"):
            workflow.data_source("other")

    def test_a_workflow_declaring_none_refuses_every_name(self) -> None:
        with pytest.raises(AppError, match="declares no data source 'proposals'"):
            _hydrate(_rows())[0].data_source("proposals")


class TestNoParallelHydrationModel:
    def test_there_is_no_workflow_source(self) -> None:
        import rey_lib.workflow.workflow as workflow_module

        assert not hasattr(workflow_module, "WorkflowSource")
        assert not (Path(workflow_module.__file__).parent / "workflow_source.py").exists()

    def test_hydration_resolves_only_through_the_public_access_point(self) -> None:
        import rey_lib.workflow.workflow as workflow_module

        tree = ast.parse(Path(workflow_module.__file__).read_text(encoding="utf-8"))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }

        assert "resolve_config_paths" in names
        assert not names & {"_apply_path_resolver", "_build_path_resolver",
                            "_resolve_tokens", "_expand_config", "_deep_merge"}
