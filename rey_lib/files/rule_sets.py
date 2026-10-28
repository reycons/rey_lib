"""Validation and resolution for durable File Operator rule sets.

The application context is already assembled by ``rey_lib.config``.  This
module validates that resolved ``ctx.data_engine_rule_sets`` surface and
returns immutable values for later inspection and execution stages.  It does
not read YAML, classify files, execute rules, or select Rey Loader transforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.encryption import sha256_text
from rey_lib.files.json import render_json

from rey_lib.files.error_utils import InputError, RedactorError

__all__ = [
    "FileOperatorRuleSet",
    "FileReference",
    "FieldMatch",
    "RowRule",
    "SourceFormat",
    "StructuralNormalization",
    "TableDefinition",
    "ValidationDefinition",
    "canonical_rule_sha256",
    "resolve_file_operator_rule_set",
    "resolve_file_operator_rule_sets",
]

_SECTION = "data_engine_rule_sets"
_VERSIONED_NAME = re.compile(
    r"^(?P<base>[A-Za-z0-9][A-Za-z0-9_.-]*)__v(?P<version>[0-9]{4,})$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEADER_MATCH_TYPES = frozenset({"exact_fields", "contains_fields"})
_ROW_STANDARDS = frozenset({"blank", "repeated_header"})
_ROW_MATCH_TYPES = frozenset({"contains", "regex"})
_REGEX_FLAGS = frozenset({"ASCII", "IGNORECASE", "MULTILINE", "DOTALL", "VERBOSE"})
_VALIDATION_TYPES = frozenset(
    {
        "matching_files",
        "table_headers",
        "table_regions",
        "output_rows",
        "deterministic_reruns",
        "control_total",
    }
)
_REQUIRED_VALIDATION_TYPES = frozenset(
    {
        "matching_files",
        "table_headers",
        "table_regions",
        "output_rows",
        "deterministic_reruns",
        "control_total",
    }
)
_CANONICAL_RULE_KEYS = (
    "name",
    "schema_version",
    "rule_set_name",
    "version",
    "file_type",
    "file_references",
    "source_format",
    "structural_normalization",
)


@dataclass(frozen=True)
class FileReference:
    """One alternative filename/header identity for a structural file type."""

    name: str
    file_patterns: tuple[str, ...]
    header: str | None


@dataclass(frozen=True)
class SourceFormat:
    """Delimited source decoding settings."""

    layout: str
    delimiter: str
    encoding: str


@dataclass(frozen=True)
class FieldMatch:
    """A normalized field-list matcher used by headers and boundaries."""

    type: str
    values: tuple[str, ...]
    case_sensitive: bool
    trim_fields: bool


@dataclass(frozen=True)
class RowRule:
    """One ordered deterministic row-discard rule."""

    name: str
    order: int
    action: str
    standard: str | None
    match_type: str | None
    value: str | None
    pattern: str | None
    case_sensitive: bool | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class TableDefinition:
    """One structural table region and its ordered row rules."""

    name: str
    output_name: str
    expected_columns: tuple[str, ...]
    header_match: FieldMatch
    keep_first_header: bool
    repeated_comparison: str
    repeated_action: str
    start_type: str
    end_type: str
    end_match: FieldMatch | None
    row_rules: tuple[RowRule, ...]


@dataclass(frozen=True)
class ValidationDefinition:
    """One named validation with type-specific immutable settings."""

    name: str
    type: str
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class StructuralNormalization:
    """Validated structural normalization behavior."""

    enabled: bool
    mode: str
    default_action: str
    unmatched_row_policy: str
    tables: tuple[TableDefinition, ...]
    validations: tuple[ValidationDefinition, ...]


@dataclass(frozen=True)
class Approval:
    """Evidence that a durable rule set passed deterministic validation."""

    status: str
    validated_file_count: int
    matching_file_count: int
    approved_by: str
    approved_at: str


@dataclass(frozen=True)
class FileOperatorRuleSet:
    """One fully validated, versioned durable File Operator rule set."""

    name: str
    schema_version: int
    rule_set_name: str
    version: int
    status: str
    enabled: bool
    file_type: str
    file_references: tuple[FileReference, ...]
    source_format: SourceFormat
    structural_normalization: StructuralNormalization
    lineage: Mapping[str, str]
    approval: Approval
    canonicalization_algorithm: str
    rule_sha256: str


def resolve_file_operator_rule_sets(ctx: Any) -> tuple[FileOperatorRuleSet, ...]:
    """Validate and return every resolved ``ctx.data_engine_rule_sets`` item."""

    entries = getattr(ctx, _SECTION, None)
    if entries is None:
        raise RedactorError(
            "Resolved context is missing required "
            "'data_engine_rule_sets' configuration."
        )
    if not isinstance(entries, list):
        raise RedactorError("Resolved ctx.data_engine_rule_sets must be a list.")
    if not entries:
        raise RedactorError("Resolved ctx.data_engine_rule_sets must not be empty.")

    resolved = tuple(
        _parse_rule_set(entry, index) for index, entry in enumerate(entries)
    )
    names = [rule_set.name for rule_set in resolved]
    _require_unique(names, "data_engine_rule_sets names")
    revisions = [(item.rule_set_name, item.version) for item in resolved]
    _require_unique(revisions, "data_engine_rule_sets revisions")
    return resolved


def resolve_file_operator_rule_set(ctx: Any, name: str) -> FileOperatorRuleSet:
    """Resolve exactly one durable rule set by its stable versioned name."""

    requested = _non_empty_string(name, "rule-set reference")
    matches = [
        rule_set
        for rule_set in resolve_file_operator_rule_sets(ctx)
        if rule_set.name == requested
    ]
    if not matches:
        raise RedactorError(
            f"File Operator rule set '{requested}' was not found in "
            "ctx.data_engine_rule_sets."
        )
    return matches[0]


def canonical_rule_sha256(entry: Any) -> str:
    """Hash the stable executable rule projection as canonical UTF-8 JSON.

    Activation state, lineage, and approval are not executable rule semantics.
    They are excluded so a revision can be activated/deactivated without
    invalidating its validation evidence, and so evidence can contain this hash
    before its own path/hash are attached. The canonicalization block is
    excluded to avoid hashing the digest into itself.
    """

    value = _mapping(entry, "data_engine_rule_set")
    missing = [key for key in _CANONICAL_RULE_KEYS if key not in value]
    if missing:
        raise _error(
            "data_engine_rule_set",
            "is missing canonical fields: " + ", ".join(missing),
        )
    projection = {key: _canonical_plain(value[key]) for key in _CANONICAL_RULE_KEYS}
    # The canonical renderer is defined as this representation, so the digest
    # is unchanged: every rule_sha256 already persisted stays valid.
    # rule_sets still decides what is hashed -- the projection and its canonical
    # rendering. Only the digest itself belongs elsewhere.
    return sha256_text(render_json(projection, mode="canonical"))


def _parse_rule_set(entry: Any, index: int) -> FileOperatorRuleSet:
    path = f"{_SECTION}[{index}]"
    value = _mapping(entry, path)
    _exact_keys(
        value,
        {
            "name",
            "schema_version",
            "rule_set_name",
            "version",
            "status",
            "enabled",
            "file_type",
            "file_references",
            "source_format",
            "structural_normalization",
            "lineage",
            "approval",
            "canonicalization",
        },
        path,
    )

    name = _required_string(value, "name", path)
    rule_set_name = _required_string(value, "rule_set_name", path)
    version = _positive_int(value, "version", path)
    match = _VERSIONED_NAME.fullmatch(name)
    if match is None:
        raise _error(
            path,
            "field 'name' must use '<rule_set_name>__v0001' versioned form.",
        )
    if match.group("base") != rule_set_name:
        raise _error(path, "field 'name' must start with the exact rule_set_name.")
    if int(match.group("version")) != version:
        raise _error(path, "field 'name' version must equal field 'version'.")

    schema_version = _positive_int(value, "schema_version", path)
    if schema_version != 1:
        raise _error(path, "field 'schema_version' must be 1.")
    status = _required_string(value, "status", path)
    if status != "approved":
        raise _error(path, "field 'status' must be 'approved'.")
    enabled = _required_bool(value, "enabled", path)

    references = _required_list(value, "file_references", path)
    if not references:
        raise _error(path, "field 'file_references' must not be empty.")
    parsed_references = tuple(
        _parse_file_reference(item, f"{path}.file_references[{offset}]")
        for offset, item in enumerate(references)
    )
    _require_unique(
        [reference.name for reference in parsed_references],
        f"{path}.file_references names",
    )
    for reference in parsed_references:
        _require_unique(
            reference.file_patterns,
            f"{path}.file_references '{reference.name}' file_pattern values",
        )

    source_format = _parse_source_format(
        _required_mapping(value, "source_format", path),
        f"{path}.source_format",
    )
    structural_normalization = _parse_structural_normalization(
        _required_mapping(value, "structural_normalization", path),
        f"{path}.structural_normalization",
    )
    lineage = _parse_lineage(
        _required_mapping(value, "lineage", path),
        f"{path}.lineage",
    )
    approval = _parse_approval(
        _required_mapping(value, "approval", path),
        f"{path}.approval",
    )
    canonicalization = _parse_canonicalization(
        _required_mapping(value, "canonicalization", path),
        f"{path}.canonicalization",
    )
    expected_rule_sha256 = canonical_rule_sha256(value)
    if canonicalization["rule_sha256"] != expected_rule_sha256:
        raise _error(
            f"{path}.canonicalization.rule_sha256",
            "does not match the canonical executable rule content.",
        )

    return FileOperatorRuleSet(
        name=name,
        schema_version=schema_version,
        rule_set_name=rule_set_name,
        version=version,
        status=status,
        enabled=enabled,
        file_type=_required_string(value, "file_type", path),
        file_references=parsed_references,
        source_format=source_format,
        structural_normalization=structural_normalization,
        lineage=lineage,
        approval=approval,
        **canonicalization,
    )


def _parse_file_reference(entry: Any, path: str) -> FileReference:
    value = _mapping(entry, path)
    _allowed_keys(
        value,
        required={"name"},
        optional={"file_pattern", "header"},
        path=path,
    )
    raw_patterns = value.get("file_pattern")
    if raw_patterns is None:
        patterns: tuple[str, ...] = ()
    elif isinstance(raw_patterns, str):
        patterns = (_non_empty_string(raw_patterns, f"{path}.file_pattern"),)
    elif isinstance(raw_patterns, list):
        if not raw_patterns:
            raise _error(path, "field 'file_pattern' list must not be empty.")
        patterns = tuple(
            _non_empty_string(pattern, f"{path}.file_pattern[{index}]")
            for index, pattern in enumerate(raw_patterns)
        )
    else:
        raise _error(path, "field 'file_pattern' must be a string or list.")

    header_value = value.get("header")
    header = (
        None
        if header_value is None
        else _non_empty_string(header_value, f"{path}.header")
    )
    if not patterns and header is None:
        raise _error(path, "must define file_pattern, header, or both.")
    return FileReference(
        name=_required_string(value, "name", path),
        file_patterns=patterns,
        header=header,
    )


def _parse_source_format(value: Mapping[str, Any], path: str) -> SourceFormat:
    _exact_keys(value, {"layout", "delimiter", "encoding"}, path)
    layout = _required_string(value, "layout", path)
    if layout != "delimited":
        raise _error(path, "field 'layout' must be 'delimited'.")
    delimiter = _required_string(value, "delimiter", path)
    if len(delimiter) != 1:
        raise _error(path, "field 'delimiter' must contain exactly one character.")
    return SourceFormat(
        layout=layout,
        delimiter=delimiter,
        encoding=_required_string(value, "encoding", path),
    )


def _parse_structural_normalization(
    value: Mapping[str, Any],
    path: str,
) -> StructuralNormalization:
    _exact_keys(
        value,
        {
            "enabled",
            "mode",
            "default_action",
            "unmatched_row_policy",
            "tables",
            "validations",
        },
        path,
    )
    if _required_string(value, "mode", path) != "rules":
        raise _error(path, "field 'mode' must be 'rules'.")
    if _required_string(value, "default_action", path) != "keep":
        raise _error(path, "field 'default_action' must be 'keep'.")
    if _required_string(value, "unmatched_row_policy", path) != "drop_outside_tables":
        raise _error(
            path,
            "field 'unmatched_row_policy' must be 'drop_outside_tables'.",
        )

    raw_tables = _required_list(value, "tables", path)
    if not raw_tables:
        raise _error(path, "field 'tables' must not be empty.")
    tables = tuple(
        _parse_table(table, f"{path}.tables[{index}]")
        for index, table in enumerate(raw_tables)
    )
    _require_unique([table.name for table in tables], f"{path}.tables names")
    _require_unique(
        [table.output_name for table in tables],
        f"{path}.tables output_name values",
    )
    for index, table in enumerate(tables):
        if table.end_type == "eof" and index != len(tables) - 1:
            raise _error(
                f"{path}.tables[{index}].end",
                "only the final table may use type 'eof'.",
            )

    raw_validations = _required_list(value, "validations", path)
    validations = tuple(
        _parse_validation(item, f"{path}.validations[{index}]")
        for index, item in enumerate(raw_validations)
    )
    _require_unique(
        [validation.name for validation in validations],
        f"{path}.validations names",
    )
    present_types = {validation.type for validation in validations}
    missing_types = sorted(_REQUIRED_VALIDATION_TYPES - present_types)
    if missing_types:
        raise _error(
            path,
            "field 'validations' is missing required types: "
            + ", ".join(missing_types),
        )
    for validation_type in _REQUIRED_VALIDATION_TYPES - {"control_total"}:
        count = sum(item.type == validation_type for item in validations)
        if count != 1:
            raise _error(
                path,
                f"validation type '{validation_type}' must appear exactly once.",
            )

    enabled = _required_bool(value, "enabled", path)
    if not enabled:
        raise _error(path, "field 'enabled' must be true.")

    table_by_name = {table.name: table for table in tables}
    for validation in validations:
        if validation.type != "control_total":
            continue
        table_name = validation.settings["table"]
        if table_name not in table_by_name:
            raise _error(
                path,
                f"control_total '{validation.name}' references unknown table "
                f"'{table_name}'.",
            )
        column = validation.settings["column"]
        if column not in table_by_name[table_name].expected_columns:
            raise _error(
                path,
                f"control_total '{validation.name}' references unknown column "
                f"'{column}' in table '{table_name}'.",
            )

    return StructuralNormalization(
        enabled=enabled,
        mode="rules",
        default_action="keep",
        unmatched_row_policy="drop_outside_tables",
        tables=tables,
        validations=validations,
    )


def _parse_table(entry: Any, path: str) -> TableDefinition:
    value = _mapping(entry, path)
    _exact_keys(
        value,
        {
            "name",
            "output_name",
            "expected_columns",
            "header",
            "start",
            "end",
            "row_rules",
        },
        path,
    )
    expected_columns = _string_list(value, "expected_columns", path, allow_empty=False)
    _require_unique(expected_columns, f"{path}.expected_columns")

    header = _required_mapping(value, "header", path)
    header_path = f"{path}.header"
    _exact_keys(header, {"match", "keep_first", "repeated"}, header_path)
    header_match = _parse_field_match(
        _required_mapping(header, "match", header_path),
        f"{header_path}.match",
    )
    if header_match.values != expected_columns:
        raise _error(
            header_path,
            "match values must equal expected_columns in the same order.",
        )
    repeated = _required_mapping(header, "repeated", header_path)
    repeated_path = f"{header_path}.repeated"
    _exact_keys(repeated, {"comparison", "action"}, repeated_path)
    comparison = _required_string(repeated, "comparison", repeated_path)
    action = _required_string(repeated, "action", repeated_path)
    if comparison != "normalized_exact" or action != "drop":
        raise _error(
            repeated_path,
            "must use comparison 'normalized_exact' and action 'drop'.",
        )

    start = _required_mapping(value, "start", path)
    start_path = f"{path}.start"
    _exact_keys(start, {"type"}, start_path)
    start_type = _required_string(start, "type", start_path)
    if start_type != "header":
        raise _error(start_path, "field 'type' must be 'header'.")

    end = _required_mapping(value, "end", path)
    end_path = f"{path}.end"
    end_type = _required_string(end, "type", end_path)
    if end_type == "eof":
        _exact_keys(end, {"type"}, end_path)
        end_match = None
    elif end_type == "before_match":
        _exact_keys(end, {"type", "match"}, end_path)
        end_match = _parse_field_match(
            _required_mapping(end, "match", end_path),
            f"{end_path}.match",
        )
    else:
        raise _error(end_path, "field 'type' must be 'before_match' or 'eof'.")

    raw_rules = _required_list(value, "row_rules", path)
    row_rules = tuple(
        _parse_row_rule(rule, f"{path}.row_rules[{index}]")
        for index, rule in enumerate(raw_rules)
    )
    _require_unique([rule.name for rule in row_rules], f"{path}.row_rules names")
    orders = [rule.order for rule in row_rules]
    _require_unique(orders, f"{path}.row_rules order values")
    if orders != sorted(orders):
        raise _error(path, "row_rules must be declared in ascending order.")

    output_name = _required_string(value, "output_name", path)
    if "{source_stem}" not in output_name:
        raise _error(path, "field 'output_name' must contain '{source_stem}'.")
    keep_first = _required_bool(header, "keep_first", header_path)
    if not keep_first:
        raise _error(header_path, "field 'keep_first' must be true.")
    return TableDefinition(
        name=_required_string(value, "name", path),
        output_name=output_name,
        expected_columns=expected_columns,
        header_match=header_match,
        keep_first_header=keep_first,
        repeated_comparison=comparison,
        repeated_action=action,
        start_type=start_type,
        end_type=end_type,
        end_match=end_match,
        row_rules=row_rules,
    )


def _parse_field_match(value: Mapping[str, Any], path: str) -> FieldMatch:
    _exact_keys(
        value,
        {"type", "values", "case_sensitive", "trim_fields"},
        path,
    )
    match_type = _required_string(value, "type", path)
    if match_type not in _HEADER_MATCH_TYPES:
        raise _error(
            path,
            "field 'type' must be 'exact_fields' or 'contains_fields'.",
        )
    values = _string_list(value, "values", path, allow_empty=False)
    return FieldMatch(
        type=match_type,
        values=values,
        case_sensitive=_required_bool(value, "case_sensitive", path),
        trim_fields=_required_bool(value, "trim_fields", path),
    )


def _parse_row_rule(entry: Any, path: str) -> RowRule:
    value = _mapping(entry, path)
    base_keys = {"name", "order", "action"}
    has_standard = "standard" in value
    has_match = "match" in value
    if has_standard == has_match:
        raise _error(path, "must define exactly one of 'standard' or 'match'.")
    _exact_keys(
        value,
        base_keys | ({"standard"} if has_standard else {"match"}),
        path,
    )
    action = _required_string(value, "action", path)
    if action != "drop":
        raise _error(path, "field 'action' must be 'drop'.")
    order = _positive_int(value, "order", path)

    if has_standard:
        standard = _required_string(value, "standard", path)
        if standard not in _ROW_STANDARDS:
            raise _error(
                path,
                "field 'standard' must be 'blank' or 'repeated_header'.",
            )
        match_type = value_text = pattern = None
        case_sensitive = None
        flags: tuple[str, ...] = ()
    else:
        standard = None
        match = _required_mapping(value, "match", path)
        match_path = f"{path}.match"
        match_type = _required_string(match, "type", match_path)
        if match_type == "contains":
            _exact_keys(match, {"type", "value", "case_sensitive"}, match_path)
            value_text = _required_string(match, "value", match_path)
            case_sensitive = _required_bool(match, "case_sensitive", match_path)
            pattern = None
            flags = ()
        elif match_type == "regex":
            _exact_keys(match, {"type", "pattern", "flags"}, match_path)
            pattern = _required_string(match, "pattern", match_path)
            raw_flags = _string_list(match, "flags", match_path, allow_empty=True)
            unknown_flags = sorted(set(raw_flags) - _REGEX_FLAGS)
            if unknown_flags:
                raise _error(
                    match_path,
                    "field 'flags' contains unsupported values: "
                    + ", ".join(unknown_flags),
                )
            _require_unique(raw_flags, f"{match_path}.flags")
            try:
                re.compile(pattern, _regex_flags(raw_flags))
            except re.error as exc:
                raise _error(match_path, f"invalid regex pattern: {exc}") from exc
            flags = raw_flags
            value_text = None
            case_sensitive = None
        else:
            raise _error(
                match_path,
                "field 'type' must be 'contains' or 'regex'.",
            )

    return RowRule(
        name=_required_string(value, "name", path),
        order=order,
        action=action,
        standard=standard,
        match_type=match_type,
        value=value_text,
        pattern=pattern,
        case_sensitive=case_sensitive,
        flags=flags,
    )


def _parse_validation(entry: Any, path: str) -> ValidationDefinition:
    value = _mapping(entry, path)
    validation_type = _required_string(value, "type", path)
    if validation_type not in _VALIDATION_TYPES:
        raise _error(path, f"unsupported validation type '{validation_type}'.")
    expected_keys = {
        "matching_files": {
            "name",
            "type",
            "require_every_matching_file",
            "allow_missing_matching_files",
        },
        "table_headers": {
            "name",
            "type",
            "require_unique_table_headers",
            "require_exactly_one_output_header",
        },
        "table_regions": {
            "name",
            "type",
            "require_non_overlapping_table_regions",
        },
        "output_rows": {
            "name",
            "type",
            "require_consistent_field_count",
            "require_expected_column_order",
            "minimum_data_rows_per_table",
            "reject_unresolved_rows",
            "preserve_leading_zero_values",
        },
        "deterministic_reruns": {"name", "type", "count"},
        "control_total": {
            "name",
            "type",
            "table",
            "column",
            "aggregation",
            "tolerance",
            "expected_values",
        },
    }[validation_type]
    _exact_keys(value, expected_keys, path)

    settings = {key: item for key, item in value.items() if key not in {"name", "type"}}
    if validation_type == "matching_files":
        _require_bool_value(value, "require_every_matching_file", True, path)
        _require_bool_value(value, "allow_missing_matching_files", False, path)
    elif validation_type == "table_headers":
        _require_bool_value(value, "require_unique_table_headers", True, path)
        _require_bool_value(value, "require_exactly_one_output_header", True, path)
    elif validation_type == "table_regions":
        _require_bool_value(
            value,
            "require_non_overlapping_table_regions",
            True,
            path,
        )
    elif validation_type == "output_rows":
        for key in (
            "require_consistent_field_count",
            "require_expected_column_order",
            "reject_unresolved_rows",
            "preserve_leading_zero_values",
        ):
            _require_bool_value(value, key, True, path)
        _positive_int(value, "minimum_data_rows_per_table", path)
    elif validation_type == "deterministic_reruns":
        if _positive_int(value, "count", path) < 2:
            raise _error(path, "field 'count' must be at least 2.")
    else:
        for key in ("table", "column", "tolerance"):
            _required_string(value, key, path)
        if _required_string(value, "aggregation", path) != "sum":
            raise _error(path, "field 'aggregation' must be 'sum'.")
        expected_values = _required_list(value, "expected_values", path)
        if not expected_values:
            raise _error(path, "field 'expected_values' must not be empty.")
        normalized_expected = []
        for index, expected in enumerate(expected_values):
            expected_path = f"{path}.expected_values[{index}]"
            expected_map = _mapping(expected, expected_path)
            _exact_keys(expected_map, {"source_file", "value"}, expected_path)
            normalized_expected.append(
                MappingProxyType(
                    {
                        "source_file": _required_string(
                            expected_map, "source_file", expected_path
                        ),
                        "value": _required_string(expected_map, "value", expected_path),
                    }
                )
            )
        _require_unique(
            [item["source_file"] for item in normalized_expected],
            f"{path}.expected_values source_file values",
        )
        settings["expected_values"] = tuple(normalized_expected)

    return ValidationDefinition(
        name=_required_string(value, "name", path),
        type=validation_type,
        settings=MappingProxyType(settings),
    )


def verify_contract_lineage(ctx: Any, name: str, expected_sha256: str, label: str) -> None:
    """Check that the stored analysis contract ``name`` hashes to ``expected_sha256``.

    Contract lineage is the contract's declared name plus the hash of its
    canonical stored text (backlog 903), resolved through the one runtime path
    -- control.analysis_contract via the runtime's Control -- never a file.

    Raises:
        InputError: If the contract cannot be resolved or its hash differs.
    """
    from rey_lib.ai.errors import AIConfigurationError  # noqa: PLC0415
    from rey_lib.analysis import analysis_contract  # noqa: PLC0415
    from rey_lib.errors.error_utils import ConfigError  # noqa: PLC0415

    try:
        contract = analysis_contract(getattr(ctx, "shared_control", None), name)
    except (ConfigError, AIConfigurationError) as exc:
        raise InputError(f"{label} contract '{name}' cannot be resolved: {exc}") from exc
    if contract.hash != expected_sha256:
        raise InputError(
            f"{label} contract hash does not match stored contract '{name}'."
        )


def _parse_lineage(value: Mapping[str, Any], path: str) -> Mapping[str, str]:
    keys = {
        "analysis_artifact",
        "analysis_artifact_sha256",
        "llm_contract_name",
        "llm_contract_sha256",
        "analyzer_workflow",
        "analysis_name",
        "model_profile",
        "provider",
        "model",
        "request_id",
        "analyzer_run_id",
        "analyzer_result_path",
        "analyzer_result_sha256",
        "candidate_rules_path",
        "candidate_rules_sha256",
        "validation_evidence_path",
        "validation_evidence_sha256",
    }
    _exact_keys(value, keys, path)
    parsed = {key: _required_string(value, key, path) for key in keys}
    for key in keys:
        if key.endswith("_sha256"):
            _validate_sha256(parsed[key], f"{path}.{key}")
    return MappingProxyType(parsed)


def _parse_approval(value: Mapping[str, Any], path: str) -> Approval:
    _exact_keys(
        value,
        {
            "status",
            "validated_file_count",
            "matching_file_count",
            "unresolved_ambiguities",
            "deterministic_output_hashes_match",
            "approved_by",
            "approved_at",
        },
        path,
    )
    status = _required_string(value, "status", path)
    if status != "passed":
        raise _error(path, "field 'status' must be 'passed'.")
    validated = _positive_int(value, "validated_file_count", path)
    matching = _positive_int(value, "matching_file_count", path)
    if validated != matching:
        raise _error(
            path,
            "validated_file_count must equal matching_file_count.",
        )
    ambiguities = _required_list(value, "unresolved_ambiguities", path)
    if ambiguities:
        raise _error(path, "field 'unresolved_ambiguities' must be empty.")
    if not _required_bool(value, "deterministic_output_hashes_match", path):
        raise _error(
            path,
            "field 'deterministic_output_hashes_match' must be true.",
        )
    return Approval(
        status=status,
        validated_file_count=validated,
        matching_file_count=matching,
        approved_by=_required_string(value, "approved_by", path),
        approved_at=_required_string(value, "approved_at", path),
    )


def _parse_canonicalization(value: Mapping[str, Any], path: str) -> dict[str, str]:
    _exact_keys(value, {"algorithm", "rule_sha256"}, path)
    algorithm = _required_string(value, "algorithm", path)
    if algorithm != "canonical-json-sorted-keys-utf8":
        raise _error(
            path,
            "field 'algorithm' must be 'canonical-json-sorted-keys-utf8'.",
        )
    rule_sha256 = _required_string(value, "rule_sha256", path)
    _validate_sha256(rule_sha256, f"{path}.rule_sha256")
    return {
        "canonicalization_algorithm": algorithm,
        "rule_sha256": rule_sha256,
    }


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): getattr(value, key) for key in keys()}
    if hasattr(value, "__dict__"):
        return vars(value)
    raise _error(path, "must be a mapping.")


def _canonical_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_plain(item) for key, item in value.items()}
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): _canonical_plain(getattr(value, key)) for key in keys()}
    if isinstance(value, (list, tuple)):
        return [_canonical_plain(item) for item in value]
    return value


def _required_mapping(
    value: Mapping[str, Any],
    key: str,
    path: str,
) -> Mapping[str, Any]:
    if key not in value:
        raise _error(path, f"is missing required field '{key}'.")
    return _mapping(value[key], f"{path}.{key}")


def _required_list(value: Mapping[str, Any], key: str, path: str) -> list[Any]:
    if key not in value:
        raise _error(path, f"is missing required field '{key}'.")
    result = value[key]
    if not isinstance(result, list):
        raise _error(path, f"field '{key}' must be a list.")
    return result


def _required_string(value: Mapping[str, Any], key: str, path: str) -> str:
    if key not in value:
        raise _error(path, f"is missing required field '{key}'.")
    return _non_empty_string(value[key], f"{path}.{key}")


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(path, "must be a non-empty string.")
    return value.strip()


def _required_bool(value: Mapping[str, Any], key: str, path: str) -> bool:
    if key not in value:
        raise _error(path, f"is missing required field '{key}'.")
    result = value[key]
    if not isinstance(result, bool):
        raise _error(path, f"field '{key}' must be a boolean.")
    return result


def _require_bool_value(
    value: Mapping[str, Any],
    key: str,
    expected: bool,
    path: str,
) -> None:
    actual = _required_bool(value, key, path)
    if actual is not expected:
        rendered = "true" if expected else "false"
        raise _error(path, f"field '{key}' must be {rendered}.")


def _positive_int(value: Mapping[str, Any], key: str, path: str) -> int:
    if key not in value:
        raise _error(path, f"is missing required field '{key}'.")
    result = value[key]
    if isinstance(result, bool) or not isinstance(result, int) or result < 1:
        raise _error(path, f"field '{key}' must be a positive integer.")
    return result


def _string_list(
    value: Mapping[str, Any],
    key: str,
    path: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    raw = _required_list(value, key, path)
    if not raw and not allow_empty:
        raise _error(path, f"field '{key}' must not be empty.")
    return tuple(
        _non_empty_string(item, f"{path}.{key}[{index}]")
        for index, item in enumerate(raw)
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    path: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise _error(path, "is missing required fields: " + ", ".join(missing))
    if unknown:
        raise _error(path, "contains unknown fields: " + ", ".join(unknown))


def _allowed_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    path: str,
) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing:
        raise _error(path, "is missing required fields: " + ", ".join(missing))
    if unknown:
        raise _error(path, "contains unknown fields: " + ", ".join(unknown))


def _require_unique(values: list[Any] | tuple[Any, ...], label: str) -> None:
    seen: set[Any] = set()
    duplicates: list[Any] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(repr(value) for value in duplicates)
        raise RedactorError(f"{label} must be unique; duplicates: {rendered}.")


def _validate_sha256(value: str, path: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise _error(path, "must be a lowercase 64-character SHA-256 hex digest.")


def _regex_flags(flags: tuple[str, ...]) -> int:
    return sum(int(getattr(re, flag)) for flag in flags)


def _error(path: str, message: str) -> RedactorError:
    return RedactorError(f"Invalid {path}: {message}")
