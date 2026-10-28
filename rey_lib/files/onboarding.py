"""Configuration-driven structural rule-set onboarding boundaries.

File Operator owns two public deterministic phases:

* preparation discovers and classifies configured filename groups, calculates
  trusted totals, and stages bounded analysis artifacts for Rey Analyzer;
* materialization resolves governed Analyzer artifacts by exact lineage,
  validates candidates, allocates historical revisions, and publishes inactive
  durable YAML.

Rey Analyzer executes between those phases as its own public pipeline step.
This module never invokes an LLM or activates a generated rule set.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from rey_lib.errors.error_utils import AppError
from rey_lib.encryption import sha256_file
from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.csv import parse_delimited_line
from rey_lib.files.json import write_json_file
from rey_lib.files.file_utils import copy_file, move_file, read_text_file, visible_files
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import (
    AppLogger,
    log_artifact_reference,
    log_row_count,
    log_validation_result,
)
from rey_lib.profiling import normalized_header
from rey_lib.workflow.workflow import Workflow

from rey_lib.files.error_utils import InputError, RedactorError
from rey_lib.files.feed_config import FEED_CONFIG_FILENAME
from rey_lib.files.file_types import classify_filenames
from rey_lib.files.processor import run_batch
from rey_lib.files.rule_set_materialization import (
    ControlTotal,
    ControlTotalExpectation,
    MaterializationRequest,
    MaterializationResult,
    materialize_file_operator_rule_set,
)
from rey_lib.files.rule_sets import (
    FileOperatorRuleSet,
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
)

__all__ = [
    "ControlTotalCalculation",
    "RuleSetOnboardingConfig",
    "materialize_prepared_rule_sets",
    "prepare_rule_set_inputs",
    "resolve_rule_set_onboarding_config",
    "resolve_rule_set_onboarding_configs",
]

_INLINE_ONBOARDINGS_FIELD = "onboardings"
_SYSTEM_APPROVER = "file_operator_rule_set_onboarding"
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_VERSIONED_FILE = re.compile(r"^(?P<name>.+)__v(?P<version>[0-9]{4,})[.]yaml$")
_PREPARATION_MANIFEST = "rule_set_input_manifest.json"
_TRUSTED_TOTALS = "trusted_control_totals.json"
_log = AppLogger(__name__)


@dataclass(frozen=True)
class ControlTotalCalculation:
    """Workflow-owned trusted source aggregation projected to ControlTotal."""

    name: str
    table: str
    column: str
    source_column: str
    tolerance: str
    applicability: str = "required"


@dataclass(frozen=True)
class RuleSetOnboardingConfig:
    """Validated reusable configuration for one grouped onboarding flow."""

    name: str
    enabled: bool
    source_inbox: Path
    filename_regex: str
    group_capture: str
    analyzer_workflow: str
    run_workspace: Path
    output_root: Path
    rule_set_name_pattern: str
    control_totals: tuple[ControlTotalCalculation, ...]


@dataclass(frozen=True)
class _AnalyzerSurface:
    workflow: str
    source_name: str
    analysis_name: str
    model_profile: str
    inbox: Path
    results: Path
    candidates: Path
    candidate_extension: str
    file_pattern: str


@dataclass(frozen=True)
class _Group:
    value: str
    slug: str
    files: tuple[Path, ...]


@dataclass(frozen=True)
class _ClassificationResult:
    file_type: str
    analysis_path: Path
    file_type_path: Path
    source_paths: tuple[Path, ...]
    delimiter: str
    encoding: str
    ordered_headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _AnalyzerArtifacts:
    candidate_path: Path
    context_path: Path


@dataclass(frozen=True)
class _PublishedRevision:
    path: Path
    rule_set: FileOperatorRuleSet
    reused: bool


Materializer = Callable[[Any, MaterializationRequest], MaterializationResult]


def resolve_rule_set_onboarding_config(
    entry: Mapping[str, Any],
) -> RuleSetOnboardingConfig:
    """Validate one process-local rule-set onboarding mapping."""

    if not isinstance(entry, Mapping):
        raise RedactorError("Each inline onboarding entry must be a mapping.")
    if "config_ref" in entry:
        raise RedactorError(
            "Rule-set onboarding config_ref is unsupported; supply the onboarding "
            "configuration inline."
        )
    name = str(_get(entry, "name", "") or "").strip()
    if not name:
        raise RedactorError("Every inline onboarding entry requires a non-empty name.")
    return _typed_onboarding_config(entry, name)


def resolve_rule_set_onboarding_configs(
    inline_config: Mapping[str, Any],
) -> tuple[RuleSetOnboardingConfig, ...]:
    """Validate enabled onboardings supplied by one workflow process mapping."""

    if not isinstance(inline_config, Mapping):
        raise RedactorError(
            "Rule-set onboarding requires an inline process configuration mapping."
        )
    if "config_ref" in inline_config:
        raise RedactorError(
            "Rule-set onboarding config_ref is unsupported; supply an 'onboardings' "
            "list inline."
        )
    entries = inline_config.get(_INLINE_ONBOARDINGS_FIELD)
    if not isinstance(entries, list):
        raise RedactorError(
            "Rule-set onboarding inline configuration requires an 'onboardings' list."
        )
    configs = tuple(
        sorted(
            (resolve_rule_set_onboarding_config(entry) for entry in entries),
            key=lambda item: item.name,
        )
    )
    names = [item.name for item in configs]
    if len(names) != len(set(names)):
        raise RedactorError(
            "Rule-set onboarding inline configuration contains duplicate names."
        )
    enabled = tuple(item for item in configs if item.enabled)
    if not enabled:
        raise RedactorError(
            "Rule-set onboarding inline configuration has no enabled entries."
        )
    return enabled


def _typed_onboarding_config(entry: Any, name: str) -> RuleSetOnboardingConfig:
    classification = _required_mapping(entry, "filename_classification", name)
    analyzer = _required_mapping(entry, "analyzer", name)
    raw_controls = _required_list(entry, "control_totals", name)
    controls = tuple(
        _control_total_config(item, name, index)
        for index, item in enumerate(raw_controls)
    )
    if not controls:
        raise RedactorError(
            f"Rule-set onboarding '{name}' requires at least one control total."
        )

    filename_regex = _required_string(classification, "regex", name)
    try:
        compiled = re.compile(filename_regex)
    except re.error as exc:
        raise RedactorError(
            f"Rule-set onboarding '{name}' filename regex is invalid: {exc}"
        ) from exc
    group_capture = _required_string(classification, "group_capture", name)
    if group_capture not in compiled.groupindex:
        raise RedactorError(
            f"Rule-set onboarding '{name}' group_capture '{group_capture}' "
            "is not a named capture in filename_classification.regex."
        )

    pattern = _required_string(entry, "rule_set_name_pattern", name)
    _validate_name_pattern(pattern, name)
    return RuleSetOnboardingConfig(
        name=name,
        enabled=_required_bool(entry, "enabled", name),
        source_inbox=_required_path(entry, "source_inbox", name),
        filename_regex=filename_regex,
        group_capture=group_capture,
        analyzer_workflow=_required_string(analyzer, "workflow", name),
        run_workspace=_required_path(entry, "run_workspace", name),
        output_root=_required_path(entry, "output_root", name),
        rule_set_name_pattern=pattern,
        control_totals=controls,
    )


def prepare_rule_set_inputs(ctx: Any, run_log, inline_config: Mapping[str, Any]) -> int:
    """Prepare every configured group for a separate Rey Analyzer step."""

    configs = resolve_rule_set_onboarding_configs(inline_config)
    workflow_names = {
        config.analyzer_workflow for config in configs if config.enabled
    }
    if len(workflow_names) != 1:
        raise RedactorError(
            "One preparation run must resolve exactly one Analyzer workflow; "
            f"found {len(workflow_names)}: {', '.join(sorted(workflow_names))}."
        )
    surface = _resolve_analyzer_surface(ctx, next(iter(workflow_names)))
    _assert_analyzer_inbox_clear(surface)

    prepared_count = 0
    failed_count = 0
    staged_names: set[str] = set()

    for config in configs:
        if not config.enabled:
            log_validation_result(run_log,
                validation_name="rule_set_input_preparation_config",
                status="skipped",
                message=f"Rule-set onboarding '{config.name}' is disabled.",
                onboarding_name=config.name,
            )
            continue

        # A configuration that cannot be discovered fails on its own; later
        # configurations still run.
        try:
            if not config.source_inbox.is_dir():
                raise RedactorError(
                    f"Rule-set onboarding inbox does not exist: '{config.source_inbox}'."
                )
            groups = _discover_groups(config)
        except (RedactorError, OSError) as exc:
            failed_count += 1
            _record_preparation_failure(ctx, run_log, config.name, "", exc)
            continue

        log_row_count(run_log,
            count_name="onboarding_filename_groups",
            count=len(groups),
            subject=config.name,
        )

        entries: list[dict[str, Any]] = []
        for group in groups:
            # One lifecycle boundary per discovered group. Everything from
            # classification through Analyzer staging succeeds or fails together
            # for that group alone, so an unrelated group is never abandoned.
            staged_path: Path | None = None
            try:
                group_root = (
                    config.run_workspace / "onboardings" / config.name / "groups" / group.slug
                )
                classification = _classify_group(
                    ctx, run_log,
                    config,
                    group,
                    group_root / "classification",
                )
                trusted_path = _write_trusted_total_evidence(ctx, run_log,
                    config,
                    group,
                    classification,
                    group_root / _TRUSTED_TOTALS,
                )
                staged_name = _staged_analysis_name(config, group, classification)
                if staged_name in staged_names:
                    raise InputError(
                        "Configured onboarding entries produced duplicate staged "
                        f"Analyzer artifact name '{staged_name}'."
                    )
                entry = _preparation_entry(
                    config,
                    group,
                    classification,
                    trusted_path,
                    surface,
                    staged_name,
                )
                staged_path = _stage_analyzer_input(
                    ctx,
                    Path(str(entry["analysis_artifact"]["path"])),
                    surface,
                    dest_name=staged_name,
                )
                if _sha256(staged_path) != str(entry["staged_analysis"]["sha256"]):
                    raise InputError(
                        f"Staged Analyzer input hash changed for onboarding "
                        f"'{config.name}' group '{group.value}'."
                    )
            except (RedactorError, OSError) as exc:
                # The group's own staged artifact is withdrawn so the Analyzer
                # inbox never carries an input with no prepared entry. Artifacts
                # staged by other groups are theirs and are left untouched.
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)
                failed_count += 1
                _record_preparation_failure(ctx, run_log, config.name, group.value, exc)
                continue

            staged_names.add(staged_name)
            entries.append(entry)
            prepared_count += 1
            log_validation_result(run_log,
                validation_name="rule_set_input_preparation_group",
                status="passed",
                message=(
                    f"Prepared onboarding '{config.name}' filename group "
                    f"'{group.value}' for Analyzer."
                ),
                onboarding_name=config.name,
                group_value=group.value,
                file_type=entry["file_type"],
                staged_analysis=str(staged_path),
            )

        # The manifest records only the groups this configuration actually
        # prepared, so materialization never expects Analyzer output for a
        # group that failed here.
        _write_preparation_manifest(ctx, run_log, config, surface, entries)

    log_row_count(run_log,
        count_name="rule_set_analysis_inputs_prepared",
        count=prepared_count,
        subject="prepare_rule_set_inputs",
    )
    log_row_count(run_log,
        count_name="rule_set_analysis_inputs_failed",
        count=failed_count,
        subject="prepare_rule_set_inputs",
    )
    return 1 if failed_count else 0


def _record_preparation_failure(
    ctx: Any, run_log,
    onboarding_name: str,
    group_value: str,
    exc: Exception,
) -> None:
    """Record one failed preparation scope without stopping the remaining work."""
    log_validation_result(run_log,
        validation_name="rule_set_input_preparation_group",
        status="failed",
        message=str(exc),
        onboarding_name=onboarding_name,
        group_value=group_value or "<configuration>",
    )


def materialize_prepared_rule_sets(
    ctx: Any, run_log,
    inline_config: Mapping[str, Any],
    *,
    materializer: Materializer = materialize_file_operator_rule_set,
) -> int:
    """Materialize all prepared governed candidates without LLM execution."""

    configs = resolve_rule_set_onboarding_configs(inline_config)
    failed = 0
    completed = 0
    for config in configs:
        if not config.enabled:
            continue
        surface = _resolve_analyzer_surface(ctx, config.analyzer_workflow)
        manifest = _read_preparation_manifest(config, surface)
        for raw_entry in manifest["groups"]:
            entry = _mapping(raw_entry, "preparation manifest group")
            group_value = _required_string(entry, "group_value", config.name)
            try:
                _materialize_prepared_group(ctx, run_log,
                    config,
                    surface,
                    entry,
                    materializer,
                )
                completed += 1
                log_validation_result(run_log,
                    validation_name="rule_set_materialization_group",
                    status="passed",
                    message=(
                        f"Materialized onboarding '{config.name}' filename "
                        f"group '{group_value}'."
                    ),
                    onboarding_name=config.name,
                    group_value=group_value,
                )
            except Exception as exc:  # noqa: BLE001 - preserve independent evidence
                failed += 1
                # Handled -- counted, recorded, the next group runs -- so
                # reported here (rule 79).
                error = exc if isinstance(exc, AppError) else RedactorError(
                    f"materialization failed for group '{group_value}'")
                if error is not exc:
                    error.__cause__ = exc
                error.report(
                    f"materializing onboarding {config.name} group {group_value}",
                    source=__name__,
                )
                log_validation_result(run_log,
                    validation_name="rule_set_materialization_group",
                    status="failed",
                    message="Rule-set materialization failed.",
                    onboarding_name=config.name,
                    group_value=group_value,
                )
    log_row_count(run_log,
        count_name="rule_sets_materialized",
        count=completed,
        subject="materialize_prepared_rule_sets",
    )
    log_row_count(run_log,
        count_name="rule_set_groups_failed",
        count=failed,
        subject="materialize_prepared_rule_sets",
    )
    return 1 if failed else 0


def _materialize_prepared_group(
    ctx: Any, run_log,
    config: RuleSetOnboardingConfig,
    surface: _AnalyzerSurface,
    entry: Mapping[str, Any],
    materializer: Materializer,
) -> _PublishedRevision:
    group = _group_from_preparation_entry(config, entry)
    classification = _classification_from_preparation_entry(entry, group)
    staged = _mapping(entry.get("staged_analysis"), "staged analysis")
    staged_name = _required_string(staged, "name", "staged analysis")
    staged_hash = _required_string(staged, "sha256", "staged analysis")
    if staged_hash != _sha256(classification.analysis_path):
        raise InputError(
            f"Prepared analysis lineage changed for onboarding '{config.name}' "
            f"group '{group.value}'."
        )
    analyzer_artifacts = _resolve_analyzer_artifacts(
        ctx,
        surface,
        input_name=staged_name,
        input_hash=staged_hash,
    )
    group_root = config.run_workspace / "onboardings" / config.name / "groups" / group.slug
    analyzer_artifacts = _preserve_analyzer_artifacts(
        ctx,
        analyzer_artifacts,
        group_root / "analyzer",
    )
    controls = _control_totals_from_evidence(
        Path(_required_string(entry, "trusted_control_totals", config.name)),
        analyzer_artifacts.candidate_path,
        onboarding_name=config.name,
        group_value=group.value,
        file_type=classification.file_type,
        analysis_sha256=staged_hash,
        source_files=tuple(path.name for path in classification.source_paths),
    )
    published = _materialize_revision(
        ctx,
        config,
        classification,
        analyzer_artifacts,
        surface,
        group_root,
        _render_rule_set_name(config, group),
        controls,
        materializer,
    )
    log_artifact_reference(run_log,
        str(published.path),
        role="generated_file_operator_rule_set",
        producer="file_operator",
        artifact_group="rule_sets",
        producing_app="file_operator",
        artifact_type="file_operator_rule_set",
        viewer_type="file",
        safe_to_preview=True,
        rule_set_name=published.rule_set.name,
        rule_sha256=published.rule_set.rule_sha256,
        revision_reused=published.reused,
        onboarding_name=config.name,
        filename_group=group.value,
    )
    return published


def _discover_groups(config: RuleSetOnboardingConfig) -> tuple[_Group, ...]:
    files = [
        path
        for path in visible_files(config.source_inbox, recursive=False)
        if path.name != FEED_CONFIG_FILENAME
    ]
    if not files:
        raise InputError(
            f"Rule-set onboarding found no files in '{config.source_inbox}'."
        )
    classifications = classify_filenames(
        [path.name for path in files],
        config.filename_regex,
    )
    files_by_name = {path.name: path for path in files}
    grouped: dict[str, list[Path]] = {}
    for item in classifications:
        value = str(item["values"].get(config.group_capture) or "").strip()
        if not value:
            raise InputError(
                f"Filename classification '{item['filename']}' produced an empty "
                f"'{config.group_capture}' value."
            )
        grouped.setdefault(value, []).append(files_by_name[item["filename"]])

    slugs: dict[str, str] = {}
    groups: list[_Group] = []
    for value in sorted(grouped):
        slug = _slug(value)
        prior = slugs.get(slug)
        if prior is not None and prior != value:
            raise InputError(
                f"Filename group values '{prior}' and '{value}' normalize to "
                f"the same workspace key '{slug}'."
            )
        slugs[slug] = value
        groups.append(
            _Group(
                value=value,
                slug=slug,
                files=tuple(sorted(grouped[value], key=lambda path: path.name)),
            )
        )
    return tuple(groups)


def _classify_group(
    ctx: Any, run_log,
    config: RuleSetOnboardingConfig,
    group: _Group,
    output_dir: Path,
) -> _ClassificationResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(
        layout="delimited",
        output_dir=str(output_dir),
        processed_path=None,
        rejected_path=None,
        delimiter=None,
        encoding=None,
        sample_size=None,
        redact_columns=None,
        redact_all=True,
        redact_masks={},
        skip_blank_lines=False,
        header_contains=None,
        drop_ragged_rows=False,
        classification_filename_regex=config.filename_regex,
    )
    if run_batch(list(group.files), run_log, ctx, args) != 0:
        raise InputError(f"Classification failed for filename group '{group.value}'.")

    manifest = _read_json_object(
        output_dir / "file_type_analysis_manifest.json",
        "File Operator analysis manifest",
    )
    entries = manifest.get("file_types")
    if not isinstance(entries, list) or len(entries) != 1:
        count = len(entries) if isinstance(entries, list) else 0
        raise InputError(
            f"Filename group '{group.value}' produced {count} structural "
            "identities; exactly one is required."
        )
    entry = _mapping(entries[0], "analysis manifest file type")
    analysis_path = output_dir / _required_string(
        entry,
        "analysis_artifact",
        "analysis manifest",
    )
    file_type_path = output_dir / _required_string(
        entry,
        "file_type_artifact",
        "analysis manifest",
    )
    file_type = _read_json_object(file_type_path, "file-type artifact")
    expected_names = [path.name for path in group.files]
    if file_type.get("matching_files") != expected_names:
        raise InputError(
            f"Filename group '{group.value}' file-type coverage does not match "
            "the discovered group."
        )
    for item in file_type.get("filename_classifications") or []:
        values = _mapping(item, "filename classification").get("values")
        if (
            not isinstance(values, Mapping)
            or values.get(config.group_capture) != group.value
        ):
            raise InputError(
                f"Filename group '{group.value}' contains conflicting "
                "classification evidence."
            )

    delimiter, encoding = _inspection_format(output_dir, file_type)
    return _ClassificationResult(
        file_type=_required_string(file_type, "file_type", "file-type artifact"),
        analysis_path=analysis_path,
        file_type_path=file_type_path,
        source_paths=group.files,
        delimiter=delimiter,
        encoding=encoding,
        ordered_headers=tuple(
            _required_string_list(
                file_type,
                "ordered_headers",
                "file-type artifact",
            )
        ),
    )


def _inspection_format(
    output_dir: Path,
    file_type: Mapping[str, Any],
) -> tuple[str, str]:
    formats: set[tuple[str, str]] = set()
    for name in file_type.get("inspection_artifacts") or []:
        path = output_dir / str(name)
        first_line = read_text_file(path, encoding="utf-8").splitlines()[0]
        metadata = json.loads(first_line)
        formats.add((str(metadata["delimiter"]), str(metadata["encoding"])))
    if len(formats) != 1:
        raise InputError("Structural group inspection formats are inconsistent.")
    return next(iter(formats))


def _staged_analysis_name(
    config: RuleSetOnboardingConfig,
    group: _Group,
    classification: _ClassificationResult,
) -> str:
    stem = classification.analysis_path.name.removesuffix(".analysis.json")
    return f"{stem}__{_slug(config.name)}__{group.slug}.analysis.json"


def _preparation_entry(
    config: RuleSetOnboardingConfig,
    group: _Group,
    classification: _ClassificationResult,
    trusted_path: Path,
    surface: _AnalyzerSurface,
    staged_name: str,
) -> dict[str, Any]:
    return {
        "onboarding_name": config.name,
        "group_value": group.value,
        "group_slug": group.slug,
        "file_type": classification.file_type,
        "source_files": [
            {
                "name": path.name,
                "path": str(path),
                "sha256": _sha256(path),
            }
            for path in classification.source_paths
        ],
        "analysis_artifact": {
            "path": str(classification.analysis_path),
            "sha256": _sha256(classification.analysis_path),
        },
        "file_type_artifact": {
            "path": str(classification.file_type_path),
            "sha256": _sha256(classification.file_type_path),
        },
        "staged_analysis": {
            "name": staged_name,
            "sha256": _sha256(classification.analysis_path),
        },
        "trusted_control_totals": str(trusted_path),
        "analyzer": {
            "workflow": surface.workflow,
            "source": surface.source_name,
            "analysis": surface.analysis_name,
            "model_profile": surface.model_profile,
        },
    }


def _write_preparation_manifest(
    ctx: Any, run_log,
    config: RuleSetOnboardingConfig,
    surface: _AnalyzerSurface,
    groups: list[dict[str, Any]],
) -> Path:
    path = (
        config.run_workspace
        / "onboardings"
        / config.name
        / _PREPARATION_MANIFEST
    )
    payload = {
        "schema_version": 1,
        "onboarding_name": config.name,
        "analyzer": {
            "workflow": surface.workflow,
            "source": surface.source_name,
            "analysis": surface.analysis_name,
            "model_profile": surface.model_profile,
        },
        "groups": groups,
    }
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    log_artifact_reference(run_log,
        str(path),
        role="rule_set_input_manifest",
        producer="file_operator",
        artifact_group="rule_set_preparation",
        producing_app="file_operator",
        artifact_type="json",
        viewer_type="json",
        safe_to_preview=True,
        onboarding_name=config.name,
        group_count=len(groups),
        sha256=_sha256(path),
    )
    return path


def _read_preparation_manifest(
    config: RuleSetOnboardingConfig,
    surface: _AnalyzerSurface,
) -> dict[str, Any]:
    path = (
        config.run_workspace
        / "onboardings"
        / config.name
        / _PREPARATION_MANIFEST
    )
    manifest = _read_json_object(path, "rule-set input manifest")
    if manifest.get("schema_version") != 1:
        raise InputError(f"Unsupported preparation manifest version in '{path}'.")
    if manifest.get("onboarding_name") != config.name:
        raise InputError(f"Preparation manifest onboarding identity changed: '{path}'.")
    analyzer = _mapping(manifest.get("analyzer"), "preparation manifest analyzer")
    expected = {
        "workflow": surface.workflow,
        "source": surface.source_name,
        "analysis": surface.analysis_name,
        "model_profile": surface.model_profile,
    }
    if dict(analyzer) != expected:
        raise InputError(
            f"Preparation manifest Analyzer routing no longer matches configuration: "
            f"'{path}'."
        )
    groups = manifest.get("groups")
    if not isinstance(groups, list) or not groups:
        raise InputError(f"Preparation manifest '{path}' has no groups.")
    keys = [
        (
            str(_get(item, "onboarding_name", "")),
            str(_get(item, "group_value", "")),
        )
        for item in groups
    ]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise InputError(
            f"Preparation manifest '{path}' groups are not unique and ordered."
        )
    return manifest


def _write_trusted_total_evidence(
    ctx: Any, run_log,
    config: RuleSetOnboardingConfig,
    group: _Group,
    classification: _ClassificationResult,
    path: Path,
) -> Path:
    applicable = _applicable_control_totals(
        config.control_totals,
        classification,
    )
    if not applicable:
        raise InputError(
            "No configured control-total column applies to the structural group."
        )
    controls = []
    for definition in applicable:
        controls.append(
            {
                "name": definition.name,
                "table": definition.table,
                "column": definition.column,
                "source_column": definition.source_column,
                "tolerance": definition.tolerance,
                "expected_values": [
                    {
                        "source_file": source.name,
                        "value": _sum_source_column(
                            source,
                            definition.source_column,
                            classification.delimiter,
                            classification.encoding,
                        ),
                    }
                    for source in classification.source_paths
                ],
            }
        )
    payload = {
        "schema_version": 1,
        "onboarding_name": config.name,
        "group_value": group.value,
        "file_type": classification.file_type,
        "analysis_sha256": _sha256(classification.analysis_path),
        "controls": controls,
    }
    write_json_file(path, payload, mode="pretty")
    log_artifact_reference(run_log,
        str(path),
        role="trusted_control_totals",
        producer="file_operator",
        artifact_group="rule_set_preparation",
        producing_app="file_operator",
        artifact_type="json",
        viewer_type="json",
        safe_to_preview=True,
        onboarding_name=config.name,
        filename_group=group.value,
        file_type=classification.file_type,
        sha256=_sha256(path),
    )
    return path


def _group_from_preparation_entry(
    config: RuleSetOnboardingConfig,
    entry: Mapping[str, Any],
) -> _Group:
    if _required_string(entry, "onboarding_name", config.name) != config.name:
        raise InputError("Prepared group onboarding identity changed.")
    value = _required_string(entry, "group_value", config.name)
    slug = _required_string(entry, "group_slug", config.name)
    if slug != _slug(value):
        raise InputError(f"Prepared filename group slug changed for '{value}'.")
    raw_sources = entry.get("source_files")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise InputError(f"Prepared filename group '{value}' has no source files.")
    files: list[Path] = []
    for index, raw_source in enumerate(raw_sources):
        source = _mapping(raw_source, f"prepared source_files[{index}]")
        path = Path(_required_string(source, "path", f"source_files[{index}]")).resolve()
        if path.parent != config.source_inbox:
            raise InputError(
                f"Prepared source '{path}' is outside configured inbox "
                f"'{config.source_inbox}'."
            )
        if path.name != _required_string(source, "name", f"source_files[{index}]"):
            raise InputError(f"Prepared source filename identity changed: '{path}'.")
        if not path.is_file() or _sha256(path) != _required_string(
            source,
            "sha256",
            f"source_files[{index}]",
        ):
            raise InputError(f"Prepared source lineage changed: '{path}'.")
        match = re.fullmatch(config.filename_regex, path.name)
        if match is None or match.group(config.group_capture) != value:
            raise InputError(
                f"Prepared source '{path.name}' no longer belongs to group '{value}'."
            )
        files.append(path)
    if files != sorted(files, key=lambda item: item.name):
        raise InputError(f"Prepared sources for group '{value}' are not ordered.")
    return _Group(value=value, slug=slug, files=tuple(files))


def _classification_from_preparation_entry(
    entry: Mapping[str, Any],
    group: _Group,
) -> _ClassificationResult:
    analysis = _mapping(entry.get("analysis_artifact"), "analysis artifact")
    file_type_artifact = _mapping(
        entry.get("file_type_artifact"),
        "file-type artifact",
    )
    analysis_path = Path(_required_string(analysis, "path", "analysis artifact"))
    file_type_path = Path(
        _required_string(file_type_artifact, "path", "file-type artifact")
    )
    if not analysis_path.is_file() or _sha256(analysis_path) != _required_string(
        analysis,
        "sha256",
        "analysis artifact",
    ):
        raise InputError(f"Prepared analysis artifact lineage changed: '{analysis_path}'.")
    if not file_type_path.is_file() or _sha256(file_type_path) != _required_string(
        file_type_artifact,
        "sha256",
        "file-type artifact",
    ):
        raise InputError(f"Prepared file-type artifact lineage changed: '{file_type_path}'.")
    file_type = _read_json_object(file_type_path, "file-type artifact")
    identity = _required_string(file_type, "file_type", "file-type artifact")
    if identity != _required_string(entry, "file_type", "preparation manifest"):
        raise InputError("Prepared structural identity changed.")
    if file_type.get("matching_files") != [path.name for path in group.files]:
        raise InputError("Prepared structural group source coverage changed.")
    delimiter, encoding = _inspection_format(file_type_path.parent, file_type)
    return _ClassificationResult(
        file_type=identity,
        analysis_path=analysis_path,
        file_type_path=file_type_path,
        source_paths=group.files,
        delimiter=delimiter,
        encoding=encoding,
        ordered_headers=tuple(
            _required_string_list(
                file_type,
                "ordered_headers",
                "file-type artifact",
            )
        ),
    )


def _control_totals_from_evidence(
    path: Path,
    candidate_path: Path,
    *,
    onboarding_name: str,
    group_value: str,
    file_type: str,
    analysis_sha256: str,
    source_files: tuple[str, ...],
) -> tuple[ControlTotal, ...]:
    evidence = _read_json_object(path, "trusted control-total evidence")
    if evidence.get("schema_version") != 1:
        raise InputError(f"Unsupported trusted control-total evidence: '{path}'.")
    expected_identity = {
        "onboarding_name": onboarding_name,
        "group_value": group_value,
        "file_type": file_type,
        "analysis_sha256": analysis_sha256,
    }
    actual_identity = {
        key: evidence.get(key) for key in expected_identity
    }
    if actual_identity != expected_identity:
        raise InputError(
            f"Trusted control-total evidence lineage changed: '{path}'."
        )
    raw_controls = evidence.get("controls")
    if not isinstance(raw_controls, list) or not raw_controls:
        raise InputError(f"Trusted control-total evidence has no controls: '{path}'.")
    candidate = _read_json_object(candidate_path, "Analyzer candidate")
    controls: list[ControlTotal] = []
    for index, raw_control in enumerate(raw_controls):
        item = _mapping(raw_control, f"trusted controls[{index}]")
        definition = ControlTotalCalculation(
            name=_required_string(item, "name", f"trusted controls[{index}]"),
            table=_required_string(item, "table", f"trusted controls[{index}]"),
            column=_required_string(item, "column", f"trusted controls[{index}]"),
            source_column=_required_string(
                item,
                "source_column",
                f"trusted controls[{index}]",
            ),
            tolerance=_required_string(
                item,
                "tolerance",
                f"trusted controls[{index}]",
            ),
        )
        raw_values = item.get("expected_values")
        if not isinstance(raw_values, list) or not raw_values:
            raise InputError(
                f"Trusted controls[{index}] requires expected_values."
            )
        expected_values = tuple(
            ControlTotalExpectation(
                source_file=_required_string(
                    _mapping(value, "trusted expected value"),
                    "source_file",
                    "trusted expected value",
                ),
                value=_required_string(
                    _mapping(value, "trusted expected value"),
                    "value",
                    "trusted expected value",
                ),
            )
            for value in raw_values
        )
        if tuple(value.source_file for value in expected_values) != source_files:
            raise InputError(
                f"Trusted control-total source coverage changed: '{path}'."
            )
        controls.append(
            ControlTotal(
                name=definition.name,
                table=_control_total_table(definition, candidate),
                column=definition.column,
                tolerance=definition.tolerance,
                expected_values=expected_values,
            )
        )
    return tuple(controls)


def _resolve_analyzer_surface(ctx: Any, workflow_name: str) -> _AnalyzerSurface:
    # The Analyzer workflow is the governed Workflow of that name, hydrated
    # through its own governed boundary (backlog 1011).
    workflow = Workflow.from_governed(ctx, workflow_name)
    analysis_steps = [
        step for step in workflow.steps.values()
        if step.process.application.name == "rey_analyzer"
        and step.operation.name == "analysis"
    ]
    if len(analysis_steps) != 1:
        raise RedactorError(
            f"Analyzer workflow '{workflow_name}' must contain exactly one "
            "rey_analyzer analysis step."
        )
    step_config = _mapping(
        analysis_steps[0].settings,
        "Analyzer workflow step settings",
    )
    source_name = _required_string(step_config, "source", workflow_name)
    # The data source and its analysis config are part of the analysis step's
    # process configuration: its WorkflowProcess holds both (#1032, #1043, #1005).
    process = analysis_steps[0].process
    source = process.data_source(source_name)
    analysis_name = str(_get(source, "analysis_config", "") or "")
    analysis = process.analysis_config(analysis_name)
    paths = _mapping(_get(source, "paths", None), "Analyzer source paths")
    return _AnalyzerSurface(
        workflow=workflow_name,
        source_name=source_name,
        analysis_name=analysis_name,
        model_profile=_required_string(
            _mapping(analysis, "Analyzer analysis"),
            "llm_execution_profile",
            analysis_name,
        ),
        inbox=_required_path(paths, "inbox_path", source_name),
        results=_required_path(paths, "results_path", source_name),
        candidates=_required_path(paths, "raw_output_path", source_name),
        candidate_extension=str(
            _get(source, "raw_output_extension", ".candidate.json") or ".candidate.json"
        ),
        file_pattern=str(_get(source, "file_pattern", "*") or "*"),
    )


def _stage_analyzer_input(
    ctx: Any,
    analysis_path: Path,
    surface: _AnalyzerSurface,
    *,
    dest_name: str,
) -> Path:
    pattern = surface.file_pattern
    if not fnmatch.fnmatch(dest_name, pattern):
        raise InputError(
            f"Staged analysis artifact '{dest_name}' does not match Analyzer "
            f"source pattern '{pattern}'."
        )
    destination = surface.inbox / dest_name
    if destination.exists():
        raise InputError(
            f"Analyzer input destination already exists: '{destination}'."
        )
    return copy_file(
        analysis_path,
        surface.inbox,
        dest_name=dest_name,
        state_ctx=ctx,
        app="file_operator",
        pipeline=getattr(ctx, "pipeline_name", None),
        reason="analyzer_input",
    )


def _assert_analyzer_inbox_clear(surface: _AnalyzerSurface) -> None:
    existing = sorted(surface.inbox.glob(surface.file_pattern)) if surface.inbox.exists() else []
    if existing:
        raise InputError(
            f"Analyzer inbox for '{surface.source_name}' is not empty: "
            + ", ".join(path.name for path in existing)
        )


def _resolve_analyzer_artifacts(
    ctx: Any,
    surface: _AnalyzerSurface,
    *,
    input_name: str,
    input_hash: str,
) -> _AnalyzerArtifacts:
    run_id = str(getattr(ctx, "run_id", "") or "")
    contexts: list[Path] = []
    if surface.results.exists():
        for path in sorted(surface.results.glob("*.context.json")):
            value = _read_json_object(path, "Analyzer context")
            if (
                Path(str(value.get("input_file") or "")).name == input_name
                and value.get("input_hash") == input_hash
                and value.get("analysis_name") == surface.analysis_name
                and value.get("source_name") == surface.source_name
                and (not run_id or value.get("run_id") == run_id)
                and value.get("status") == "success"
            ):
                contexts.append(path)
    if len(contexts) != 1:
        raise InputError(
            f"Analyzer lineage resolution for '{input_name}' found "
            f"{len(contexts)} matching context artifacts; exactly one is required."
        )
    candidate = surface.candidates / (
        f"{Path(input_name).stem}{surface.candidate_extension}"
    )
    if not candidate.is_file():
        raise InputError(
            f"Analyzer candidate for '{input_name}' was not found: "
            f"'{candidate}'."
        )
    return _AnalyzerArtifacts(candidate_path=candidate, context_path=contexts[0])


def _preserve_analyzer_artifacts(
    ctx: Any,
    artifacts: _AnalyzerArtifacts,
    destination: Path,
) -> _AnalyzerArtifacts:
    """Copy Analyzer-owned artifacts into File Operator lineage evidence."""

    candidate = copy_file(
        artifacts.candidate_path,
        destination,
        state_ctx=ctx,
        app="file_operator",
        pipeline=getattr(ctx, "pipeline_name", None),
        reason="analyzer_candidate_lineage",
    )
    context = copy_file(
        artifacts.context_path,
        destination,
        state_ctx=ctx,
        app="file_operator",
        pipeline=getattr(ctx, "pipeline_name", None),
        reason="analyzer_context_lineage",
    )
    return _AnalyzerArtifacts(candidate_path=candidate, context_path=context)


def _calculate_control_totals(
    definitions: tuple[ControlTotalCalculation, ...],
    classification: _ClassificationResult,
    candidate_path: Path | None = None,
) -> tuple[ControlTotal, ...]:
    applicable = _applicable_control_totals(definitions, classification)
    if not applicable:
        raise InputError(
            "No configured control-total column applies to the structural group."
        )
    candidate = (
        _read_json_object(candidate_path, "Analyzer candidate")
        if candidate_path is not None
        else None
    )
    return tuple(
        ControlTotal(
            name=definition.name,
            table=_control_total_table(definition, candidate),
            column=definition.column,
            tolerance=definition.tolerance,
            expected_values=tuple(
                ControlTotalExpectation(
                    source_file=source.name,
                    value=_sum_source_column(
                        source,
                        definition.source_column,
                        classification.delimiter,
                        classification.encoding,
                    ),
                )
                for source in classification.source_paths
            ),
        )
        for definition in applicable
    )


def _applicable_control_totals(
    definitions: tuple[ControlTotalCalculation, ...],
    classification: _ClassificationResult,
) -> tuple[ControlTotalCalculation, ...]:
    headers = set(normalized_header(classification.ordered_headers))
    applicable: list[ControlTotalCalculation] = []
    for definition in definitions:
        present = normalized_header([definition.source_column])[0] in headers
        if definition.applicability == "when_present":
            if present:
                applicable.append(definition)
            continue
        if not present:
            raise InputError(
                f"Configured control-total column '{definition.source_column}' "
                "is absent from the structural group's authoritative header."
            )
        applicable.append(definition)
    return tuple(applicable)


def _control_total_table(
    definition: ControlTotalCalculation,
    candidate: Mapping[str, Any] | None,
) -> str:
    if definition.table != "auto":
        return definition.table
    if candidate is None:
        raise InputError(
            f"Control total '{definition.name}' requires an Analyzer candidate "
            "to resolve table: auto."
        )
    raw_tables = candidate.get("tables")
    if not isinstance(raw_tables, list):
        raise InputError("Analyzer candidate tables must be a list.")
    matches: list[str] = []
    wanted = normalized_header([definition.column])[0]
    for index, raw_table in enumerate(raw_tables):
        table = _mapping(raw_table, f"Analyzer candidate tables[{index}]")
        name = table.get("name")
        columns = table.get("expected_columns")
        if (
            isinstance(name, str)
            and name
            and isinstance(columns, list)
            and wanted in normalized_header([str(value) for value in columns])
        ):
            matches.append(name)
    if len(matches) != 1:
        raise InputError(
            f"Control total '{definition.name}' table:auto matched "
            f"{len(matches)} candidate tables; exactly one is required."
        )
    return matches[0]


def _sum_source_column(
    source: Path,
    column: str,
    delimiter: str,
    encoding: str,
) -> str:
    text = read_text_file(source, encoding=encoding)
    rows = [
        parse_delimited_line(line, delimiter)
        for line in text.splitlines()
    ]
    wanted = normalized_header([column])[0]
    header_index = next(
        (index for index, row in enumerate(rows) if wanted in normalized_header(row)),
        None,
    )
    if header_index is None:
        raise InputError(
            f"Configured control-total column '{column}' is absent from "
            f"'{source.name}'."
        )
    header = normalized_header(rows[header_index])
    column_index = header.index(wanted)
    total = Decimal("0")
    found = False
    for physical_row in rows[header_index + 1 :]:
        if not any(value.strip() for value in physical_row):
            continue
        if normalized_header(physical_row) == header:
            continue
        if column_index >= len(physical_row):
            continue
        raw = physical_row[column_index].strip()
        if not raw:
            continue
        try:
            value = _decimal(raw)
        except InvalidOperation as exc:
            raise InputError(
                f"Trusted control total '{column}' cannot be calculated for "
                f"'{source.name}': nonnumeric value '{raw}'."
            ) from exc
        total += value
        found = True
    if not found:
        raise InputError(
            f"Trusted control total '{column}' has no numeric values in "
            f"'{source.name}'."
        )
    return format(total, "f")


def _decimal(value: str) -> Decimal:
    normalized = value.replace(",", "").replace("$", "").strip()
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    return Decimal(normalized)


def _materialize_revision(
    ctx: Any,
    config: RuleSetOnboardingConfig,
    classification: _ClassificationResult,
    analyzer: _AnalyzerArtifacts,
    surface: _AnalyzerSurface,
    group_root: Path,
    rule_set_name: str,
    controls: tuple[ControlTotal, ...],
    materializer: Materializer,
) -> _PublishedRevision:
    history = _load_history(config.output_root, rule_set_name)
    next_version = max((item.version for item, _path in history), default=0) + 1
    materialization_root = group_root / "materialization"
    request = MaterializationRequest(
        rule_set_name=rule_set_name,
        version=next_version,
        analysis_artifact_path=classification.analysis_path,
        candidate_rules_path=analyzer.candidate_path,
        analyzer_context_path=analyzer.context_path,
        model_profile=surface.model_profile,
        source_paths=classification.source_paths,
        delimiter=classification.delimiter,
        encoding=classification.encoding,
        control_totals=controls,
        approved_by=_SYSTEM_APPROVER,
        approved_at=_run_timestamp(ctx),
        output_dir=materialization_root,
    )
    generated = materializer(ctx, request)

    for existing, path in history:
        comparable = copy.deepcopy(dict(generated.rule_set_item))
        comparable["name"] = existing.name
        comparable["version"] = existing.version
        if canonical_rule_sha256(comparable) == existing.rule_sha256:
            generated.yaml_path.unlink(missing_ok=True)
            return _PublishedRevision(path=path, rule_set=existing, reused=True)

    destination = config.output_root / generated.yaml_path.name
    if destination.exists():
        raise InputError(
            f"Generated revision destination already exists and will not be "
            f"overwritten: '{destination}'."
        )
    published = move_file(
        generated.yaml_path,
        config.output_root,
        state_ctx=ctx,
        app="file_operator",
        pipeline=getattr(ctx, "pipeline_name", None),
        reason="validated_rule_set_publication",
    )
    return _PublishedRevision(
        path=published,
        rule_set=generated.rule_set,
        reused=False,
    )


def _load_history(
    output_root: Path,
    rule_set_name: str,
) -> tuple[tuple[FileOperatorRuleSet, Path], ...]:
    if not output_root.exists():
        return ()
    history: list[tuple[FileOperatorRuleSet, Path]] = []
    for path in sorted(output_root.glob(f"{rule_set_name}__v*.yaml")):
        match = _VERSIONED_FILE.fullmatch(path.name)
        if match is None or match.group("name") != rule_set_name:
            continue
        value = parse_yaml(read_text_file(path, encoding="utf-8"))
        if not isinstance(value, dict):
            raise InputError(f"Historical rule-set YAML is invalid: '{path}'.")
        entries = value.get("file_operator_rule_sets")
        if not isinstance(entries, list) or len(entries) != 1:
            raise InputError(
                f"Historical rule-set YAML must contain one item: '{path}'."
            )
        item = entries[0]
        resolved = resolve_file_operator_rule_set(
            SimpleNamespace(file_operator_rule_sets=[item]),
            str(_mapping(item, str(path)).get("name") or ""),
        )
        history.append((resolved, path))
    versions = [item.version for item, _path in history]
    if len(versions) != len(set(versions)):
        raise InputError(
            f"Historical revisions for '{rule_set_name}' contain duplicate versions."
        )
    return tuple(sorted(history, key=lambda pair: pair[0].version))


def _render_rule_set_name(
    config: RuleSetOnboardingConfig,
    group: _Group,
) -> str:
    rendered = config.rule_set_name_pattern.format(
        onboarding_name=config.name,
        group=group.value,
        group_slug=group.slug,
    )
    if not _SAFE_NAME.fullmatch(rendered):
        raise InputError(
            f"Rule-set naming convention produced invalid name '{rendered}'."
        )
    return rendered


def _run_timestamp(ctx: Any) -> str:
    value = str(
        getattr(ctx, "run_started_at", "") or getattr(ctx, "run_timestamp", "") or ""
    ).strip()
    if not value:
        raise RedactorError(
            "Rule-set onboarding requires the pipeline run timestamp for "
            "validation approval evidence."
        )
    return value


def _validate_name_pattern(pattern: str, name: str) -> None:
    allowed = {"onboarding_name", "group", "group_slug"}
    fields = set(re.findall(r"{([^{}]+)}", pattern))
    unknown = sorted(fields - allowed)
    if unknown:
        raise RedactorError(
            f"Rule-set onboarding '{name}' naming pattern has unsupported "
            f"field(s): {', '.join(unknown)}."
        )
    if "group" not in fields and "group_slug" not in fields:
        raise RedactorError(
            f"Rule-set onboarding '{name}' naming pattern must include "
            "'{group}' or '{group_slug}'."
        )


def _control_total_config(
    value: Any,
    config_name: str,
    index: int,
) -> ControlTotalCalculation:
    item = _mapping(value, f"{config_name}.control_totals[{index}]")
    aggregation = str(item.get("aggregation") or "sum")
    if aggregation != "sum":
        raise RedactorError(
            f"{config_name}.control_totals[{index}].aggregation must be 'sum'."
        )
    column = _required_string(item, "column", config_name)
    applicability = str(item.get("applicability") or "required")
    if applicability not in {"required", "when_present"}:
        raise RedactorError(
            f"{config_name}.control_totals[{index}].applicability must be "
            "'required' or 'when_present'."
        )
    return ControlTotalCalculation(
        name=_required_string(item, "name", config_name),
        table=_required_string(item, "table", config_name),
        column=column,
        source_column=str(item.get("source_column") or column),
        tolerance=_required_string(item, "tolerance", config_name),
        applicability=applicability,
    )


def _sha256(path: Path) -> str:
    return sha256_file(path)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(read_text_file(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read {label} '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label} '{path}' must contain a JSON object.")
    return value


def _slug(value: str) -> str:
    result = _SAFE_COMPONENT.sub("_", value).strip("._-").lower()
    if not result:
        raise InputError(f"Filename group value '{value}' has no safe characters.")
    return result


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): getattr(value, key) for key in keys()}
    if hasattr(value, "__dict__"):
        return vars(value)
    raise RedactorError(f"{label} must be a mapping.")


def _required_mapping(value: Any, key: str, label: str) -> Mapping[str, Any]:
    mapping = _mapping(value, label)
    if key not in mapping:
        raise RedactorError(f"{label} is missing required field '{key}'.")
    return _mapping(mapping[key], f"{label}.{key}")


def _required_list(value: Any, key: str, label: str) -> list[Any]:
    mapping = _mapping(value, label)
    result = mapping.get(key)
    if not isinstance(result, list):
        raise RedactorError(f"{label}.{key} must be a list.")
    return result


def _required_string_list(
    value: Any,
    key: str,
    label: str,
) -> list[str]:
    result = _get(value, key, None)
    if (
        not isinstance(result, list)
        or not result
        or any(not isinstance(item, str) or not item for item in result)
    ):
        raise RedactorError(
            f"{label} requires non-empty string list field '{key}'."
        )
    return result


def _required_string(value: Any, key: str, label: str) -> str:
    result = _get(value, key, None)
    if not isinstance(result, str) or not result.strip():
        raise RedactorError(f"{label} requires non-empty string field '{key}'.")
    return result.strip()


def _required_bool(value: Any, key: str, label: str) -> bool:
    result = _get(value, key, None)
    if not isinstance(result, bool):
        raise RedactorError(f"{label} requires boolean field '{key}'.")
    return result


def _required_path(value: Any, key: str, label: str) -> Path:
    result = _get(value, key, None)
    if isinstance(result, Path):
        return result.expanduser().resolve()
    if not isinstance(result, str) or not result.strip():
        raise RedactorError(f"{label} requires non-empty path field '{key}'.")
    return Path(result.strip()).expanduser().resolve()
