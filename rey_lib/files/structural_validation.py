"""Deterministic validation of structural normalization candidates.

The validator consumes reversible candidate outputs produced by
``structural_normalization``.  It executes the named validation list in order
and writes pass/fail evidence only.  It does not publish, activate, classify,
invoke Analyzer, or register workflow behavior.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.csv import CsvReadError, parse_delimited_line
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.profiling import normalized_header

from rey_lib.files.rule_sets import (
    FileOperatorRuleSet,
    ValidationDefinition,
)
from rey_lib.files.structural_normalization import (
    NormalizationOutput,
    NormalizationResult,
    execute_structural_normalization,
)

__all__ = [
    "CandidateValidationResult",
    "ValidationCheck",
    "validate_structural_normalization_candidates",
]


@dataclass(frozen=True)
class ValidationCheck:
    """One configured named validation result."""

    name: str
    type: str
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class CandidateValidationResult:
    """Overall validation result and its deterministic evidence artifact."""

    status: str
    checks: tuple[ValidationCheck, ...]
    evidence_path: Path
    evidence_sha256: str


@dataclass(frozen=True)
class _OutputView:
    result: NormalizationOutput
    lines: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    actual_sha256: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _CandidateView:
    result: NormalizationResult
    source_lines: tuple[str, ...]
    outputs: dict[str, _OutputView]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _ValidationContext:
    rule_set: FileOperatorRuleSet
    candidates: tuple[NormalizationResult, ...]
    expected_source_files: tuple[str, ...]
    views: dict[str, _CandidateView]
    validation_dir: Path
    control_total_scope: str


def validate_structural_normalization_candidates(
    run_log: Any,
    rule_set: FileOperatorRuleSet,
    candidates: tuple[NormalizationResult, ...] | list[NormalizationResult],
    expected_source_files: tuple[str, ...] | list[str],
    validation_dir: Path,
    *,
    control_total_scope: str = "complete",
) -> CandidateValidationResult:
    """Run every named validation and write deterministic pass/fail evidence."""

    if control_total_scope not in {"complete", "matching"}:
        raise ConfigError("control_total_scope must be 'complete' or 'matching'.")
    validation_dir = Path(validation_dir).expanduser().resolve()
    validation_dir.mkdir(parents=True, exist_ok=True)
    candidate_tuple = tuple(candidates)
    expected = tuple(sorted(Path(name).name for name in expected_source_files))
    context = _ValidationContext(
        rule_set=rule_set,
        candidates=candidate_tuple,
        expected_source_files=expected,
        views=_load_candidate_views(rule_set, candidate_tuple),
        validation_dir=validation_dir,
        control_total_scope=control_total_scope,
    )

    handlers: dict[
        str,
        Callable[[_ValidationContext, Any, ValidationDefinition], ValidationCheck],
    ] = {
        "matching_files": _validate_matching_files,
        "table_headers": _validate_table_headers,
        "table_regions": _validate_table_regions,
        "output_rows": _validate_output_rows,
        "deterministic_reruns": _validate_deterministic_reruns,
        "control_total": _validate_control_total,
    }
    checks: list[ValidationCheck] = []
    for validation in rule_set.structural_normalization.validations:
        handler = handlers.get(validation.type)
        if handler is None:
            checks.append(
                _failed_check(
                    validation,
                    errors=[f"Unsupported validation type '{validation.type}'."],
                )
            )
            continue
        checks.append(handler(context, run_log, validation))

    status = "passed" if all(check.status == "passed" for check in checks) else "failed"
    evidence = _build_validation_evidence(context, checks, status)
    evidence_text = (
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    evidence_path = validation_dir / (f"{rule_set.file_type}.rule_validation.json")
    atomic_write_text(evidence_path, evidence_text)
    return CandidateValidationResult(
        status=status,
        checks=tuple(checks),
        evidence_path=evidence_path,
        evidence_sha256=sha256_text(evidence_text),
    )


def _load_candidate_views(
    rule_set: FileOperatorRuleSet,
    candidates: tuple[NormalizationResult, ...],
) -> dict[str, _CandidateView]:
    views: dict[str, _CandidateView] = {}
    for candidate in sorted(candidates, key=lambda item: item.source_path.name):
        source_name = candidate.source_path.name
        errors: list[str] = []
        if source_name in views:
            continue
        try:
            source_text = read_text_file(
                candidate.source_path,
                encoding=rule_set.source_format.encoding,
            )
            source_lines = tuple(source_text.splitlines())
        except (OSError, UnicodeError) as exc:
            source_lines = ()
            errors.append(f"Cannot read source candidate '{source_name}': {exc}")

        output_views: dict[str, _OutputView] = {}
        for output in candidate.outputs:
            if output.table_name in output_views:
                errors.append(
                    f"Duplicate table output '{output.table_name}' for "
                    f"'{source_name}'."
                )
                continue
            output_views[output.table_name] = _load_output_view(
                output,
                rule_set.source_format.delimiter,
            )
        views[source_name] = _CandidateView(
            result=candidate,
            source_lines=source_lines,
            outputs=output_views,
            errors=tuple(errors),
        )
    return views


def _load_output_view(
    output: NormalizationOutput,
    delimiter: str,
) -> _OutputView:
    errors: list[str] = []
    try:
        content = read_text_file(output.output_path, encoding="utf-8")
        lines = tuple(content.splitlines())
    except (OSError, UnicodeError) as exc:
        return _OutputView(
            result=output,
            lines=(),
            rows=(),
            actual_sha256="",
            errors=(f"Cannot read candidate output '{output.output_path}': {exc}",),
        )

    rows: list[tuple[str, ...]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            rows.append(tuple(parse_delimited_line(line, delimiter, strict=True)))
        except CsvReadError as exc:
            errors.append(
                f"Cannot parse candidate '{output.output_path.name}' line "
                f"{line_number}: {exc}"
            )
    return _OutputView(
        result=output,
        lines=lines,
        rows=tuple(rows),
        actual_sha256=sha256_text(content),
        errors=tuple(errors),
    )


def _validate_matching_files(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    actual_names = [candidate.source_path.name for candidate in context.candidates]
    actual_set = set(actual_names)
    expected_set = set(context.expected_source_files)
    duplicate_expected = _duplicates(context.expected_source_files)
    duplicate_actual = _duplicates(tuple(actual_names))
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    errors: list[str] = []
    if duplicate_expected:
        errors.append(
            "Authoritative expected source files contain duplicates: "
            + ", ".join(duplicate_expected)
        )
    if duplicate_actual:
        errors.append(
            "Candidate results contain duplicate source files: "
            + ", ".join(duplicate_actual)
        )
    if missing:
        errors.append("Missing candidate source files: " + ", ".join(missing))
    if unexpected:
        errors.append("Unexpected candidate source files: " + ", ".join(unexpected))
    if not context.expected_source_files:
        errors.append("Authoritative expected source-file set is empty.")
    return _check(
        validation,
        errors,
        expected_source_files=list(context.expected_source_files),
        candidate_source_files=sorted(actual_names),
        missing=missing,
        unexpected=unexpected,
    )


def _validate_table_headers(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    errors: list[str] = []
    expected_identities = [
        tuple(normalized_header(table.expected_columns))
        for table in context.rule_set.structural_normalization.tables
    ]
    if len(set(expected_identities)) != len(expected_identities):
        errors.append("Configured table headers are not unique.")

    header_counts: list[dict[str, Any]] = []
    for source_name, view in sorted(context.views.items()):
        errors.extend(view.errors)
        for table in context.rule_set.structural_normalization.tables:
            output = view.outputs.get(table.name)
            if output is None:
                errors.append(
                    f"{source_name}: missing output for table '{table.name}'."
                )
                continue
            errors.extend(output.errors)
            expected = tuple(normalized_header(table.expected_columns))
            matching_count = sum(
                tuple(normalized_header(row)) == expected for row in output.rows
            )
            first_matches = bool(
                output.rows and tuple(normalized_header(output.rows[0])) == expected
            )
            header_counts.append(
                {
                    "source_file": source_name,
                    "table": table.name,
                    "matching_header_count": matching_count,
                    "first_row_matches": first_matches,
                }
            )
            if matching_count != 1:
                errors.append(
                    f"{source_name}:{table.name} contains {matching_count} "
                    "matching output headers; expected exactly 1."
                )
            if not first_matches:
                errors.append(
                    f"{source_name}:{table.name} first output row does not "
                    "match expected columns."
                )
    return _check(validation, errors, header_counts=header_counts)


def _validate_table_regions(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    errors: list[str] = []
    regions: list[dict[str, Any]] = []
    expected_tables = [
        table.name for table in context.rule_set.structural_normalization.tables
    ]
    for source_name, view in sorted(context.views.items()):
        actual_tables = [output.table_name for output in view.result.outputs]
        if actual_tables != expected_tables:
            errors.append(
                f"{source_name}: candidate table order/names {actual_tables} "
                f"do not equal configured tables {expected_tables}."
            )
        output_by_name = {output.table_name: output for output in view.result.outputs}
        previous_end = 0
        for table_name in expected_tables:
            output = output_by_name.get(table_name)
            if output is None:
                errors.append(
                    f"{source_name}: missing region for table '{table_name}'."
                )
                continue
            region = {
                "source_file": source_name,
                "table": table_name,
                "start_line": output.region_start_line,
                "end_line_exclusive": output.region_end_line_exclusive,
            }
            regions.append(region)
            if output.region_start_line < previous_end:
                errors.append(
                    f"{source_name}:{table_name} overlaps the previous table region."
                )
            if output.region_end_line_exclusive <= output.region_start_line:
                errors.append(
                    f"{source_name}:{table_name} has an empty or reversed region."
                )
            if output.header_line_number != output.region_start_line:
                errors.append(
                    f"{source_name}:{table_name} header does not start its region."
                )
            if output.region_end_line_exclusive > len(view.source_lines) + 1:
                errors.append(
                    f"{source_name}:{table_name} ends beyond the source file."
                )
            previous_end = max(previous_end, output.region_end_line_exclusive)
    return _check(validation, errors, regions=regions)


def _validate_output_rows(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    errors: list[str] = []
    summaries: list[dict[str, Any]] = []
    minimum_rows = int(validation.settings["minimum_data_rows_per_table"])
    expected_tables = {
        table.name: table for table in context.rule_set.structural_normalization.tables
    }

    for source_name, view in sorted(context.views.items()):
        actual_tables = [output.table_name for output in view.result.outputs]
        if actual_tables != list(expected_tables):
            errors.append(
                f"{source_name}: candidate table order/names {actual_tables} "
                f"do not equal configured tables {list(expected_tables)}."
            )
        for table_name, table in expected_tables.items():
            output = view.outputs.get(table_name)
            if output is None:
                errors.append(
                    f"{source_name}: missing output for table '{table_name}'."
                )
                continue
            errors.extend(output.errors)
            expected_width = len(table.expected_columns)
            # A row is valid when it carries AT MOST the columns the header
            # names: len(row) <= expected_width. Fewer is an export that stopped
            # emitting its trailing columns; more has a value in a position with
            # no column at all.
            ragged_lines = [
                index
                for index, row in enumerate(output.rows, start=1)
                if not len(row) <= expected_width
            ]
            if ragged_lines:
                errors.append(
                    f"{source_name}:{table_name} has rows carrying more fields "
                    f"than the header names, at output lines {ragged_lines}."
                )
            data_rows = max(0, len(output.rows) - 1)
            if data_rows < minimum_rows:
                errors.append(
                    f"{source_name}:{table_name} has {data_rows} data rows; "
                    f"minimum is {minimum_rows}."
                )
            if output.actual_sha256 != output.result.output_sha256:
                errors.append(
                    f"{source_name}:{table_name} output hash does not match "
                    "candidate evidence."
                )
            if len(output.lines) != output.result.output_row_count:
                errors.append(
                    f"{source_name}:{table_name} row count does not match "
                    "candidate evidence."
                )

            kept_hashes = [
                decision.row_sha256
                for decision in output.result.decisions
                if decision.action == "keep"
            ]
            actual_hashes = [sha256_text(line) for line in output.lines]
            if kept_hashes != actual_hashes:
                errors.append(
                    f"{source_name}:{table_name} output rows are not exact "
                    "kept source rows."
                )
            summaries.append(
                {
                    "source_file": source_name,
                    "table": table_name,
                    "output_rows": len(output.rows),
                    "data_rows": data_rows,
                    "ragged_output_lines": ragged_lines,
                    "actual_sha256": output.actual_sha256,
                    "evidence_sha256": output.result.output_sha256,
                    "exact_kept_rows": kept_hashes == actual_hashes,
                }
            )
    return _check(validation, errors, outputs=summaries)


def _validate_deterministic_reruns(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    count = int(validation.settings["count"])
    errors: list[str] = []
    hashes_by_run: list[dict[str, dict[str, str]]] = [
        _candidate_hashes(context.candidates)
    ]
    for run_number in range(2, count + 1):
        with tempfile.TemporaryDirectory(
            prefix=f"file_operator_rerun_{run_number}_",
            dir=context.validation_dir,
        ) as temporary:
            run_results: list[NormalizationResult] = []
            for candidate in sorted(
                context.candidates,
                key=lambda item: item.source_path.name,
            ):
                try:
                    run_results.append(
                        execute_structural_normalization(
                            None, run_log,
                            context.rule_set,
                            candidate.source_path,
                            Path(temporary) / candidate.source_path.stem,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"Rerun {run_number} failed for "
                        f"'{candidate.source_path.name}': "
                        f"{type(exc).__name__}."
                    )
            hashes_by_run.append(_candidate_hashes(tuple(run_results)))

    baseline = hashes_by_run[0]
    for index, hashes in enumerate(hashes_by_run[1:], start=2):
        if hashes != baseline:
            errors.append(
                f"Rerun {index} output hashes do not match the candidate baseline."
            )
    return _check(
        validation,
        errors,
        rerun_count=count,
        hashes_by_run=hashes_by_run,
        deterministic=not errors,
    )


def _validate_control_total(
    context: _ValidationContext,
    run_log: Any,
    validation: ValidationDefinition,
) -> ValidationCheck:
    table_name = str(validation.settings["table"])
    column_name = str(validation.settings["column"])
    tolerance_text = str(validation.settings["tolerance"])
    errors: list[str] = []
    totals: list[dict[str, Any]] = []
    try:
        tolerance = Decimal(tolerance_text)
    except InvalidOperation:
        return _failed_check(
            validation,
            errors=[f"Invalid control-total tolerance '{tolerance_text}'."],
        )
    if tolerance < 0:
        return _failed_check(
            validation,
            errors=["Control-total tolerance must not be negative."],
        )

    expected_by_source = {
        str(item["source_file"]): str(item["value"])
        for item in validation.settings["expected_values"]
    }
    if context.control_total_scope == "matching":
        expected_by_source = {
            source_file: value
            for source_file, value in expected_by_source.items()
            if source_file in context.views
        }
    for source_name, view in sorted(context.views.items()):
        output = view.outputs.get(table_name)
        if output is None:
            errors.append(f"{source_name}: missing control-total table '{table_name}'.")
            continue
        errors.extend(output.errors)
        if source_name not in expected_by_source:
            errors.append(f"{source_name}: missing expected control-total value.")
            continue
        if not output.rows:
            errors.append(f"{source_name}:{table_name} has no output header.")
            continue
        normalized_columns = normalized_header(output.rows[0])
        normalized_column = normalized_header([column_name])[0]
        try:
            column_index = normalized_columns.index(normalized_column)
        except ValueError:
            errors.append(
                f"{source_name}:{table_name} is missing control column "
                f"'{column_name}'."
            )
            continue

        actual = Decimal("0")
        parse_failed = False
        for line_number, row in enumerate(output.rows[1:], start=2):
            if column_index >= len(row):
                errors.append(
                    f"{source_name}:{table_name} output line {line_number} "
                    "does not contain the control column."
                )
                parse_failed = True
                continue
            try:
                actual += Decimal(row[column_index].strip())
            except InvalidOperation:
                errors.append(
                    f"{source_name}:{table_name} output line {line_number} "
                    f"has non-numeric control value '{row[column_index]}'."
                )
                parse_failed = True
        try:
            expected = Decimal(expected_by_source[source_name])
        except InvalidOperation:
            errors.append(
                f"{source_name}: configured expected control total is invalid."
            )
            continue
        difference = abs(actual - expected)
        passed = not parse_failed and difference <= tolerance
        totals.append(
            {
                "source_file": source_name,
                "table": table_name,
                "column": column_name,
                "aggregation": "sum",
                "actual": str(actual),
                "expected": str(expected),
                "difference": str(difference),
                "tolerance": str(tolerance),
                "passed": passed,
            }
        )
        if not passed:
            errors.append(
                f"{source_name}:{table_name} control total differs by "
                f"{difference}, exceeding tolerance {tolerance}."
            )
    unexpected_expected = sorted(set(expected_by_source) - set(context.views))
    if unexpected_expected:
        errors.append(
            "Control-total expectations have no candidate source: "
            + ", ".join(unexpected_expected)
        )
    return _check(validation, errors, totals=totals)


def _candidate_hashes(
    candidates: tuple[NormalizationResult, ...],
) -> dict[str, dict[str, str]]:
    return {
        candidate.source_path.name: {
            output.table_name: output.output_sha256 for output in candidate.outputs
        }
        for candidate in sorted(candidates, key=lambda item: item.source_path.name)
    }


def _build_validation_evidence(
    context: _ValidationContext,
    checks: list[ValidationCheck],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "file_type": context.rule_set.file_type,
        "rule_set": {
            "name": context.rule_set.name,
            "rule_set_name": context.rule_set.rule_set_name,
            "version": context.rule_set.version,
            "rule_sha256": context.rule_set.rule_sha256,
        },
        "expected_source_files": list(context.expected_source_files),
        "candidate_source_files": sorted(context.views),
        "validation_count": len(checks),
        "validations": [
            {
                "name": check.name,
                "type": check.type,
                "status": check.status,
                "details": check.details,
            }
            for check in checks
        ],
    }


def _check(
    validation: ValidationDefinition,
    errors: list[str],
    **details: Any,
) -> ValidationCheck:
    return ValidationCheck(
        name=validation.name,
        type=validation.type,
        status="failed" if errors else "passed",
        details={"errors": errors, **details},
    )


def _failed_check(
    validation: ValidationDefinition,
    *,
    errors: list[str],
) -> ValidationCheck:
    return _check(validation, errors)


def _duplicates(values: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)
