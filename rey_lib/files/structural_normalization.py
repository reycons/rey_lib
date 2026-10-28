"""Deterministic structural normalization for approved File Operator rules.

This executor consumes one already-resolved ``FileOperatorRuleSet`` and one
delimited source file.  It performs no classification, Analyzer invocation,
workflow registration, validation, or promotion.  Candidate outputs and
decision evidence are written to the caller-supplied directory; the source is
never modified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from rey_lib.encryption import sha256_text
from rey_lib.files.csv import CsvReadError, parse_delimited_line
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import log_artifact_reference
from rey_lib.profiling import normalized_header

from rey_lib.files.error_utils import InputError
from rey_lib.files.rule_sets import (
    FieldMatch,
    FileOperatorRuleSet,
    RowRule,
    TableDefinition,
)

__all__ = [
    "NormalizationOutput",
    "NormalizationResult",
    "execute_structural_normalization",
    "find_structural_match",
    "matches_structural_fields",
    "matches_structural_row_rule",
]

_REGEX_FLAGS = {
    "ASCII": re.ASCII,
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
}
_RowT = TypeVar("_RowT")


@dataclass(frozen=True)
class SourceRow:
    """One line-oriented delimited source row."""

    index: int
    physical_line_number: int
    logical_row_number: int
    text: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RowDecision:
    """One deterministic decision for a row inside a table region."""

    line_number: int
    action: str
    rule_name: str | None
    rule_order: int | None
    rule_kind: str | None
    row_sha256: str


@dataclass(frozen=True)
class NormalizationOutput:
    """One candidate table output and its deterministic evidence."""

    table_name: str
    output_path: Path
    output_sha256: str
    region_start_line: int
    region_end_line_exclusive: int
    header_line_number: int
    output_row_count: int
    data_row_count: int
    dropped_row_count: int
    decisions: tuple[RowDecision, ...]


@dataclass(frozen=True)
class NormalizationResult:
    """Candidate outputs and evidence produced for one source file."""

    source_path: Path
    source_text_sha256: str
    rule_set_name: str
    outputs: tuple[NormalizationOutput, ...]
    outside_table_line_numbers: tuple[int, ...]
    evidence_path: Path


@dataclass(frozen=True)
class _PreparedOutput:
    result: NormalizationOutput
    content: str


def execute_structural_normalization(
    ctx: Any, run_log,
    rule_set: FileOperatorRuleSet,
    source_path: Path,
    candidate_dir: Path,
) -> NormalizationResult:
    """Apply one approved rule set and write reversible candidate artifacts."""

    source_path = Path(source_path).expanduser().resolve()
    candidate_dir = Path(candidate_dir).expanduser().resolve()
    _require_executable_rule_set(rule_set)
    source_text = _read_source_text(source_path, rule_set)
    source_text_sha256 = sha256_text(source_text)
    rows = _parse_source_rows(
        source_text,
        delimiter=rule_set.source_format.delimiter,
        source_path=source_path,
    )

    prepared, covered_indices = _prepare_outputs(
        rule_set,
        source_path,
        candidate_dir,
        rows,
    )
    outside_lines = tuple(
        row.physical_line_number for row in rows if row.index not in covered_indices
    )
    evidence_path = candidate_dir / (
        f"{source_path.stem}.structural_normalization.json"
    )
    output_paths = {item.result.output_path for item in prepared}
    if evidence_path in output_paths or evidence_path == source_path:
        raise InputError(
            f"Structural normalization evidence path collides with another "
            f"artifact: '{evidence_path}'."
        )

    candidate_dir.mkdir(parents=True, exist_ok=True)
    for item in prepared:
        atomic_write_text(item.result.output_path, item.content)
    evidence = _build_evidence(
        rule_set,
        source_path,
        source_text_sha256,
        prepared,
        outside_lines,
    )
    atomic_write_text(
        evidence_path,
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    if ctx is not None:
        _log_candidate_artifacts(ctx, run_log, source_path, prepared, evidence_path)

    return NormalizationResult(
        source_path=source_path,
        source_text_sha256=source_text_sha256,
        rule_set_name=rule_set.name,
        outputs=tuple(item.result for item in prepared),
        outside_table_line_numbers=outside_lines,
        evidence_path=evidence_path,
    )


def _require_executable_rule_set(rule_set: FileOperatorRuleSet) -> None:
    if not rule_set.enabled:
        raise InputError(f"File Operator rule set '{rule_set.name}' is disabled.")
    if rule_set.status != "approved":
        raise InputError(f"File Operator rule set '{rule_set.name}' is not approved.")
    normalization = rule_set.structural_normalization
    if not normalization.enabled or normalization.mode != "rules":
        raise InputError(
            f"File Operator rule set '{rule_set.name}' does not enable "
            "rules-based structural normalization."
        )


def _read_source_text(
    source_path: Path,
    rule_set: FileOperatorRuleSet,
) -> str:
    if not source_path.is_file():
        raise InputError(
            f"Structural normalization source does not exist: '{source_path}'."
        )
    try:
        return read_text_file(
            source_path,
            encoding=rule_set.source_format.encoding,
        )
    except (OSError, UnicodeError) as exc:
        raise InputError(
            f"Cannot read structural normalization source '{source_path}': {exc}"
        ) from exc


def _parse_source_rows(
    source_text: str,
    *,
    delimiter: str,
    source_path: Path,
) -> tuple[SourceRow, ...]:
    rows: list[SourceRow] = []
    for index, line in enumerate(source_text.splitlines()):
        try:
            fields = tuple(
                parse_delimited_line(line, delimiter, strict=True)
            )
        except CsvReadError as exc:
            raise InputError(
                f"Cannot parse source '{source_path}' at physical line "
                f"{index + 1}: {exc}"
            ) from exc
        rows.append(
            SourceRow(
                index=index,
                physical_line_number=index + 1,
                logical_row_number=index + 1,
                text=line,
                fields=fields,
            )
        )
    return tuple(rows)


def _prepare_outputs(
    rule_set: FileOperatorRuleSet,
    source_path: Path,
    candidate_dir: Path,
    rows: tuple[SourceRow, ...],
) -> tuple[tuple[_PreparedOutput, ...], set[int]]:
    if not rows:
        raise InputError(f"Structural normalization source '{source_path}' is empty.")

    prepared: list[_PreparedOutput] = []
    covered_indices: set[int] = set()
    output_paths: set[Path] = set()
    cursor = 0

    for table in rule_set.structural_normalization.tables:
        start = _find_matching_row(rows, cursor, table.header_match)
        if start is None:
            raise InputError(
                f"Table '{table.name}' header was not found at or after "
                f"physical line {cursor + 1} in '{source_path.name}'."
            )
        if table.end_type == "eof":
            end = len(rows)
        else:
            if table.end_match is None:
                raise InputError(
                    f"Table '{table.name}' before_match boundary has no matcher."
                )
            boundary = _find_matching_row(rows, start + 1, table.end_match)
            if boundary is None:
                raise InputError(
                    f"Table '{table.name}' end boundary was not found after "
                    f"physical line {start + 1} in '{source_path.name}'."
                )
            end = boundary
        if end <= start:
            raise InputError(
                f"Table '{table.name}' resolved an empty or reversed region."
            )

        output_path = _resolve_output_path(
            source_path,
            candidate_dir,
            table,
        )
        if output_path in output_paths:
            raise InputError(
                f"Structural normalization output collision: '{output_path}'."
            )
        if output_path == source_path:
            raise InputError(
                f"Structural normalization output would overwrite its source: "
                f"'{source_path}'."
            )
        output_paths.add(output_path)

        prepared.append(
            _prepare_table_output(
                rule_set,
                table,
                rows,
                start,
                end,
                output_path,
            )
        )
        covered_indices.update(range(start, end))
        cursor = end

    return tuple(prepared), covered_indices


def _prepare_table_output(
    rule_set: FileOperatorRuleSet,
    table: TableDefinition,
    rows: tuple[SourceRow, ...],
    start: int,
    end: int,
    output_path: Path,
) -> _PreparedOutput:
    header = rows[start]
    kept_rows = [header]
    decisions: list[RowDecision] = [_decision(header, action="keep", rule=None)]

    for row in rows[start + 1 : end]:
        matched_rule = _first_matching_rule(table, row)
        if matched_rule is None:
            kept_rows.append(row)
            decisions.append(_decision(row, action="keep", rule=None))
        else:
            decisions.append(
                _decision(row, action=matched_rule.action, rule=matched_rule)
            )

    content = "\n".join(row.text for row in kept_rows) + "\n"
    output_sha256 = sha256_text(content)
    dropped = sum(decision.action == "drop" for decision in decisions)
    result = NormalizationOutput(
        table_name=table.name,
        output_path=output_path,
        output_sha256=output_sha256,
        region_start_line=rows[start].physical_line_number,
        region_end_line_exclusive=(
            rows[end].physical_line_number if end < len(rows) else len(rows) + 1
        ),
        header_line_number=header.physical_line_number,
        output_row_count=len(kept_rows),
        data_row_count=max(0, len(kept_rows) - 1),
        dropped_row_count=dropped,
        decisions=tuple(decisions),
    )
    return _PreparedOutput(result=result, content=content)


def _find_matching_row(
    rows: tuple[SourceRow, ...],
    start: int,
    matcher: FieldMatch,
) -> int | None:
    return find_structural_match(
        rows,
        start,
        lambda row: row.fields,
        match_type=matcher.type,
        values=matcher.values,
        case_sensitive=matcher.case_sensitive,
        trim_fields=matcher.trim_fields,
    )


def find_structural_match(
    rows: Sequence[_RowT],
    start: int,
    fields: Callable[[_RowT], Sequence[str]],
    *,
    match_type: str,
    values: Sequence[str],
    case_sensitive: bool,
    trim_fields: bool,
) -> int | None:
    """Return the first row matching canonical structural field semantics."""

    for index in range(start, len(rows)):
        if matches_structural_fields(
            fields(rows[index]),
            match_type=match_type,
            values=values,
            case_sensitive=case_sensitive,
            trim_fields=trim_fields,
        ):
            return index
    return None


def matches_structural_fields(
    fields: Sequence[str],
    *,
    match_type: str,
    values: Sequence[str],
    case_sensitive: bool,
    trim_fields: bool,
) -> bool:
    """Apply the one canonical File Operator structural field matcher."""

    actual = tuple(
        _normalize_match_field(
            field,
            case_sensitive=case_sensitive,
            trim_fields=trim_fields,
        )
        for field in fields
    )
    expected = tuple(
        _normalize_match_field(
            field,
            case_sensitive=case_sensitive,
            trim_fields=trim_fields,
        )
        for field in values
    )
    if match_type == "exact_fields":
        return actual == expected
    if match_type == "contains_fields":
        return _is_ordered_subsequence(expected, actual)
    raise InputError(f"Unsupported field match type '{match_type}'.")


def _normalize_match_field(
    value: str,
    *,
    case_sensitive: bool,
    trim_fields: bool,
) -> str:
    normalized = value.strip() if trim_fields else value
    return normalized if case_sensitive else normalized.casefold()


def _is_ordered_subsequence(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> bool:
    if not expected:
        return True
    position = 0
    for value in actual:
        if value == expected[position]:
            position += 1
            if position == len(expected):
                return True
    return False


def _first_matching_rule(
    table: TableDefinition,
    row: SourceRow,
) -> RowRule | None:
    for rule in table.row_rules:
        if _matches_row_rule(table, rule, row):
            return rule
    return None


def _matches_row_rule(
    table: TableDefinition,
    rule: RowRule,
    row: SourceRow,
) -> bool:
    return matches_structural_row_rule(
        fields=row.fields,
        text=row.text,
        is_blank=not row.text.strip(),
        expected_columns=table.expected_columns,
        rule_name=rule.name,
        standard=rule.standard,
        match_type=rule.match_type,
        value=rule.value,
        case_sensitive=rule.case_sensitive,
        pattern=rule.pattern,
        flags=rule.flags,
    )


def matches_structural_row_rule(
    *,
    fields: Sequence[str],
    text: str,
    is_blank: bool,
    expected_columns: Sequence[str],
    rule_name: str,
    standard: str | None,
    match_type: str | None,
    value: str | None,
    case_sensitive: bool | None,
    pattern: str | None,
    flags: Sequence[str],
) -> bool:
    """Apply the one canonical File Operator structural row-rule matcher."""

    if standard == "blank":
        return is_blank
    if standard == "repeated_header":
        return normalized_header(fields) == normalized_header(expected_columns)
    if match_type == "contains":
        if value is None or case_sensitive is None:
            raise InputError(f"Row rule '{rule_name}' contains matcher is incomplete.")
        haystack = text if case_sensitive else text.casefold()
        needle = value if case_sensitive else value.casefold()
        return needle in haystack
    if match_type == "regex":
        if pattern is None:
            raise InputError(f"Row rule '{rule_name}' regex matcher is incomplete.")
        compiled_flags = 0
        for flag in flags:
            try:
                compiled_flags |= _REGEX_FLAGS[flag]
            except KeyError as exc:
                raise InputError(
                    f"Row rule '{rule_name}' has unsupported regex flag '{flag}'."
                ) from exc
        return re.search(pattern, text, compiled_flags) is not None
    raise InputError(
        f"Row rule '{rule_name}' has no supported deterministic matcher."
    )


def _decision(
    row: SourceRow,
    *,
    action: str,
    rule: RowRule | None,
) -> RowDecision:
    if rule is None:
        rule_kind = None
    elif rule.standard is not None:
        rule_kind = f"standard:{rule.standard}"
    else:
        rule_kind = f"match:{rule.match_type}"
    return RowDecision(
        line_number=row.physical_line_number,
        action=action,
        rule_name=rule.name if rule is not None else None,
        rule_order=rule.order if rule is not None else None,
        rule_kind=rule_kind,
        row_sha256=sha256_text(row.text),
    )


def _resolve_output_path(
    source_path: Path,
    candidate_dir: Path,
    table: TableDefinition,
) -> Path:
    try:
        output_name = table.output_name.format(source_stem=source_path.stem)
    except (KeyError, ValueError) as exc:
        raise InputError(
            f"Table '{table.name}' output_name cannot be rendered: {exc}"
        ) from exc
    if (
        not output_name
        or Path(output_name).name != output_name
        or output_name in {".", ".."}
    ):
        raise InputError(
            f"Table '{table.name}' output_name must resolve to one filename."
        )
    return (candidate_dir / output_name).resolve()


def _build_evidence(
    rule_set: FileOperatorRuleSet,
    source_path: Path,
    source_text_sha256: str,
    prepared: tuple[_PreparedOutput, ...],
    outside_lines: tuple[int, ...],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_text_sha256": source_text_sha256,
        "rule_set": {
            "name": rule_set.name,
            "rule_set_name": rule_set.rule_set_name,
            "version": rule_set.version,
            "rule_sha256": rule_set.rule_sha256,
        },
        "unmatched_row_policy": (
            rule_set.structural_normalization.unmatched_row_policy
        ),
        "outside_table_line_numbers": list(outside_lines),
        "tables": [
            {
                "name": item.result.table_name,
                "output_path": str(item.result.output_path),
                "output_sha256": item.result.output_sha256,
                "region_start_line": item.result.region_start_line,
                "region_end_line_exclusive": (item.result.region_end_line_exclusive),
                "header_line_number": item.result.header_line_number,
                "output_row_count": item.result.output_row_count,
                "data_row_count": item.result.data_row_count,
                "dropped_row_count": item.result.dropped_row_count,
                "decisions": [
                    {
                        "line_number": decision.line_number,
                        "action": decision.action,
                        "rule_name": decision.rule_name,
                        "rule_order": decision.rule_order,
                        "rule_kind": decision.rule_kind,
                        "row_sha256": decision.row_sha256,
                    }
                    for decision in item.result.decisions
                ],
            }
            for item in prepared
        ],
    }


def _log_candidate_artifacts(
    ctx: Any, run_log,
    source_path: Path,
    prepared: tuple[_PreparedOutput, ...],
    evidence_path: Path,
) -> None:
    for item in prepared:
        log_artifact_reference(run_log,
            str(item.result.output_path),
            role="structural_normalization_candidate",
            producer="data_engine",
            artifact_group="candidate_outputs",
            producing_app="data_engine",
            artifact_type="normalized_csv_candidate",
            viewer_type="file",
            safe_to_preview=False,
            source_path=str(source_path),
            table_name=item.result.table_name,
            artifact_sha256=item.result.output_sha256,
            row_count=item.result.output_row_count,
        )
    log_artifact_reference(run_log,
        str(evidence_path),
        role="structural_normalization_evidence",
        producer="data_engine",
        artifact_group="candidate_outputs",
        producing_app="data_engine",
        artifact_type="structural_normalization_evidence",
        viewer_type="file",
        safe_to_preview=True,
        source_path=str(source_path),
    )
