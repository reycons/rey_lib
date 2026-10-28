"""Activation and LLM-free execution of durable File Operator rule sets."""

from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from rey_lib.encryption import sha256_file, sha256_text
from rey_lib.config.config_utils import dump_yaml, parse_yaml
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.file_utils import (
    matches_file_pattern,
    pattern_to_glob,
    read_text_file,
)
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.files.transformer import match_header
from rey_lib.logs import log_artifact_reference

from rey_lib.files.error_utils import InputError, RedactorError
from rey_lib.files.rule_sets import (
    FileOperatorRuleSet,
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
    resolve_file_operator_rule_sets,
    verify_contract_lineage,
)
from rey_lib.files.structural_normalization import execute_structural_normalization
from rey_lib.files.structural_validation import CandidateValidationResult
from rey_lib.files.structural_validation import (
    validate_structural_normalization_candidates,
)

__all__ = [
    "ActivationResult",
    "FutureFileExecutionResult",
    "activate_generated_rule_set",
    "execute_active_rule_set",
    "resolve_active_rule_set_for_file",
]

_RULE_SET_CONFIG_PATH = "data_engine_rule_sets_config"


@dataclass(frozen=True)
class ActivationResult:
    """One atomically promoted version and the preserved installation set."""

    rule_set: FileOperatorRuleSet
    installation_path: Path
    active_rule_set_name: str
    preserved_versions: tuple[str, ...]


@dataclass(frozen=True)
class FutureFileExecutionResult:
    """Published outputs and evidence for one future source file."""

    rule_set: FileOperatorRuleSet
    source_path: Path
    output_paths: tuple[Path, ...]
    validation: CandidateValidationResult
    evidence_path: Path
    evidence_sha256: str


def activate_generated_rule_set(
    ctx: Any, run_log,
    generated_yaml_path: Path,
) -> ActivationResult:
    """Promote one generated version into installation-owned configuration.

    Existing revisions are preserved. A previously active revision for the
    same authoritative ``file_type`` is disabled and rehashed before the new
    revision is appended, making the active-version transition atomic.
    """

    generated_path = Path(generated_yaml_path).expanduser().resolve()
    if not generated_path.is_file():
        raise InputError(f"Generated rule-set YAML not found: '{generated_path}'.")
    destination = _installation_rule_set_path(ctx)
    generated_root = _read_yaml_mapping(generated_path, "generated rule-set YAML")
    _exact_keys(generated_root, {"data_engine_rule_sets"}, "generated YAML")
    generated_entries = generated_root["data_engine_rule_sets"]
    if not isinstance(generated_entries, list) or len(generated_entries) != 1:
        raise InputError("Generated YAML must contain exactly one rule-set item.")
    incoming_item = _mapping(generated_entries[0], "generated rule-set item")
    incoming = _resolve_one(incoming_item)
    if not incoming.enabled or incoming.status != "approved":
        raise InputError("Generated rule set must be enabled and approved.")
    _verify_lineage_artifacts(ctx, incoming_item)

    existing_items = _load_installation_items(destination)
    existing = _resolve_items(existing_items)
    if any(item.name == incoming.name for item in existing):
        raise InputError(
            f"Rule-set revision '{incoming.name}' already exists; "
            "versioned revisions are never overwritten."
        )
    prior_versions = [
        item.version
        for item in existing
        if item.rule_set_name == incoming.rule_set_name
    ]
    if prior_versions and incoming.version <= max(prior_versions):
        raise InputError(
            f"Rule-set version {incoming.version} must be greater than existing "
            f"version {max(prior_versions)} for '{incoming.rule_set_name}'."
        )

    promoted_items = deepcopy(existing_items)
    for item in promoted_items:
        if item.get("file_type") == incoming.file_type and item.get("enabled") is True:
            item["enabled"] = False
            item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)
    promoted_items.append(deepcopy(dict(incoming_item)))

    resolved_promoted = _resolve_items(promoted_items)
    active_same_identity = [
        item
        for item in resolved_promoted
        if item.file_type == incoming.file_type
        and item.enabled
        and item.status == "approved"
    ]
    if len(active_same_identity) != 1:
        raise InputError(
            f"Structural identity '{incoming.file_type}' must have exactly one "
            "enabled approved rule set after activation."
        )

    rendered = dump_yaml(
        {"data_engine_rule_sets": promoted_items},
        sort_keys=False,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    reparsed = parse_yaml(rendered)
    if not isinstance(reparsed, dict):
        raise InputError("Promoted File Operator YAML root is not a mapping.")
    _resolve_items(reparsed["data_engine_rule_sets"])

    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination, rendered)
    if ctx is not None:
        log_artifact_reference(run_log,
            str(destination),
            role="active_data_engine_rule_sets",
            producer="data_engine",
            artifact_group="rule_sets",
            producing_app="data_engine",
            artifact_type="data_engine_rule_sets",
            viewer_type="file",
            safe_to_preview=True,
            active_rule_set_name=incoming.name,
            file_type=incoming.file_type,
            rule_sha256=incoming.rule_sha256,
        )
    return ActivationResult(
        rule_set=incoming,
        installation_path=destination,
        active_rule_set_name=incoming.name,
        preserved_versions=tuple(item.name for item in existing),
    )


def resolve_active_rule_set_for_file(
    ctx: Any,
    source_path: Path,
) -> FileOperatorRuleSet:
    """Return the only enabled approved rule set matching one source identity."""

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise InputError(f"Future source file not found: '{source}'.")
    try:
        rule_sets = resolve_file_operator_rule_sets(ctx)
    except RedactorError as exc:
        raise InputError(
            f"Cannot resolve active File Operator rule sets: {exc}"
        ) from exc

    header_cache: dict[str, tuple[str, ...]] = {}
    identity_matches = [
        rule_set
        for rule_set in rule_sets
        if _rule_set_matches_source(rule_set, source, header_cache)
    ]
    active_matches = [
        rule_set
        for rule_set in identity_matches
        if rule_set.enabled and rule_set.status == "approved"
    ]
    if len(active_matches) != 1:
        active_names = sorted(item.name for item in active_matches)
        identity_names = sorted(item.name for item in identity_matches)
        raise InputError(
            f"Structural identity for '{source.name}' requires exactly one enabled "
            f"approved rule set; found {len(active_matches)}. "
            f"active_matches={active_names}, identity_matches={identity_names}."
        )
    return active_matches[0]


def execute_active_rule_set(
    ctx: Any, run_log,
    source_path: Path,
    output_dir: Path,
    review_dir: Path,
) -> FutureFileExecutionResult:
    """Resolve, validate, and publish one future file without Analyzer or an LLM."""

    source = Path(source_path).expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    review_root = Path(review_dir).expanduser().resolve()
    try:
        rule_set = resolve_active_rule_set_for_file(ctx, source)
    except InputError as exc:
        review_path = _write_review_record(
            review_root,
            source,
            status="identity_failed",
            errors=[str(exc)],
        )
        raise InputError(f"{exc} Controlled review evidence: '{review_path}'.") from exc

    with tempfile.TemporaryDirectory(
        prefix="file_operator_active_rule_",
    ) as temporary:
        temporary_root = Path(temporary)
        candidate = execute_structural_normalization(
            None, run_log,
            rule_set,
            source,
            temporary_root / "candidate",
        )
        validation = validate_structural_normalization_candidates(
            run_log,
            rule_set,
            [candidate],
            [source.name],
            temporary_root / "validation",
            control_total_scope="matching",
        )
        validation_text = validation.evidence_path.read_text(encoding="utf-8")
        prepared_outputs = tuple(
            (
                output.table_name,
                output.output_sha256,
                read_text_file(
                    output.output_path,
                    encoding=rule_set.source_format.encoding,
                ),
            )
            for output in candidate.outputs
        )

    if validation.status != "passed":
        validation_path = review_root / (
            f"{source.stem}.{rule_set.name}.validation.json"
        )
        review_root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(validation_path, validation_text)
        review_path = _write_review_record(
            review_root,
            source,
            status="validation_failed",
            errors=[
                error
                for check in validation.checks
                for error in check.details.get("errors", [])
            ],
            rule_set=rule_set,
            validation_path=validation_path,
        )
        raise InputError(
            "Active File Operator rule-set validation failed; outputs were not "
            f"published. Controlled review evidence: '{review_path}'."
        )

    destinations = tuple(
        output_root / f"{source.stem}.{table_name}.csv"
        for table_name, _digest, _content in prepared_outputs
    )
    if len(set(destinations)) != len(destinations):
        raise InputError("Active rule-set output paths collide.")
    if source in destinations:
        raise InputError("Active rule-set output path collides with its source.")

    output_root.mkdir(parents=True, exist_ok=True)
    for destination, (_table, _digest, content) in zip(
        destinations,
        prepared_outputs,
        strict=True,
    ):
        atomic_write_text(destination, content)
    validation_path = output_root / (f"{source.stem}.{rule_set.name}.validation.json")
    atomic_write_text(validation_path, validation_text)
    published_validation = replace(validation, evidence_path=validation_path)
    evidence = {
        "schema_version": 1,
        "status": "passed",
        "source_path": str(source),
        "source_sha256": _file_sha256(source),
        "file_type": rule_set.file_type,
        "rule_set": {
            "name": rule_set.name,
            "version": rule_set.version,
            "rule_sha256": rule_set.rule_sha256,
        },
        "validation": {
            "path": str(validation_path),
            "sha256": published_validation.evidence_sha256,
        },
        "outputs": [
            {
                "table": table_name,
                "path": str(destination),
                "sha256": digest,
            }
            for destination, (table_name, digest, _content) in zip(
                destinations,
                prepared_outputs,
                strict=True,
            )
        ],
    }
    evidence_text = (
        json.dumps(
            evidence,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    evidence_path = output_root / (f"{source.stem}.{rule_set.name}.application.json")
    atomic_write_text(evidence_path, evidence_text)
    evidence_sha256 = sha256_text(evidence_text)
    if ctx is not None:
        for destination, output in zip(
            destinations,
            evidence["outputs"],
            strict=True,
        ):
            log_artifact_reference(run_log,
                str(destination),
                role="structural_normalization_output",
                producer="data_engine",
                artifact_group="normalized_outputs",
                producing_app="data_engine",
                artifact_type="normalized_csv",
                viewer_type="file",
                safe_to_preview=False,
                source_path=str(source),
                file_type=rule_set.file_type,
                rule_set_name=rule_set.name,
                rule_sha256=rule_set.rule_sha256,
                table_name=output["table"],
                artifact_sha256=output["sha256"],
            )
        log_artifact_reference(run_log,
            str(evidence_path),
            role="structural_normalization_application",
            producer="data_engine",
            artifact_group="normalized_outputs",
            producing_app="data_engine",
            artifact_type="structural_normalization_application",
            viewer_type="file",
            safe_to_preview=True,
            source_path=str(source),
            file_type=rule_set.file_type,
            rule_set_name=rule_set.name,
            rule_sha256=rule_set.rule_sha256,
            artifact_sha256=evidence_sha256,
        )
    return FutureFileExecutionResult(
        rule_set=rule_set,
        source_path=source,
        output_paths=destinations,
        validation=published_validation,
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
    )


def _installation_rule_set_path(ctx: Any) -> Path:
    paths = getattr(ctx, "paths", None)
    if paths is None:
        raise InputError("File Operator context is missing the installation paths.")
    try:
        config_root = Path(paths.resolve("configs")).expanduser().resolve()
        destination = Path(paths.resolve(_RULE_SET_CONFIG_PATH)).expanduser().resolve()
    except Exception as exc:
        raise InputError(
            "File Operator context must resolve 'configs' and "
            f"'{_RULE_SET_CONFIG_PATH}'."
        ) from exc
    if not destination.is_relative_to(config_root):
        raise InputError(
            "File Operator rule-set activation target must be inside the "
            "installation config root."
        )
    return destination


def _load_installation_items(destination: Path) -> list[dict[str, Any]]:
    if not destination.exists():
        return []
    root = _read_yaml_mapping(destination, "installation rule-set YAML")
    _exact_keys(root, {"data_engine_rule_sets"}, "installation rule-set YAML")
    entries = root["data_engine_rule_sets"]
    if not isinstance(entries, list):
        raise InputError("Installation data_engine_rule_sets must be a list.")
    return [
        deepcopy(dict(_mapping(item, "installation rule-set item"))) for item in entries
    ]


def _resolve_items(items: list[Mapping[str, Any]]) -> tuple[FileOperatorRuleSet, ...]:
    if not items:
        return ()
    try:
        return resolve_file_operator_rule_sets(
            SimpleNamespace(data_engine_rule_sets=items)
        )
    except RedactorError as exc:
        raise InputError(
            f"Installation File Operator rule sets are invalid: {exc}"
        ) from exc


def _resolve_one(item: Mapping[str, Any]) -> FileOperatorRuleSet:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise InputError("Generated rule-set item is missing its name.")
    try:
        return resolve_file_operator_rule_set(
            SimpleNamespace(data_engine_rule_sets=[item]),
            name,
        )
    except RedactorError as exc:
        raise InputError(f"Generated File Operator rule set is invalid: {exc}") from exc


def _verify_lineage_artifacts(ctx: Any, item: Mapping[str, Any]) -> None:
    lineage = _mapping(item.get("lineage"), "generated rule-set lineage")
    # The contract is lineage by its canonical identity -- declared name plus
    # the hash of its stored text -- not a file (backlog 903).
    contract_name = lineage.get("llm_contract_name")
    if not isinstance(contract_name, str) or not contract_name:
        raise InputError("Generated lineage 'llm_contract_name' is invalid.")
    verify_contract_lineage(
        ctx, contract_name, lineage.get("llm_contract_sha256"), "Generated lineage",
    )
    for path_key, hash_key in (
        ("analysis_artifact", "analysis_artifact_sha256"),
        ("candidate_rules_path", "candidate_rules_sha256"),
        ("validation_evidence_path", "validation_evidence_sha256"),
    ):
        path_value = lineage.get(path_key)
        expected = lineage.get(hash_key)
        if not isinstance(path_value, str) or not path_value:
            raise InputError(f"Generated lineage '{path_key}' is invalid.")
        artifact = Path(path_value).expanduser().resolve()
        if not artifact.is_file():
            raise InputError(f"Generated lineage artifact not found: '{artifact}'.")
        if _file_sha256(artifact) != expected:
            raise InputError(
                f"Generated lineage hash '{hash_key}' does not match '{artifact}'."
            )
    evidence_path = Path(str(lineage["validation_evidence_path"])).resolve()
    evidence = _read_json_mapping(evidence_path, "validation evidence")
    if evidence.get("status") != "passed":
        raise InputError("Generated validation evidence status must be 'passed'.")
    evidence_rule = _mapping(evidence.get("rule_set"), "validation evidence.rule_set")
    if evidence_rule.get("rule_sha256") != item["canonicalization"]["rule_sha256"]:
        raise InputError(
            "Generated validation evidence rule_sha256 does not match the rule set."
        )


def _rule_set_matches_source(
    rule_set: FileOperatorRuleSet,
    source: Path,
    header_cache: dict[str, tuple[str, ...]],
) -> bool:
    for reference in rule_set.file_references:
        filename_matches = not reference.file_patterns or any(
            matches_file_pattern(source, pattern_to_glob(pattern))
            for pattern in reference.file_patterns
        )
        if not filename_matches:
            continue
        if reference.header is None:
            return True
        lines = header_cache.get(rule_set.source_format.encoding)
        if lines is None:
            try:
                with source.open(
                    encoding=rule_set.source_format.encoding,
                    errors="replace",
                ) as handle:
                    lines = tuple(line.strip() for line in handle if line.strip())
            except OSError as exc:
                raise InputError(
                    f"Cannot inspect structural identity for '{source}': {exc}"
                ) from exc
            header_cache[rule_set.source_format.encoding] = lines
        if any(match_header(line, {"header": reference.header}) for line in lines):
            return True
    return False


def _write_review_record(
    review_root: Path,
    source: Path,
    *,
    status: str,
    errors: list[str],
    rule_set: FileOperatorRuleSet | None = None,
    validation_path: Path | None = None,
) -> Path:
    review_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "status": status,
        "source_path": str(source),
        "source_sha256": _file_sha256(source) if source.is_file() else "",
        "rule_set": (
            {
                "name": rule_set.name,
                "file_type": rule_set.file_type,
                "rule_sha256": rule_set.rule_sha256,
            }
            if rule_set is not None
            else None
        ),
        "validation_path": str(validation_path) if validation_path else "",
        "errors": errors,
        "automatic_rule_regeneration": False,
    }
    path = review_root / f"{source.stem}.structural_review.json"
    atomic_write_text(
        path,
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return path


def _read_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = parse_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ConfigError) as exc:
        raise InputError(f"Cannot read {label} '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label.capitalize()} '{path}' must contain a mapping.")
    return value


def _read_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read {label} '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label.capitalize()} '{path}' must contain a mapping.")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise InputError(f"Cannot hash File Operator artifact '{path}': {exc}") from exc


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputError(f"{path} must be a mapping.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise InputError(f"{path} is missing fields: {', '.join(missing)}.")
    if unknown:
        raise InputError(f"{path} contains unknown fields: {', '.join(unknown)}.")
