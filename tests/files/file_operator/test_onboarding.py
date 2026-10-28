"""Tests for the public structural rule-set onboarding boundaries."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import ConfigError
from rey_lib.workflow.workflow import Workflow
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep

from rey_lib.files import onboarding
from rey_lib.files.error_utils import InputError, RedactorError
from rey_lib.files.rule_set_materialization import MaterializationResult


def _config(tmp_path: Path, name: str = "statements") -> onboarding.RuleSetOnboardingConfig:
    return onboarding.RuleSetOnboardingConfig(
        name=name,
        enabled=True,
        source_inbox=(tmp_path / name / "inbox").resolve(),
        filename_regex=r"statement_(?P<file_kind>[A-Za-z]+)_[0-9]+[.]csv",
        group_capture="file_kind",
        analyzer_workflow="infer_bad_row_redaction_rules",
        run_workspace=(tmp_path / "runs" / "run-1").resolve(),
        output_root=(tmp_path / name / "rules").resolve(),
        rule_set_name_pattern=f"{name}_{{group_slug}}",
        control_totals=(
            onboarding.ControlTotalCalculation(
                name="amount",
                table="transactions",
                column="Amount",
                source_column="Amount",
                tolerance="0.00",
            ),
        ),
    )


def _entry(config: onboarding.RuleSetOnboardingConfig) -> dict[str, object]:
    return {
        "name": config.name,
        "enabled": config.enabled,
        "source_inbox": str(config.source_inbox),
        "filename_classification": {
            "regex": config.filename_regex,
            "group_capture": config.group_capture,
        },
        "analyzer": {"workflow": config.analyzer_workflow},
        "run_workspace": str(config.run_workspace),
        "output_root": str(config.output_root),
        "rule_set_name_pattern": config.rule_set_name_pattern,
        "control_totals": [
            {
                "name": "amount",
                "table": "transactions",
                "column": "Amount",
                "aggregation": "sum",
                "tolerance": "0.00",
            }
        ],
    }


def _ctx(tmp_path: Path, **values: object) -> SimpleNamespace:
    defaults = {
        "run_id": "run-1",
        "run_timestamp": "20260728_120000",
        "pipeline_name": "structural_rule_set_onboarding",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _analyzer_workflow(name: str, surface: onboarding._AnalyzerSurface) -> Workflow:
    """The governed Analyzer workflow: one rey_analyzer analysis step naming its source.

    The data source and its analysis config are the analysis process's governed
    configuration, so its WorkflowProcess declares them, with paths as hydration
    leaves them (plain strings).
    """
    analysis = ApplicationOperation(name="analysis", contract_definition={
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    })
    analyzer = Application(name="rey_analyzer", workflow_operations=(analysis,))
    step = WorkflowStep(
        id="infer_tabular_discard_rules", process_name="rey_analyzer",
        label="infer_tabular_discard_rules", ordinal=1, application=analyzer,
        operation_name="analysis", approved={"analysis": lambda *_args: None},
        settings={"source": surface.source_name},
    )
    config = {"data_sources": [{
        "name": surface.source_name,
        "file_pattern": surface.file_pattern,
        "analysis_config": surface.analysis_name,
        "raw_output_extension": surface.candidate_extension,
        "paths": {
            "inbox_path": str(surface.inbox),
            "results_path": str(surface.results),
            "raw_output_path": str(surface.candidates),
        },
    }], "analysis_configs": [{
        "name": surface.analysis_name,
        "llm_execution_profile": surface.model_profile,
    }]}
    process = WorkflowProcess(name="rey_analyzer", ordinal=1, application=analyzer,
                              config=config, steps=[step])
    return Workflow(workflow_id=1, name=name, enabled=True, config=None, processes=[process], control=None)


@pytest.fixture(autouse=True)
def governed_analyzer_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workflow's governed hydration answers with the installation's Analyzer workflow."""
    surface = _surface(tmp_path)

    def from_governed(_ctx: object, workflow_name: str, **_kwargs: object) -> Workflow:
        if workflow_name != surface.workflow:
            raise ConfigError(f"Workflow: installation has no workflow '{workflow_name}'.")
        return _analyzer_workflow(workflow_name, surface)

    monkeypatch.setattr(onboarding.Workflow, "from_governed", from_governed)


def _surface(tmp_path: Path) -> onboarding._AnalyzerSurface:
    return onboarding._AnalyzerSurface(
        "infer_bad_row_redaction_rules",
        "file_operator_structural_file_type",
        "infer_tabular_discard_rules",
        "local_precision",
        (tmp_path / "analysis" / "inbox").resolve(),
        (tmp_path / "analysis" / "results").resolve(),
        (tmp_path / "analysis" / "candidates").resolve(),
        ".candidate.json",
        "file_type_*.analysis.json",
    )


def _classification(
    tmp_path: Path,
    sources: tuple[Path, ...] = (),
) -> onboarding._ClassificationResult:
    analysis = tmp_path / "file_type_abc.analysis.json"
    file_type = tmp_path / "file_type_abc.json"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text('{"file_type":"file_type_abc"}\n', encoding="utf-8")
    file_type.write_text(
        json.dumps(
            {
                "file_type": "file_type_abc",
                "matching_files": [path.name for path in sources],
                "ordered_headers": ["ID", "Amount"],
                "inspection_artifacts": ["inspection.jsonl"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "inspection.jsonl").write_text(
        '{"delimiter":",","encoding":"utf-8"}\n',
        encoding="utf-8",
    )
    return onboarding._ClassificationResult(
        file_type="file_type_abc",
        analysis_path=analysis,
        file_type_path=file_type,
        source_paths=sources,
        delimiter=",",
        encoding="utf-8",
        ordered_headers=("ID", "Amount"),
    )


def test_inline_mapping_resolves_all_enabled_entries_in_name_order(
    tmp_path: Path,
) -> None:
    alpha = _config(tmp_path, "alpha")
    zeta = _config(tmp_path, "zeta")
    disabled = _config(tmp_path, "disabled")
    disabled_entry = _entry(disabled)
    disabled_entry["enabled"] = False
    inline = {
        "onboardings": [
            _entry(zeta),
            disabled_entry,
            _entry(alpha),
        ]
    }

    resolved = onboarding.resolve_rule_set_onboarding_configs(inline)

    assert [item.name for item in resolved] == ["alpha", "zeta"]


def test_inline_mapping_resolves_one_source_neutral_entry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resolved = onboarding.resolve_rule_set_onboarding_configs(
        {"onboardings": [_entry(config)]}
    )

    assert resolved == (config,)


def test_inline_mapping_preserves_nested_input_and_rejects_config_ref(
    tmp_path: Path,
) -> None:
    entry = _entry(_config(tmp_path))
    inline = {"apply_only": True, "onboardings": [entry]}
    snapshot = deepcopy(inline)

    resolved = onboarding.resolve_rule_set_onboarding_configs(inline)

    assert resolved[0] == _config(tmp_path)
    assert inline == snapshot
    for unsupported in (
        {"config_ref": "file_operator_rule_set_onboardings"},
        {
            "onboardings": [
                {
                    "config_ref": (
                        "file_operator_rule_set_onboardings.statements"
                    )
                }
            ]
        },
    ):
        with pytest.raises(RedactorError, match="config_ref is unsupported"):
            onboarding.resolve_rule_set_onboarding_configs(unsupported)


def test_collection_reference_rejects_conflicting_analyzer_workflows(run_log, 
    tmp_path: Path,
) -> None:
    first = _config(tmp_path, "first")
    second = _entry(_config(tmp_path, "second"))
    second["analyzer"] = {"workflow": "other_workflow"}
    ctx = _ctx(tmp_path)

    with pytest.raises(RedactorError, match="exactly one Analyzer workflow"):
        onboarding.prepare_rule_set_inputs(ctx, run_log,
            {"onboardings": [_entry(first), second]},
        )


def test_discovers_multiple_filename_groups_in_deterministic_order(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.source_inbox.mkdir(parents=True)
    for name in (
        "statement_Zeta_2.csv",
        "statement_Alpha_2.csv",
        "statement_Alpha_1.csv",
    ):
        (config.source_inbox / name).write_text("ID,Amount\n1,1\n", encoding="utf-8")

    groups = onboarding._discover_groups(config)

    assert [group.value for group in groups] == ["Alpha", "Zeta"]
    assert [path.name for path in groups[0].files] == [
        "statement_Alpha_1.csv",
        "statement_Alpha_2.csv",
    ]


def test_preparation_stages_every_group_and_writes_handoff_manifest(run_log, 
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.source_inbox.mkdir(parents=True)
    files = []
    for name in ("statement_Alpha_1.csv", "statement_Zeta_1.csv"):
        path = config.source_inbox / name
        path.write_text("ID,Amount\n1,10\n", encoding="utf-8")
        files.append(path)
    classifications = {
        "Alpha": _classification(tmp_path / "alpha", (files[0],)),
        "Zeta": _classification(tmp_path / "zeta", (files[1],)),
    }
    ctx = _ctx(tmp_path)

    def classify(
        _ctx: object,
        _run_log: object,
        _config: object,
        group: onboarding._Group,
        _output: Path,
    ) -> onboarding._ClassificationResult:
        return classifications[group.value]

    def totals(
        _ctx: object,
        _run_log: object,
        _config: object,
        group: onboarding._Group,
        _classification: object,
        path: Path,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "controls": [],
                    "group_value": group.value,
                }
            ),
            encoding="utf-8",
        )
        return path

    with patch.object(onboarding, "_classify_group", side_effect=classify), patch.object(
        onboarding,
        "_write_trusted_total_evidence",
        side_effect=totals,
    ), patch.object(onboarding, "log_artifact_reference"), patch.object(
        onboarding,
        "log_validation_result",
    ), patch.object(onboarding, "log_row_count"):
        status = onboarding.prepare_rule_set_inputs(ctx, run_log,
            {"onboardings": [_entry(config)]},
        )

    assert status == 0
    staged = sorted(_surface(tmp_path).inbox.glob("*.analysis.json"))
    assert len(staged) == 2
    assert "__statements__alpha.analysis.json" in staged[0].name
    assert "__statements__zeta.analysis.json" in staged[1].name
    manifest = json.loads(
        (
            config.run_workspace
            / "onboardings"
            / config.name
            / "rule_set_input_manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["group_value"] for item in manifest["groups"]] == ["Alpha", "Zeta"]


def test_collection_preparation_processes_each_configured_onboarding_entry(run_log, 
    tmp_path: Path,
) -> None:
    """A new source entry is consumed without Python or pipeline branching."""
    first = _config(tmp_path, "first")
    second = _config(tmp_path, "second")
    for config in (first, second):
        config.source_inbox.mkdir(parents=True)
        (config.source_inbox / "statement_Alpha_1.csv").write_text(
            "ID,Amount\n1,10\n",
            encoding="utf-8",
        )
    ctx = _ctx(tmp_path)

    def classify(
        _ctx: object,
        _run_log: object,
        config: onboarding.RuleSetOnboardingConfig,
        group: onboarding._Group,
        _output: Path,
    ) -> onboarding._ClassificationResult:
        return _classification(
            tmp_path / "classifications" / config.name,
            group.files,
        )

    def totals(
        _ctx: object,
        _run_log: object,
        config: onboarding.RuleSetOnboardingConfig,
        group: onboarding._Group,
        _classification: object,
        path: Path,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "onboarding_name": config.name,
                    "group_value": group.value,
                    "controls": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    with patch.object(onboarding, "_classify_group", side_effect=classify), patch.object(
        onboarding,
        "_write_trusted_total_evidence",
        side_effect=totals,
    ), patch.object(onboarding, "log_artifact_reference"), patch.object(
        onboarding,
        "log_validation_result",
    ), patch.object(onboarding, "log_row_count"):
        status = onboarding.prepare_rule_set_inputs(ctx, run_log,
            {"onboardings": [_entry(second), _entry(first)]},
        )

    assert status == 0
    staged_names = sorted(
        path.name for path in _surface(tmp_path).inbox.glob("*.analysis.json")
    )
    assert len(staged_names) == 2
    assert any("__first__alpha.analysis.json" in name for name in staged_names)
    assert any("__second__alpha.analysis.json" in name for name in staged_names)
    for config in (first, second):
        assert (
            config.run_workspace
            / "onboardings"
            / config.name
            / "rule_set_input_manifest.json"
        ).is_file()


def test_preparation_performs_no_analyzer_or_materialization_call(
    tmp_path: Path,
) -> None:
    assert not hasattr(onboarding, "_run_registered_analyzer_workflow")
    assert not hasattr(onboarding, "_onboard_group")


def test_analyzer_artifacts_resolve_only_by_exact_lineage(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    surface.results.mkdir(parents=True)
    surface.candidates.mkdir(parents=True)
    input_name = "file_type_a__source__alpha.analysis.json"
    input_hash = "a" * 64
    candidate = surface.candidates / (
        f"{Path(input_name).stem}{surface.candidate_extension}"
    )
    candidate.write_text("{}\n", encoding="utf-8")
    exact = surface.results / "exact.context.json"
    exact.write_text(
        json.dumps(
            {
                "input_file": input_name,
                "input_hash": input_hash,
                "analysis_name": surface.analysis_name,
                "source_name": surface.source_name,
                "run_id": "run-1",
                "status": "success",
            }
        ),
        encoding="utf-8",
    )
    (surface.results / "wrong.context.json").write_text(
        json.dumps(
            {
                "input_file": input_name,
                "input_hash": "0" * 64,
                "analysis_name": surface.analysis_name,
                "source_name": surface.source_name,
                "run_id": "run-1",
                "status": "success",
            }
        ),
        encoding="utf-8",
    )

    resolved = onboarding._resolve_analyzer_artifacts(
        _ctx(tmp_path),
        surface,
        input_name=input_name,
        input_hash=input_hash,
    )

    assert resolved == onboarding._AnalyzerArtifacts(candidate, exact)


@pytest.mark.parametrize("matching_contexts", [0, 2])
def test_zero_or_multiple_matching_analyzer_contexts_fail_closed(
    tmp_path: Path,
    matching_contexts: int,
) -> None:
    surface = _surface(tmp_path)
    surface.results.mkdir(parents=True)
    surface.candidates.mkdir(parents=True)
    input_name = "file_type_a__source__alpha.analysis.json"
    input_hash = hashlib.sha256(b"analysis").hexdigest()
    (surface.candidates / f"{Path(input_name).stem}.candidate.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    lineage = {
        "input_file": input_name,
        "input_hash": input_hash,
        "analysis_name": surface.analysis_name,
        "source_name": surface.source_name,
        "run_id": "run-1",
        "status": "success",
    }
    for index in range(matching_contexts):
        (surface.results / f"{index}.context.json").write_text(
            json.dumps(lineage),
            encoding="utf-8",
        )

    with pytest.raises(InputError, match=f"found {matching_contexts}"):
        onboarding._resolve_analyzer_artifacts(
            _ctx(tmp_path),
            surface,
            input_name=input_name,
            input_hash=input_hash,
        )


def test_workflow_control_totals_are_calculated_for_every_source(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("ID,Amount\n1,10.50\n2,$2.00\n", encoding="utf-8")
    second.write_text("ID,Amount\n3,(1.25)\n4,4.00\n", encoding="utf-8")
    classification = _classification(tmp_path / "classification", (first, second))
    definition = onboarding.ControlTotalCalculation(
        "amount",
        "transactions",
        "Amount",
        "Amount",
        "0.00",
    )

    totals = onboarding._calculate_control_totals((definition,), classification)

    assert [item.value for item in totals[0].expected_values] == ["12.50", "2.75"]


def test_materialization_continues_groups_and_returns_aggregate_failure(run_log, 
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manifest = {
        "groups": [
            {"group_value": "Alpha"},
            {"group_value": "Zeta"},
        ]
    }
    completed: list[str] = []

    def materialize(
        _ctx: object,
        _run_log: object,
        _config: object,
        _surface: object,
        entry: dict[str, str],
        _materializer: object,
    ) -> None:
        if entry["group_value"] == "Alpha":
            raise InputError("candidate rejected")
        completed.append(entry["group_value"])

    with patch.object(
        onboarding,
        "resolve_rule_set_onboarding_configs",
        return_value=(config,),
    ), patch.object(
        onboarding,
        "_resolve_analyzer_surface",
        return_value=_surface(tmp_path),
    ), patch.object(
        onboarding,
        "_read_preparation_manifest",
        return_value=manifest,
    ), patch.object(
        onboarding,
        "_materialize_prepared_group",
        side_effect=materialize,
    ), patch.object(onboarding, "log_validation_result"), patch.object(
        onboarding,
        "log_row_count",
    ):
        status = onboarding.materialize_prepared_rule_sets(
            _ctx(tmp_path),
            run_log,
            {"onboardings": [_entry(config)]},
        )

    assert status == 1
    assert completed == ["Zeta"]


def test_identical_rerun_reuses_revision_without_activation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    existing_path = config.output_root / "statements_alpha__v0001.yaml"
    existing = SimpleNamespace(
        name="statements_alpha__v0001",
        version=1,
        rule_sha256="same",
    )
    generated_path = tmp_path / "materialized" / "statements_alpha__v0002.yaml"
    generated_path.parent.mkdir()
    generated_path.write_text("generated\n", encoding="utf-8")
    generated_rule = SimpleNamespace(
        name="statements_alpha__v0002",
        version=2,
        rule_sha256="new",
    )
    captured: list[object] = []

    def materializer(_ctx: object, request: object) -> MaterializationResult:
        captured.append(request)
        return MaterializationResult(
            yaml_path=generated_path,
            rule_set=generated_rule,
            rule_set_item={"name": generated_rule.name, "version": 2},
            validation=Mock(),
        )

    with patch.object(
        onboarding,
        "_load_history",
        return_value=((existing, existing_path),),
    ), patch.object(
        onboarding,
        "canonical_rule_sha256",
        return_value="same",
    ):
        result = onboarding._materialize_revision(
            _ctx(tmp_path),
            config,
            _classification(tmp_path / "classification"),
            onboarding._AnalyzerArtifacts(
                tmp_path / "candidate.json",
                tmp_path / "context.json",
            ),
            _surface(tmp_path),
            tmp_path / "group",
            "statements_alpha",
            (),
            materializer,
        )

    assert result.path == existing_path
    assert result.reused is True
    assert not generated_path.exists()
    assert captured[0].version == 2
    assert captured[0].approved_by == "data_engine_rule_set_onboarding"
