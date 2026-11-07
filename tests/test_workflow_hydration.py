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
from rey_lib.workflow.workflow_step import WorkflowStep, form_fields

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


class TestTheWorkflowOwnsItsGovernedIdentity:
    """#1141: the canonical Workflow holds its control.workflow id and owns equality on it."""

    def test_hydration_takes_the_governed_id_from_its_rows(self) -> None:
        workflow, _ = _hydrate(_rows())

        assert workflow.workflow_id == 1

    def test_rows_of_more_than_one_workflow_are_refused(self) -> None:
        rows = [*_rows(), _row(2, "wf", "only", 3, "load", _settings())]

        with pytest.raises(AppError, match="more than one workflow"):
            _hydrate(rows)

    def test_a_workflow_without_its_governed_id_is_refused(self) -> None:
        rows = [{**row, "workflow_id": None} for row in _rows()]

        with pytest.raises(AppError, match="governed id"):
            _hydrate(rows)


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

    def test_the_live_objects_hold_their_configured_values_beside_the_resolved(self) -> None:
        """#1325: each object holds its settings as configured -- the {} expressions
        intact -- beside the resolved ones the run reads, and resolution does not
        overwrite them."""
        workflow = _hydrate(_rows())[0]
        export, commit = workflow.steps.values()

        assert export.configured_settings["output_root"] == "{root}/db_backups/ddl"
        assert export.settings["output_root"] == "/installations/rey/db_backups/ddl"
        assert commit.configured_settings == {"repo_root": "{root}/db_backups/ddl"}
        assert workflow.processes[0].configured_config != workflow.processes[0].config
        assert "{" in str(workflow.processes[0].configured_config)
        assert workflow.configured_config is not None

    def test_the_projection_carries_each_objects_live_configured_and_resolved_values(self) -> None:
        workflow = _hydrate(_rows())[0]
        projection = workflow.projection(object())
        step = projection["processes"][0]["steps"][0]

        assert step["settings"]["configured"]["output_root"] == "{root}/db_backups/ddl"
        assert step["settings"]["resolved"]["output_root"] == "/installations/rey/db_backups/ddl"
        assert projection["processes"][0]["config"]["configured"] == \
            workflow.processes[0].configured_config
        assert projection["config"]["resolved"] == workflow.config

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
    """``rows`` whose governed process config declares one data source."""
    for row in rows:
        row["process_config"]["data_sources"] = [{
            "name": "proposals", "enabled": True, "file_pattern": "*.analysis.json",
            "analysis_config": "infer", "paths": {
                "inbox_path": "{run_root}/Analysis/Inbox",
                "results_path": "{data}/results",
            },
        }]
    return rows


def _with_analysis_config(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` whose governed process config also declares one analysis config."""
    for row in rows:
        row["process_config"]["analysis_configs"] = [
            {"name": "infer", "contract": "infer_contract", "llm_execution_profile": "p"},
        ]
    return rows


class TestTheProcessOwnsItsDataSources:
    """A data source is governed process configuration: the WorkflowProcess answers it (#1043)."""

    def test_it_is_answered_by_name_and_read_by_attribute(self) -> None:
        process = _hydrate(_with_data_source(_rows()))[0].processes[0]
        source = process.data_source("proposals")

        assert (source.name, source.file_pattern, source.analysis_config) == (
            "proposals", "*.analysis.json", "infer")

    def test_its_paths_are_the_workflows_paths_with_run_time_placeholders_kept(self) -> None:
        source = _hydrate(_with_data_source(_rows()))[0].processes[0].data_source("proposals")

        assert source.paths.inbox_path == "/installations/rey/data/runs/{run_id}/Analysis/Inbox"
        assert source.paths.results_path == "/installations/rey/data/results"

    def test_each_step_reaches_it_through_its_own_process(self) -> None:
        workflow = _hydrate(_with_data_source(_rows()))[0]

        for step in workflow.steps.values():
            assert step.process.data_source("proposals").name == "proposals"

    def test_an_undeclared_name_is_refused(self) -> None:
        process = _hydrate(_with_data_source(_rows()))[0].processes[0]

        with pytest.raises(AppError, match="process 'fixture_app' declares no data source 'other'"):
            process.data_source("other")

    def test_a_process_declaring_none_refuses_every_name(self) -> None:
        with pytest.raises(AppError, match="declares no data source 'proposals'"):
            _hydrate(_rows())[0].processes[0].data_source("proposals")

    def test_an_analysis_config_is_answered_through_the_same_lookup(self) -> None:
        workflow = _hydrate(_with_analysis_config(_with_data_source(_rows())))[0]
        process = workflow.processes[0]

        analysis = process.analysis_config("infer")
        assert (analysis.name, analysis.contract) == ("infer", "infer_contract")
        for step in workflow.steps.values():
            assert step.process.analysis_config("infer").name == "infer"
        with pytest.raises(AppError, match="process 'fixture_app' declares no analysis config 'x'"):
            process.analysis_config("x")

    def test_a_process_declaring_no_analysis_config_refuses_every_name(self) -> None:
        with pytest.raises(AppError, match="declares no analysis config 'infer'"):
            _hydrate(_rows())[0].processes[0].analysis_config("infer")

    def test_a_config_section_is_exactly_the_named_mapping_it_holds(self) -> None:
        rows = _rows()
        for row in rows:
            row["process_config"]["runtime"] = {"max_files_per_run": 7}
            row["process_config"]["listed"] = [{"name": "x"}]
        process = _hydrate(rows)[0].processes[0]

        runtime = process.config_section("runtime")
        assert runtime.max_files_per_run == 7
        assert not hasattr(runtime, "stop_on_error")  # nothing defaulted
        with pytest.raises(AppError, match="declares no configuration section 'listed'"):
            process.config_section("listed")  # a list is not a mapping section

    def test_a_process_declaring_no_section_refuses_its_name(self) -> None:
        with pytest.raises(AppError, match="declares no configuration section 'runtime'"):
            _hydrate(_rows())[0].processes[0].config_section("runtime")

    def test_the_workflow_does_not_answer_data_sources(self) -> None:
        """One owner: no Workflow-level data source, forwarding or otherwise."""
        assert not hasattr(Workflow, "data_source")


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


class TestHydrationKeepsTheGovernedRowIds:
    """A process and a step remember the governed row they came from (backlog 1126)."""

    def test_process_and_steps_carry_their_row_ids(self) -> None:
        workflow, _ = _hydrate(_rows())

        (process,) = workflow.processes
        assert process.governed_id == 10
        assert [step.governed_id for step in process.steps] == [101, 102]


_CONTRACT = {
    "$schema": _SCHEMA, "type": "object",
    "properties": {
        "output_root": {"type": "string", "description": "Where the export lands."},
        "recurse": {"type": "boolean"},
        "limit": {"type": "integer"},
        "mode": {"enum": ["full", "delta"]},
    },
    "required": ["output_root"],
}


def _contracted() -> Workflow:
    """Workflow ``wf`` whose ``load`` publishes a contract with four settings."""
    application = Application(name="fixture_app", workflow_operations=(
        ApplicationOperation(name="load", contract_definition=_CONTRACT),
        ApplicationOperation(name="commit", contract_definition={"$schema": _SCHEMA,
                                                                 "type": "object"}),
    ))
    return Workflow.hydrate(_rows(), applications=[application], approved=_APPROVED,
                            control=RecordingControl())


class TestALiveStepIsEditedThroughItsContract:
    """#1325: a step offers every setting its operation's contract names, and edits its
    configured settings live -- {} intact, the resolved ones following -- writing nothing."""

    def test_its_fields_are_every_contract_setting_then_its_own(self) -> None:
        export = next(iter(_contracted().steps.values()))
        fields = form_fields(export.form()[1]["items"])

        assert [one["name"] for one in fields][:4] == ["output_root", "recurse", "limit", "mode"]
        by_name = {one["name"]: one for one in fields}
        assert by_name["output_root"]["value"] == "{root}/db_backups/ddl"
        assert by_name["output_root"]["required"] is True
        assert by_name["output_root"]["hint"].startswith("Where the export lands.\nExpects text.")
        assert (by_name["recurse"]["control"], by_name["recurse"]["value"]) == ("checkbox", "")
        assert (by_name["mode"]["control"], by_name["mode"]["choices"]) == ("select", ["full", "delta"])
        # A setting the contract does not describe is walked from its own values,
        # one field per value, shown and not edited.
        assert by_name["sources[0].source.path"]["value"] == "{data}/*/source/inbox"
        assert "command" not in by_name["sources[0].source.path"]
        assert by_name["output_root"]["label"] == "output_root *"
        assert by_name["output_root"]["resolved"] == "/installations/rey/db_backups/ddl"
        assert by_name["limit"]["command"] == {
            "command": "operate", "object": "step:1", "operation": "update",
            "arguments": {"name": "limit"}}
        assert by_name["limit"]["value_input"] == "arguments.value"

    def test_an_edit_keeps_the_expression_and_the_resolved_value_follows(self) -> None:
        workflow = _contracted()
        export = next(iter(workflow.steps.values()))

        export.update("output_root", "{data}/exports")

        assert export.configured_settings["output_root"] == "{data}/exports"
        assert export.settings["output_root"] == "/installations/rey/data/exports"
        step = workflow.projection(object())["processes"][0]["steps"][0]
        assert step["settings"]["configured"]["output_root"] == "{data}/exports"

    def test_typed_settings_are_read_as_the_contract_types_them(self) -> None:
        export = next(iter(_contracted().steps.values()))

        export.update("recurse", "true")
        export.update("limit", "25")

        assert (export.configured_settings["recurse"], export.configured_settings["limit"]) == (True, 25)
        with pytest.raises(AppError, match="not a integer value for 'limit'"):
            export.update("limit", "many")

    def test_blank_clears_but_a_required_setting_cannot_be_cleared(self) -> None:
        export = next(iter(_contracted().steps.values()))

        export.update("limit", "25")
        export.update("limit", "")
        assert "limit" not in export.configured_settings
        with pytest.raises(AppError, match="output_root"):
            export.update("output_root", "")
        assert export.configured_settings["output_root"] == "{root}/db_backups/ddl"

    def test_an_unknown_setting_or_a_running_workflow_is_refused(self) -> None:
        workflow = _contracted()
        export = next(iter(workflow.steps.values()))

        with pytest.raises(AppError, match="has no setting 'colour'"):
            export.update("colour", "red")
        workflow._running.acquire()
        try:
            with pytest.raises(AppError, match="while its workflow runs"):
                export.update("limit", "1")
        finally:
            workflow._running.release()


_SOURCES_CONTRACT = {
    "$schema": _SCHEMA, "type": "object",
    "properties": {
        "sources": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
                "source": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "file_patterns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "recursive": {"type": "boolean"},
                }, "required": ["path", "file_patterns"]},
            },
            "required": ["name", "source"],
        }},
        "scope": {"type": "string"},
    },
    "required": ["sources"],
}


def _sourced() -> Workflow:
    """Workflow ``wf`` whose ``load`` publishes a nested contract: a list of source sets."""
    application = Application(name="fixture_app", workflow_operations=(
        ApplicationOperation(name="load", contract_definition=_SOURCES_CONTRACT),
        ApplicationOperation(name="commit", contract_definition={"$schema": _SCHEMA,
                                                                 "type": "object"}),
    ))
    rows = [
        _row(1, "wf", "export", 1, "load", {"sources": [{
            "name": "feed_inbox", "enabled": True,
            "source": {"path": "{data}/*/source/inbox", "file_patterns": ["*.csv", "*.xlsx"],
                       "recursive": False}}]}),
        _row(1, "wf", "commit", 2, "commit", {}),
    ]
    return Workflow.hydrate(rows, applications=[application], approved=_APPROVED,
                            control=RecordingControl())


class TestNestedSettingsAreIndividualSettings:
    """#1325 (user): the settings an application exposes are drawn one value at a time,
    as its contract describes them -- never as one JSON value."""

    def test_every_single_value_is_its_own_field(self) -> None:
        export = next(iter(_sourced().steps.values()))
        fields = {one["name"]: one for one in form_fields(export.form()[1]["items"])}

        assert list(fields) == [
            "sources[0].name", "sources[0].enabled", "sources[0].source.path",
            "sources[0].source.file_patterns", "sources[0].source.recursive", "scope",
        ]
        path = fields["sources[0].source.path"]
        assert path["label"] == "sources › feed_inbox › source › path *"
        assert path["hint"].endswith("Required.\nSetting: sources[0].source.path")
        # A list of single values is one setting, comma-separated, with its rule in the help.
        patterns = fields["sources[0].source.file_patterns"]
        assert patterns["value"] == "*.csv,*.xlsx"
        assert "A list of text, comma-separated." in patterns["hint"]
        assert "At least 1." in patterns["hint"]
        assert (path["value"], path["resolved"]) == (
            "{data}/*/source/inbox", "/installations/rey/data/*/source/inbox")
        assert fields["sources[0].enabled"]["control"] == "checkbox"
        assert fields["scope"]["value"] == ""
        assert all("command" in one for one in fields.values())

    def test_a_nested_value_is_edited_by_its_path(self) -> None:
        export = next(iter(_sourced().steps.values()))

        export.update("sources[0].source.path", "{data}/inbox")
        export.update("sources[0].source.recursive", "true")
        export.update("sources[0].source.file_patterns", "*.csv, *.xls")

        source = export.configured_settings["sources"][0]["source"]
        assert source == {"path": "{data}/inbox", "file_patterns": ["*.csv", "*.xls"],
                          "recursive": True}
        assert export.settings["sources"][0]["source"]["path"] == "/installations/rey/data/inbox"

    def test_a_value_list_is_edited_as_one_line_and_keeps_what_the_contract_needs(self) -> None:
        export = next(iter(_sourced().steps.values()))

        export.update("sources[0].source.file_patterns", "*.csv")
        assert export.configured_settings["sources"][0]["source"]["file_patterns"] == ["*.csv"]
        with pytest.raises(AppError, match="file_patterns"):
            export.update("sources[0].source.file_patterns", "")
        assert export.configured_settings["sources"][0]["source"]["file_patterns"] == ["*.csv"]

    def test_a_path_that_names_nothing_is_refused(self) -> None:
        export = next(iter(_sourced().steps.values()))

        with pytest.raises(AppError, match="has no setting"):
            export.update("sources[0].colour", "red")
        with pytest.raises(AppError, match="names no list item"):
            export.update("sources[3].name", "x")
        with pytest.raises(AppError, match="not a setting path"):
            export.update("sources..name", "x")


class TestTheSettingsHierarchyIsDeclaredByTheObject:
    """#1325 (Option A): the live object states its sections -- label, help, the summary
    shown while collapsed, and their items; the editor draws them and derives nothing."""

    def test_the_contract_hierarchy_is_its_sections(self) -> None:
        export = next(iter(_sourced().steps.values()))
        workflow_defined, application = export.form()
        # What the workflow defines, apart from what the application exposes (user).
        assert workflow_defined["label"] == "Workflow"
        assert [one["name"] for one in workflow_defined["items"]] == [
            "step.name", "step.label", "step.ordinal", "step.apply_only", "step.always_run"]
        assert all("command" not in one for one in workflow_defined["items"])
        assert application["label"] == "fixture_app · load"
        form = application["items"]

        sources = form[0]
        assert (sources["name"], sources["label"]) == ("sources", "sources")
        feed = sources["items"][0]
        assert (feed["name"], feed["label"]) == ("sources[0]", "feed_inbox")
        assert feed["summary"] == "name=feed_inbox · enabled=true · source"
        source = feed["items"][2]
        assert source["label"] == "source"
        assert [one["label"] for one in source["items"]] == ["path *", "file_patterns *", "recursive"]
        path = source["items"][0]
        assert (path["value"], path["resolved"]) == (
            "{data}/*/source/inbox", "/installations/rey/data/*/source/inbox")
        assert form[-1]["name"] == "scope" and "items" not in form[-1]

    def test_every_field_at_every_level_carries_the_command_that_edits_it(self) -> None:
        workflow = _sourced()
        export = next(iter(workflow.steps.values()))

        def fields(items: list) -> list:
            return [leaf for one in items
                    for leaf in (fields(one["items"]) if "items" in one else [one])]

        for field in fields(export.form()[1]["items"]):
            assert field["command"]["arguments"] == {"name": field["name"]}
        export.update("sources[0].source.recursive", "true")
        export.update("scope", "file")
        projected = workflow.projection(object())["processes"][0]["steps"][0]["form"][1]["items"]
        assert fields(projected)[4]["value"] == "true"
        assert fields(projected)[-1]["value"] == "file"

    def test_a_process_states_its_configuration_as_sections_it_does_not_edit(self) -> None:
        process = _sourced().processes[0]
        workflow_defined, application = process.form()
        assert workflow_defined["label"] == "Workflow"
        assert [one["value"] for one in workflow_defined["items"]] == ["fixture_app", "1", "fixture_app"]
        assert application["label"] == "fixture_app settings"
        form = application["items"]

        assert form[0]["label"] == "app" and form[0]["summary"].startswith("records_path=")
        assert form[0]["items"][0]["value"] == "{data}/records"
        assert "command" not in form[0]["items"][0]


class _ReferencingControl(RecordingControl):
    """A Control answering setting references for one operation, and recording each read."""

    def __init__(self) -> None:
        super().__init__()
        self.read: list[tuple[str, str]] = []

    def references(self, application: str, operation: str) -> list[dict[str, Any]]:
        self.read.append((application, operation))
        if operation != "load":
            return []
        return [{"setting_path": "sources.source.path", "title": "Source path",
                 "instructions": "Where the files are found: a glob over the data root.",
                 "example": "{data}/*/source/inbox"}]


class TestEachSettingCarriesItsGovernedReference:
    """#1330: a field's detailed instructions are the governed reference (code.reference),
    read by the live step only when one of its fields is first selected, and projected
    only on the selected field."""

    def _hydrated(self, control: Any) -> Workflow:
        application = Application(name="fixture_app", workflow_operations=(
            ApplicationOperation(name="load", contract_definition=_SOURCES_CONTRACT),
            ApplicationOperation(name="commit", contract_definition={"$schema": _SCHEMA,
                                                                     "type": "object"}),
        ))
        rows = [
            _row(1, "wf", "export", 1, "load", {"sources": [
                {"name": "a", "source": {"path": "{data}/a", "file_patterns": ["*.csv"]}},
                {"name": "b", "source": {"path": "{data}/b", "file_patterns": ["*.csv"]}}]}),
            _row(1, "wf", "commit", 2, "commit", {}),
        ]
        return Workflow.hydrate(rows, applications=[application], approved=_APPROVED,
                                control=control)

    @staticmethod
    def _fields(step: Any) -> dict[str, dict[str, Any]]:
        return {one["name"]: one for one in step.fields()}

    def test_nothing_is_read_or_projected_until_a_field_is_selected(self) -> None:
        control = _ReferencingControl()
        export = next(iter(self._hydrated(control).steps.values()))

        assert control.read == []
        assert all("instructions" not in one for one in export.fields())

    def test_every_field_declares_the_command_that_selects_it(self) -> None:
        export = next(iter(self._hydrated(_ReferencingControl()).steps.values()))
        fields = export.fields()

        assert {"step.name", "step.apply_only", "sources[0].source.path", "scope"} <= {
            one["name"] for one in fields}
        for field in fields:
            assert field["selection_command"] == {
                "command": "operate", "object": "step:1", "operation": "apply_selection",
                "arguments": {"name": field["name"]}}

    def test_a_selected_field_projects_its_reference_read_once(self) -> None:
        control = _ReferencingControl()
        export = next(iter(self._hydrated(control).steps.values()))

        export.apply_selection("sources[1].source.path")
        export.apply_selection("sources[0].source.path")
        fields = self._fields(export)

        assert control.read == [("fixture_app", "load")]
        assert [name for name, one in fields.items() if "instructions" in one] == [
            "sources[0].source.path"]
        content = fields["sources[0].source.path"]["instructions"]
        assert (content["language"], content["html_trusted"]) == ("markdown", True)
        assert content["text"].startswith("## Source path")
        assert "Where the files are found" in content["text"]
        assert "{data}/*/source/inbox" in content["text"]
        assert "- Configured: `{data}/a`" in content["text"]
        assert "/installations/rey/data/a" in content["text"]
        assert "<h2>Source path</h2>" in content["html"]

    def test_a_read_only_workflow_field_is_selectable(self) -> None:
        export = next(iter(self._hydrated(_ReferencingControl()).steps.values()))

        export.apply_selection("step.apply_only")

        assert "No reference is written for this setting yet." in (
            self._fields(export)["step.apply_only"]["instructions"]["text"])

    def test_a_setting_without_a_reference_says_so(self) -> None:
        export = next(iter(self._hydrated(_ReferencingControl()).steps.values()))

        export.apply_selection("scope")

        assert "No reference is written for this setting yet." in (
            self._fields(export)["scope"]["instructions"]["text"])

    def test_a_field_the_step_does_not_have_is_refused(self) -> None:
        control = _ReferencingControl()
        export = next(iter(self._hydrated(control).steps.values()))

        with pytest.raises(AppError, match="has no field 'nowhere'"):
            export.apply_selection("nowhere")
        assert control.read == []

    def test_an_installation_without_references_still_selects(self) -> None:
        export = next(iter(self._hydrated(RecordingControl()).steps.values()))

        export.apply_selection("scope")

        assert "No reference is written" in self._fields(export)["scope"]["instructions"]["text"]
