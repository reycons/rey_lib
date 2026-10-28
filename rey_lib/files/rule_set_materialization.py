"""Deterministic materialization of validated structural-rule candidates.

The materializer treats Analyzer JSON as untrusted input. It validates and
projects that proposal into the durable File Operator contract, executes the
projected rule twice across every authoritative onboarding sample, and writes
YAML only after every configured validation passes.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from rey_lib.config.config_utils import dump_yaml, parse_yaml
from rey_lib.logs import AppLogger
from rey_lib.files.csv import render_delimited_line
from rey_lib.encryption import sha256_file
from rey_lib.files.file_utils import (
    matches_file_pattern,
    pattern_to_glob,
)
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import log_artifact_reference

from rey_lib.files.error_utils import InputError
from rey_lib.files.rule_sets import (
    FileOperatorRuleSet,
    canonical_rule_sha256,
    resolve_file_operator_rule_set,
    verify_contract_lineage,
)
from rey_lib.files.structural_normalization import (
    execute_structural_normalization,
)
from rey_lib.files.structural_validation import CandidateValidationResult
from rey_lib.files.structural_validation import (
    validate_structural_normalization_candidates,
)

__all__ = [
    "ControlTotal",
    "ControlTotalExpectation",
    "MaterializationRequest",
    "MaterializationResult",
    "materialize_file_operator_rule_set",
]

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FILE_TYPE = re.compile(r"^file_type_[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_ALGORITHM = "canonical-json-sorted-keys-utf8"


@dataclass(frozen=True)
class ControlTotalExpectation:
    """One trusted expected aggregate for one authoritative sample."""

    source_file: str
    value: str


@dataclass(frozen=True)
class ControlTotal:
    """One deterministic numeric control-total validation."""

    name: str
    table: str
    column: str
    tolerance: str
    expected_values: tuple[ControlTotalExpectation, ...]


@dataclass(frozen=True)
class MaterializationRequest:
    """Trusted inputs surrounding one untrusted Analyzer candidate."""

    rule_set_name: str
    version: int
    analysis_artifact_path: Path
    candidate_rules_path: Path
    analyzer_context_path: Path
    model_profile: str
    source_paths: tuple[Path, ...]
    delimiter: str
    encoding: str
    control_totals: tuple[ControlTotal, ...]
    approved_by: str
    approved_at: str
    output_dir: Path
    file_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializationResult:
    """A validated generated rule set and its two durable artifacts."""

    rule_set: FileOperatorRuleSet
    rule_set_item: Mapping[str, Any]
    yaml_path: Path
    validation: CandidateValidationResult


_logger = AppLogger(__name__)


def materialize_file_operator_rule_set(
    ctx: Any, run_log,
    request: MaterializationRequest,
) -> MaterializationResult:
    """Validate, execute twice, and emit one canonical versioned YAML item."""

    trusted = _validate_request(request)
    analysis = _read_json_object(
        trusted.analysis_artifact_path,
        "analysis artifact",
    )
    candidate = _read_json_object(
        trusted.candidate_rules_path,
        "candidate rules",
    )
    analyzer_context = _read_json_object(
        trusted.analyzer_context_path,
        "Analyzer context",
    )

    analysis_sha256 = sha256_file(trusted.analysis_artifact_path)
    candidate_sha256 = sha256_file(trusted.candidate_rules_path)
    lineage = _validated_analyzer_lineage(
        ctx,
        analyzer_context,
        trusted,
        analysis_sha256,
    )
    matching_files, evidence_points = _validate_analysis_artifact(
        analysis,
        trusted.source_paths,
    )
    tables = _project_candidate_tables(
        candidate,
        analysis,
        matching_files,
        evidence_points,
    )
    validations = _build_validations(trusted.control_totals, matching_files)
    versioned_name = f"{trusted.rule_set_name}__v{trusted.version:04d}"
    validation_dir = trusted.output_dir / "validation"
    validation_path = validation_dir / (f"{analysis['file_type']}.rule_validation.json")

    item = _build_rule_set_item(
        request=trusted,
        versioned_name=versioned_name,
        analysis=analysis,
        tables=tables,
        validations=validations,
        lineage={
            **lineage,
            "analysis_artifact": str(trusted.analysis_artifact_path),
            "analysis_artifact_sha256": analysis_sha256,
            "candidate_rules_path": str(trusted.candidate_rules_path),
            "candidate_rules_sha256": candidate_sha256,
            "validation_evidence_path": str(validation_path),
            "validation_evidence_sha256": "0" * 64,
        },
        matching_file_count=len(matching_files),
    )
    item["canonicalization"]["rule_sha256"] = canonical_rule_sha256(item)
    provisional = _resolve_generated_item(item, versioned_name)

    # Control is about to reach the filesystem. Materialization writes a rule
    # set an operator will act on, so which rule set and where it is going are
    # the facts a later failure has to be located against.
    _logger.debug(
        "attempting rule set materialization name=%s output_dir=%s files=%s",
        versioned_name, trusted.output_dir, len(matching_files),
    )
    trusted.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="file_operator_materialization_",
        dir=trusted.output_dir,
    ) as temporary:
        candidate_root = Path(temporary) / "candidates"
        candidates = tuple(
            execute_structural_normalization(
                None, run_log,
                provisional,
                source_path,
                candidate_root / source_path.stem,
            )
            for source_path in trusted.source_paths
        )
        validation = validate_structural_normalization_candidates(
            run_log,
            provisional,
            candidates,
            matching_files,
            validation_dir,
        )

    if validation.status != "passed":
        raise InputError(
            "Generated File Operator rule set failed deterministic validation; "
            f"YAML was not written. Evidence: '{validation.evidence_path}'."
        )

    item["lineage"]["validation_evidence_sha256"] = validation.evidence_sha256
    final_rule_set = _resolve_generated_item(item, versioned_name)
    wrapper = {"data_engine_rule_sets": [item]}
    yaml_text = dump_yaml(wrapper, sort_keys=False)
    if not yaml_text.endswith("\n"):
        yaml_text += "\n"
    reparsed = parse_yaml(yaml_text)
    if not isinstance(reparsed, dict):
        raise InputError("Generated File Operator YAML root is not a mapping.")
    round_trip = _resolve_generated_item(
        reparsed["data_engine_rule_sets"][0],
        versioned_name,
    )
    if round_trip.rule_sha256 != final_rule_set.rule_sha256:
        raise InputError("Generated File Operator YAML changed canonical rule content.")

    yaml_path = trusted.output_dir / f"{versioned_name}.yaml"
    _logger.debug("attempting rule set write path=%s", yaml_path)
    atomic_write_text(yaml_path, yaml_text)
    if ctx is not None:
        log_artifact_reference(run_log,
            str(yaml_path),
            role="generated_data_engine_rule_set",
            producer="data_engine",
            artifact_group="rule_sets",
            producing_app="data_engine",
            artifact_type="data_engine_rule_set",
            viewer_type="file",
            safe_to_preview=True,
            rule_set_name=versioned_name,
            rule_sha256=final_rule_set.rule_sha256,
            validation_evidence_path=str(validation.evidence_path),
            validation_evidence_sha256=validation.evidence_sha256,
        )
    return MaterializationResult(
        rule_set=final_rule_set,
        rule_set_item=item,
        yaml_path=yaml_path,
        validation=validation,
    )


def _validate_request(request: MaterializationRequest) -> MaterializationRequest:
    if not _SAFE_NAME.fullmatch(request.rule_set_name):
        raise InputError(
            "Materialization rule_set_name must contain only letters, numbers, "
            "dot, underscore, or hyphen and must start with a letter or number."
        )
    if isinstance(request.version, bool) or request.version < 1:
        raise InputError("Materialization version must be a positive integer.")
    if len(request.delimiter) != 1:
        raise InputError("Materialization delimiter must be exactly one character.")
    for label, value in (
        ("encoding", request.encoding),
        ("model_profile", request.model_profile),
        ("approved_by", request.approved_by),
        ("approved_at", request.approved_at),
    ):
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"Materialization {label} must be a non-empty string.")
    if not request.source_paths:
        raise InputError("Materialization source_paths must not be empty.")
    if not request.control_totals:
        raise InputError("Materialization control_totals must not be empty.")

    paths = (
        request.analysis_artifact_path,
        request.candidate_rules_path,
        request.analyzer_context_path,
        *request.source_paths,
    )
    resolved = tuple(Path(path).expanduser().resolve() for path in paths)
    for path in resolved:
        if not path.is_file():
            raise InputError(f"Materialization input file not found: '{path}'.")
    source_paths = resolved[3:]
    basenames = [path.name for path in source_paths]
    if len(set(basenames)) != len(basenames):
        raise InputError("Materialization source basenames must be unique.")
    file_patterns = tuple(sorted(set(request.file_patterns)))
    if any(not isinstance(pattern, str) or not pattern.strip() for pattern in file_patterns):
        raise InputError("Materialization file_patterns must be non-empty strings.")
    if file_patterns and any(
        not any(
            matches_file_pattern(source, pattern_to_glob(pattern))
            for pattern in file_patterns
        )
        for source in source_paths
    ):
        raise InputError(
            "Materialization file_patterns must match every authoritative source."
        )
    return MaterializationRequest(
        rule_set_name=request.rule_set_name,
        version=request.version,
        analysis_artifact_path=resolved[0],
        candidate_rules_path=resolved[1],
        analyzer_context_path=resolved[2],
        model_profile=request.model_profile,
        source_paths=tuple(sorted(source_paths, key=lambda path: path.name)),
        delimiter=request.delimiter,
        encoding=request.encoding,
        control_totals=request.control_totals,
        approved_by=request.approved_by,
        approved_at=request.approved_at,
        output_dir=Path(request.output_dir).expanduser().resolve(),
        file_patterns=file_patterns,
    )


def _validated_analyzer_lineage(
    ctx: Any,
    context: Mapping[str, Any],
    request: MaterializationRequest,
    analysis_sha256: str,
) -> dict[str, str]:
    _exact_keys(
        context,
        {
            "request_id",
            "run_id",
            "workflow_name",
            "source_name",
            "analysis_name",
            "model_profile",
            "provider",
            "model",
            "source_artifact_path",
            "source_artifact_sha256",
            "contract_name",
            "contract_hash",
            "schema_hash",
            "idempotency_mode",
            "result_artifact_path",
            "result_artifact_sha256",
            "candidate_artifact_path",
            "candidate_artifact_sha256",
            "status",
            "errors",
        },
        "Analyzer context",
    )
    if _string(context, "analysis_name", "Analyzer context") != (
        "infer_tabular_discard_rules"
    ):
        raise InputError("Analyzer context has the wrong analysis_name.")
    if _string(context, "status", "Analyzer context") != "success":
        raise InputError("Analyzer context status must be 'success'.")
    if context.get("errors") != []:
        raise InputError("Analyzer context errors must be empty.")
    if (
        _string(context, "source_artifact_sha256", "Analyzer context")
        != analysis_sha256
    ):
        raise InputError(
            "Analyzer context source_artifact_sha256 does not match analysis artifact."
        )
    context_source = Path(
        _string(context, "source_artifact_path", "Analyzer context")
    ).expanduser().resolve()
    if context_source != request.analysis_artifact_path:
        raise InputError(
            "Analyzer context source artifact does not match analysis artifact."
        )
    if _string(context, "model_profile", "Analyzer context") != request.model_profile:
        raise InputError("Analyzer context model_profile does not match the request.")
    provider = _string(context, "provider", "Analyzer context")
    model = _string(context, "model", "Analyzer context")
    workflow_name = _string(context, "workflow_name", "Analyzer context")

    contract_name = _string(context, "contract_name", "Analyzer context")
    contract_hash = _string(context, "contract_hash", "Analyzer context")
    if not _SHA256.fullmatch(contract_hash):
        raise InputError("Analyzer context contract_hash is not a SHA-256 digest.")
    verify_contract_lineage(ctx, contract_name, contract_hash, "Analyzer context")
    result_path = Path(
        _string(context, "result_artifact_path", "Analyzer context")
    ).expanduser().resolve()
    result_hash = _string(context, "result_artifact_sha256", "Analyzer context")
    candidate_path = Path(
        _string(context, "candidate_artifact_path", "Analyzer context")
    ).expanduser().resolve()
    candidate_hash = _string(
        context,
        "candidate_artifact_sha256",
        "Analyzer context",
    )
    for label, path, expected in (
        ("result", result_path, result_hash),
        ("candidate", candidate_path, candidate_hash),
    ):
        if not path.is_file() or not _SHA256.fullmatch(expected):
            raise InputError(f"Analyzer {label} artifact lineage is invalid.")
        if sha256_file(path) != expected:
            raise InputError(f"Analyzer {label} artifact hash does not match.")
    if candidate_path != request.candidate_rules_path:
        raise InputError(
            "Analyzer context candidate artifact does not match candidate rules."
        )
    return {
        "analyzer_workflow": workflow_name,
        "analysis_name": _string(context, "analysis_name", "Analyzer context"),
        "llm_contract_name": contract_name,
        "llm_contract_sha256": contract_hash,
        "model_profile": _string(context, "model_profile", "Analyzer context"),
        "provider": provider,
        "model": model,
        "request_id": _string(context, "request_id", "Analyzer context"),
        "analyzer_run_id": _string(context, "run_id", "Analyzer context"),
        "analyzer_result_path": str(result_path),
        "analyzer_result_sha256": result_hash,
    }


def _validate_analysis_artifact(
    analysis: Mapping[str, Any],
    source_paths: tuple[Path, ...],
) -> tuple[tuple[str, ...], frozenset[tuple[str, int]]]:
    if analysis.get("schema_version") != 2:
        raise InputError("Analysis artifact schema_version must be 2.")
    file_type = _string(analysis, "file_type", "analysis artifact")
    if not _FILE_TYPE.fullmatch(file_type):
        raise InputError("Analysis artifact file_type is invalid.")
    if analysis.get("layout") != "delimited":
        raise InputError("Analysis artifact layout must be 'delimited'.")
    matching = _string_list(analysis, "matching_files", "analysis artifact")
    if tuple(sorted(matching)) != tuple(path.name for path in source_paths):
        raise InputError(
            "Materialization sources must exactly match analysis matching_files."
        )
    completeness = _mapping(
        analysis.get("completeness"),
        "analysis artifact.completeness",
    )
    patterns = _mapping(
        completeness.get("patterns"),
        "analysis artifact.completeness.patterns",
    )
    if patterns.get("omitted") != 0:
        raise InputError(
            "Analysis artifact omitted structural patterns; materialization is unsafe."
        )
    evidence: set[tuple[str, int]] = set()
    _collect_evidence_points(analysis, evidence)
    if not evidence:
        raise InputError("Analysis artifact contains no source-line evidence.")
    return tuple(sorted(matching)), frozenset(evidence)


def _collect_evidence_points(value: Any, output: set[tuple[str, int]]) -> None:
    if isinstance(value, Mapping):
        source = value.get("source_file")
        line = value.get("physical_line_number")
        if (
            isinstance(source, str)
            and source
            and isinstance(line, int)
            and not isinstance(line, bool)
            and line > 0
        ):
            output.add((source, line))
        for item in value.values():
            _collect_evidence_points(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_evidence_points(item, output)


def _project_candidate_tables(
    candidate: Mapping[str, Any],
    analysis: Mapping[str, Any],
    matching_files: tuple[str, ...],
    evidence_points: frozenset[tuple[str, int]],
) -> list[dict[str, Any]]:
    _exact_keys(
        candidate,
        {"schema_version", "file_type", "tables", "unresolved_ambiguities"},
        "candidate",
    )
    if candidate.get("schema_version") != 1:
        raise InputError("Candidate schema_version must be 1.")
    if candidate.get("file_type") != analysis["file_type"]:
        raise InputError("Candidate file_type does not match analysis artifact.")
    if candidate.get("unresolved_ambiguities") != []:
        raise InputError("Candidate unresolved_ambiguities must be empty.")
    raw_tables = _list(candidate, "tables", "candidate")
    if not raw_tables:
        raise InputError("Candidate tables must not be empty.")

    tables = [
        _project_table(
            _mapping(table, f"candidate.tables[{index}]"),
            f"candidate.tables[{index}]",
            matching_files,
            evidence_points,
        )
        for index, table in enumerate(raw_tables)
    ]
    names = [str(table["name"]) for table in tables]
    _require_unique(names, "candidate table names")
    for index, table in enumerate(tables):
        if table["end"]["type"] == "eof" and index != len(tables) - 1:
            raise InputError("Only the final candidate table may end at EOF.")

    authoritative_headers = _string_list(
        analysis,
        "ordered_headers",
        "analysis artifact",
    )
    if tables[0]["expected_columns"] != list(authoritative_headers):
        raise InputError(
            "First candidate table columns must exactly match authoritative "
            "ordered_headers."
        )
    return tables


def _project_table(
    table: Mapping[str, Any],
    path: str,
    matching_files: tuple[str, ...],
    evidence_points: frozenset[tuple[str, int]],
) -> dict[str, Any]:
    _exact_keys(
        table,
        {"name", "expected_columns", "header", "start", "end", "row_rules"},
        path,
    )
    name = _string(table, "name", path)
    if not _SAFE_NAME.fullmatch(name):
        raise InputError(f"{path}.name is not safe for deterministic output naming.")
    expected_columns = list(_string_list(table, "expected_columns", path))
    _require_unique(expected_columns, f"{path}.expected_columns")

    header = _mapping(table.get("header"), f"{path}.header")
    _exact_keys(
        header,
        {"match", "keep_first", "repeated", "evidence", "confidence"},
        f"{path}.header",
    )
    header_match = _project_field_match(
        _mapping(header.get("match"), f"{path}.header.match"),
        f"{path}.header.match",
    )
    if header_match["values"] != expected_columns:
        raise InputError(f"{path}.header.match.values must equal expected_columns.")
    if header.get("keep_first") is not True:
        raise InputError(f"{path}.header.keep_first must be true.")
    _validate_proposal_evidence(
        header,
        f"{path}.header",
        matching_files,
        evidence_points,
    )
    repeated = _mapping(header.get("repeated"), f"{path}.header.repeated")
    _exact_keys(
        repeated,
        {"comparison", "action", "evidence", "confidence"},
        f"{path}.header.repeated",
    )
    if (
        repeated.get("comparison") != "normalized_exact"
        or repeated.get("action") != "drop"
    ):
        raise InputError(f"{path}.header.repeated must use normalized_exact/drop.")
    _validate_proposal_evidence(
        repeated,
        f"{path}.header.repeated",
        matching_files,
        evidence_points,
    )

    start = _mapping(table.get("start"), f"{path}.start")
    _exact_keys(start, {"type", "evidence", "confidence"}, f"{path}.start")
    if start.get("type") != "header":
        raise InputError(f"{path}.start.type must be 'header'.")
    _validate_proposal_evidence(
        start,
        f"{path}.start",
        matching_files,
        evidence_points,
    )

    end = _project_end(
        _mapping(table.get("end"), f"{path}.end"),
        f"{path}.end",
        matching_files,
        evidence_points,
    )
    raw_rules = _list(table, "row_rules", path)
    rules = [
        _project_row_rule(
            _mapping(rule, f"{path}.row_rules[{index}]"),
            f"{path}.row_rules[{index}]",
            matching_files,
            evidence_points,
        )
        for index, rule in enumerate(raw_rules)
    ]
    _require_unique([str(rule["name"]) for rule in rules], f"{path} row-rule names")
    orders = [int(rule["order"]) for rule in rules]
    if orders and orders != list(range(1, len(orders) + 1)):
        raise InputError(
            f"{path}.row_rules order values must start at 1 and be contiguous."
        )
    return {
        "name": name,
        "output_name": f"{{source_stem}}.{name}.csv",
        "expected_columns": expected_columns,
        "header": {
            "match": header_match,
            "keep_first": True,
            "repeated": {
                "comparison": "normalized_exact",
                "action": "drop",
            },
        },
        "start": {"type": "header"},
        "end": end,
        "row_rules": rules,
    }


def _project_field_match(match: Mapping[str, Any], path: str) -> dict[str, Any]:
    _exact_keys(
        match,
        {"type", "values", "case_sensitive", "trim_fields"},
        path,
    )
    match_type = _string(match, "type", path)
    if match_type not in {"exact_fields", "contains_fields"}:
        raise InputError(f"{path}.type is unsupported.")
    values = list(_string_list(match, "values", path))
    return {
        "type": match_type,
        "values": values,
        "case_sensitive": _bool(match, "case_sensitive", path),
        "trim_fields": _bool(match, "trim_fields", path),
    }


def _project_end(
    end: Mapping[str, Any],
    path: str,
    matching_files: tuple[str, ...],
    evidence_points: frozenset[tuple[str, int]],
) -> dict[str, Any]:
    end_type = _string(end, "type", path)
    expected = (
        {"type", "evidence", "confidence"}
        if end_type == "eof"
        else {"type", "match", "evidence", "confidence"}
    )
    _exact_keys(end, expected, path)
    _validate_proposal_evidence(end, path, matching_files, evidence_points)
    if end_type == "eof":
        return {"type": "eof"}
    if end_type != "before_match":
        raise InputError(f"{path}.type must be 'before_match' or 'eof'.")
    return {
        "type": "before_match",
        "match": _project_field_match(
            _mapping(end.get("match"), f"{path}.match"),
            f"{path}.match",
        ),
    }


def _project_row_rule(
    rule: Mapping[str, Any],
    path: str,
    matching_files: tuple[str, ...],
    evidence_points: frozenset[tuple[str, int]],
) -> dict[str, Any]:
    has_standard = "standard" in rule
    has_match = "match" in rule
    if has_standard == has_match:
        raise InputError(f"{path} must define exactly one of standard or match.")
    expected = {
        "name",
        "order",
        "action",
        "evidence",
        "confidence",
        "standard" if has_standard else "match",
    }
    _exact_keys(rule, expected, path)
    name = _string(rule, "name", path)
    if not _SAFE_NAME.fullmatch(name):
        raise InputError(f"{path}.name is invalid.")
    order = rule.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise InputError(f"{path}.order must be a positive integer.")
    if rule.get("action") != "drop":
        raise InputError(f"{path}.action must be 'drop'.")
    _validate_proposal_evidence(rule, path, matching_files, evidence_points)
    projected: dict[str, Any] = {
        "name": name,
        "order": order,
        "action": "drop",
    }
    if has_standard:
        standard = _string(rule, "standard", path)
        if standard not in {"blank", "repeated_header"}:
            raise InputError(f"{path}.standard is unsupported.")
        projected["standard"] = standard
        return projected

    match = _mapping(rule.get("match"), f"{path}.match")
    match_type = _string(match, "type", f"{path}.match")
    if match_type == "contains":
        _exact_keys(
            match,
            {"type", "value", "case_sensitive"},
            f"{path}.match",
        )
        projected["match"] = {
            "type": "contains",
            "value": _string(match, "value", f"{path}.match"),
            "case_sensitive": _bool(
                match,
                "case_sensitive",
                f"{path}.match",
            ),
        }
        return projected
    if match_type == "regex":
        _exact_keys(match, {"type", "pattern", "flags"}, f"{path}.match")
        pattern = _string(match, "pattern", f"{path}.match")
        if _is_unbounded_regex(pattern):
            raise InputError(f"{path}.match.pattern is unbounded.")
        flags = list(_string_list(match, "flags", f"{path}.match", allow_empty=True))
        if not set(flags) <= {"IGNORECASE", "MULTILINE"}:
            raise InputError(f"{path}.match.flags contains unsupported values.")
        _require_unique(flags, f"{path}.match.flags")
        projected["match"] = {
            "type": "regex",
            "pattern": pattern,
            "flags": flags,
        }
        return projected
    raise InputError(f"{path}.match.type is unsupported.")


def _validate_proposal_evidence(
    proposal: Mapping[str, Any],
    path: str,
    matching_files: tuple[str, ...],
    evidence_points: frozenset[tuple[str, int]],
) -> None:
    confidence = proposal.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise InputError(f"{path}.confidence must be between 0 and 1.")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise InputError(f"{path}.evidence must be a non-empty list.")
    seen: set[tuple[str, int]] = set()
    for index, raw in enumerate(evidence):
        item = _mapping(raw, f"{path}.evidence[{index}]")
        _exact_keys(
            item,
            {"source_file", "physical_line_number"},
            f"{path}.evidence[{index}]",
        )
        source = _string(item, "source_file", f"{path}.evidence[{index}]")
        line = item.get("physical_line_number")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise InputError(
                f"{path}.evidence[{index}].physical_line_number is invalid."
            )
        point = (source, line)
        if source not in matching_files or point not in evidence_points:
            raise InputError(
                f"{path}.evidence[{index}] is not present in analysis evidence."
            )
        if point in seen:
            raise InputError(f"{path}.evidence contains a duplicate line reference.")
        seen.add(point)


def _build_validations(
    controls: tuple[ControlTotal, ...],
    matching_files: tuple[str, ...],
) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = [
        {
            "name": "matching_files",
            "type": "matching_files",
            "require_every_matching_file": True,
            "allow_missing_matching_files": False,
        },
        {
            "name": "table_headers",
            "type": "table_headers",
            "require_unique_table_headers": True,
            "require_exactly_one_output_header": True,
        },
        {
            "name": "table_regions",
            "type": "table_regions",
            "require_non_overlapping_table_regions": True,
        },
        {
            "name": "output_rows",
            "type": "output_rows",
            "require_consistent_field_count": True,
            "require_expected_column_order": True,
            "minimum_data_rows_per_table": 1,
            "reject_unresolved_rows": True,
            "preserve_leading_zero_values": True,
        },
        {
            "name": "deterministic_reruns",
            "type": "deterministic_reruns",
            "count": 2,
        },
    ]
    control_names: list[str] = []
    for control in controls:
        if not _SAFE_NAME.fullmatch(control.name):
            raise InputError(f"Control-total name '{control.name}' is invalid.")
        control_names.append(control.name)
        expected = sorted(
            (
                {
                    "source_file": item.source_file,
                    "value": item.value,
                }
                for item in control.expected_values
            ),
            key=lambda item: item["source_file"],
        )
        if sorted(item["source_file"] for item in expected) != list(matching_files):
            raise InputError(
                f"Control-total '{control.name}' must define every matching file."
            )
        validations.append(
            {
                "name": control.name,
                "type": "control_total",
                "table": control.table,
                "column": control.column,
                "aggregation": "sum",
                "tolerance": control.tolerance,
                "expected_values": expected,
            }
        )
    _require_unique(control_names, "control-total names")
    if set(control_names) & {
        "matching_files",
        "table_headers",
        "table_regions",
        "output_rows",
        "deterministic_reruns",
    }:
        raise InputError("Control-total names collide with standard validations.")
    return validations


def _build_rule_set_item(
    *,
    request: MaterializationRequest,
    versioned_name: str,
    analysis: Mapping[str, Any],
    tables: list[dict[str, Any]],
    validations: list[dict[str, Any]],
    lineage: dict[str, str],
    matching_file_count: int,
) -> dict[str, Any]:
    header = _render_header(
        list(_string_list(analysis, "ordered_headers", "analysis artifact")),
        request.delimiter,
    )
    return {
        "name": versioned_name,
        "schema_version": 1,
        "rule_set_name": request.rule_set_name,
        "version": request.version,
        "status": "approved",
        "enabled": True,
        "file_type": analysis["file_type"],
        "file_references": [
            {
                "name": (
                    "authoritative_filename_group_and_header"
                    if request.file_patterns
                    else "authoritative_header"
                ),
                **(
                    {"file_pattern": list(request.file_patterns)}
                    if request.file_patterns
                    else {}
                ),
                "header": header,
            }
        ],
        "source_format": {
            "layout": "delimited",
            "delimiter": request.delimiter,
            "encoding": request.encoding,
        },
        "structural_normalization": {
            "enabled": True,
            "mode": "rules",
            "default_action": "keep",
            "unmatched_row_policy": "drop_outside_tables",
            "tables": tables,
            "validations": validations,
        },
        "lineage": lineage,
        "approval": {
            "status": "passed",
            "validated_file_count": matching_file_count,
            "matching_file_count": matching_file_count,
            "unresolved_ambiguities": [],
            "deterministic_output_hashes_match": True,
            "approved_by": request.approved_by,
            "approved_at": request.approved_at,
        },
        "canonicalization": {
            "algorithm": _CANONICAL_ALGORITHM,
            "rule_sha256": "0" * 64,
        },
    }


def _resolve_generated_item(
    item: Mapping[str, Any],
    versioned_name: str,
) -> FileOperatorRuleSet:
    try:
        return resolve_file_operator_rule_set(
            SimpleNamespace(data_engine_rule_sets=[item]),
            versioned_name,
        )
    except Exception as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"Generated File Operator rule set is invalid: {exc}") from exc


def _render_header(columns: list[str], delimiter: str) -> str:
    return render_delimited_line(columns, delimiter)


def _is_unbounded_regex(pattern: str) -> bool:
    return any(
        re.search(expression, pattern)
        for expression in (
            r"\.\*",
            r"\.\+",
            r"\[[^\]]+\][*+]",
            r"\([^)]*\)[*+]",
        )
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read {label} '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label.capitalize()} '{path}' must contain one object.")
    return value


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputError(f"{path} must be a mapping.")
    return value


def _list(value: Mapping[str, Any], key: str, path: str) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise InputError(f"{path}.{key} must be a list.")
    return result


def _string(value: Mapping[str, Any], key: str, path: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise InputError(f"{path}.{key} must be a non-empty string.")
    return result.strip()


def _string_list(
    value: Mapping[str, Any],
    key: str,
    path: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    raw = _list(value, key, path)
    if not raw and not allow_empty:
        raise InputError(f"{path}.{key} must not be empty.")
    result = tuple(item.strip() if isinstance(item, str) else "" for item in raw)
    if any(not item for item in result):
        raise InputError(f"{path}.{key} must contain only non-empty strings.")
    return result


def _bool(value: Mapping[str, Any], key: str, path: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise InputError(f"{path}.{key} must be a boolean.")
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise InputError(f"{path} is missing fields: {', '.join(missing)}.")
    if unknown:
        raise InputError(f"{path} contains unknown fields: {', '.join(unknown)}.")


def _require_unique(values: list[Any], path: str) -> None:
    if len(set(values)) != len(values):
        raise InputError(f"{path} must be unique.")
