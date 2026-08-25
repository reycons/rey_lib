"""Governed, policy-driven streamed text sanitization."""

from __future__ import annotations

import codecs
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.encryption import sha256_bytes, sha256_file
from rey_lib.files.file_routing import GovernedFileReference
from rey_lib.files.log_run_rollback import (
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
    log_source_file_mutation,
)
from rey_lib.files.governed_file import FileId
from rey_lib.files.primitive_file_io import stage_stream_write

__all__ = [
    "EffectiveSanitizationPolicy",
    "FileSanitizationCollisionPolicy",
    "FileSanitizationContext",
    "FileSanitizationError",
    "FileSanitizationEvidenceError",
    "FileSanitizationResult",
    "compose_sanitization_policy",
    "sanitize_file",
]

_CHUNK_BYTES = 64 * 1024
_OUTPUT_ENCODING = "utf-8"
_CODEPOINT = re.compile(r"U\+[0-9A-F]{4,6}")
_POLICY_FIELDS = frozenset(
    {
        "policy_name",
        "policy_version",
        "remove",
        "preserve",
        "preserve_if_quoted",
        "replace",
        "line_repair",
        "max_logical_line_characters",
    }
)
_TABLES = ("remove", "preserve", "preserve_if_quoted", "replace")
_REGEX_FLAGS = {"ASCII": re.ASCII, "IGNORECASE": re.IGNORECASE}
_QUOTE_CONTRACT = "double_quote_lexical_v1"


class FileSanitizationCollisionPolicy(str, Enum):
    FAIL = "fail"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class _CharacterRule:
    codepoint: str
    name: str
    reason: str
    replacement: str | None = None


@dataclass(frozen=True)
class _LineRepairRule:
    name: str
    pattern: str
    replacement: str
    reason: str
    flags: tuple[str, ...]
    compiled: re.Pattern[str]


@dataclass(frozen=True)
class _PolicyLayer:
    name: str
    version: str
    character_rules: tuple[tuple[str, str, _CharacterRule], ...]
    line_rules: tuple[_LineRepairRule, ...]
    max_line: int | None


@dataclass(frozen=True)
class EffectiveSanitizationPolicy:
    """Immutable composed global-plus-feed sanitization policy."""

    global_policy_name: str
    global_policy_version: str
    feed_policy_name: str
    feed_policy_version: str
    character_rules: tuple[tuple[str, str, _CharacterRule], ...]
    line_rules: tuple[_LineRepairRule, ...]
    max_logical_line_characters: int | None
    digest: str


def compose_sanitization_policy(
    global_policy: Mapping[str, Any],
    feed_policy: Mapping[str, Any],
) -> EffectiveSanitizationPolicy:
    """Validate and immutably compose one global policy and feed overlay."""
    global_layer = _parse_policy_layer(global_policy, "global")
    feed_layer = _parse_policy_layer(feed_policy, "feed")
    characters = {item[0]: item for item in global_layer.character_rules}
    for item in feed_layer.character_rules:
        characters[item[0]] = item
    lines = list(global_layer.line_rules)
    indexes = {rule.name: index for index, rule in enumerate(lines)}
    for rule in feed_layer.line_rules:
        if rule.name in indexes:
            lines[indexes[rule.name]] = rule
        else:
            indexes[rule.name] = len(lines)
            lines.append(rule)
    max_line = (
        feed_layer.max_line
        if feed_layer.max_line is not None
        else global_layer.max_line
    )
    if lines and max_line is None:
        raise ValueError(
            "An effective policy with line_repair requires "
            "max_logical_line_characters."
        )
    ordered_characters = tuple(sorted(characters.values(), key=lambda item: item[0]))
    payload = {
        "global_policy": {
            "name": global_layer.name,
            "version": global_layer.version,
        },
        "feed_policy": {"name": feed_layer.name, "version": feed_layer.version},
        "quote_contract": _QUOTE_CONTRACT,
        "character_rules": [
            {
                "codepoint": codepoint,
                "action": action,
                "name": rule.name,
                "reason": rule.reason,
                **(
                    {"with": rule.replacement}
                    if rule.replacement is not None
                    else {}
                ),
            }
            for codepoint, action, rule in ordered_characters
        ],
        "line_repair": [
            {
                "name": rule.name,
                "pattern": rule.pattern,
                "replacement": rule.replacement,
                "reason": rule.reason,
                "flags": list(rule.flags),
            }
            for rule in lines
        ],
        "max_logical_line_characters": max_line,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return EffectiveSanitizationPolicy(
        global_policy_name=global_layer.name,
        global_policy_version=global_layer.version,
        feed_policy_name=feed_layer.name,
        feed_policy_version=feed_layer.version,
        character_rules=ordered_characters,
        line_rules=tuple(lines),
        max_logical_line_characters=max_line,
        digest=sha256_bytes(encoded),
    )


@dataclass(frozen=True)
class FileSanitizationContext:
    state_ctx: Any
    run_log: Any
    application_name: str
    destination_path: Path
    governed_roots: tuple[Path, ...]
    policy: EffectiveSanitizationPolicy
    collision_policy: FileSanitizationCollisionPolicy = FileSanitizationCollisionPolicy.FAIL
    dry_run: bool = False
    add_source_line_number: bool = False
    file_operation_metadata: Mapping[str, Any] | None = None
    mutation_run_log_fields: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        application = self.application_name.strip() if isinstance(self.application_name, str) else ""
        if not application:
            raise ValueError("application_name must be non-empty.")
        if not self.governed_roots:
            raise ValueError("governed_roots must contain at least one path.")
        if not isinstance(self.policy, EffectiveSanitizationPolicy):
            raise ValueError("policy must be an EffectiveSanitizationPolicy.")
        object.__setattr__(self, "application_name", application)
        object.__setattr__(self, "destination_path", Path(self.destination_path).expanduser().resolve())
        object.__setattr__(self, "governed_roots", tuple(Path(root).expanduser().resolve() for root in self.governed_roots))
        object.__setattr__(self, "collision_policy", FileSanitizationCollisionPolicy(self.collision_policy))
        if not isinstance(self.add_source_line_number, bool):
            raise ValueError("add_source_line_number must be true or false.")
        for field in ("file_operation_metadata", "mutation_run_log_fields"):
            value = getattr(self, field)
            object.__setattr__(self, field, MappingProxyType(deepcopy(dict(value))) if value is not None else None)


@dataclass(frozen=True)
class FileSanitizationResult:
    file_id: FileId
    source_path: Path
    source_sha256: str | None
    source_size: int | None
    resolved_source_encoding: str
    source_encoding_resolution_method: str
    source_bom: str
    source_bom_present: bool
    destination_path: Path
    destination_sha256: str | None
    destination_size: int | None
    destination_encoding: str
    output_encoding_changed: bool
    global_policy_name: str
    global_policy_version: str
    feed_policy_name: str
    feed_policy_version: str
    effective_policy_digest: str
    remove_counts_by_rule: Mapping[str, int]
    preserve_counts_by_rule: Mapping[str, int]
    preserve_if_quoted_counts_by_rule: Mapping[str, int]
    replacement_counts_by_rule: Mapping[str, int]
    line_repair_counts_by_rule: Mapping[str, int]
    normalized_true_line_ending_count: int
    output_bytes_differ: bool | None
    destination_replaced: bool
    filesystem_applied: bool
    complete_evidence_acknowledged: bool
    mutation_run_log_id: int | None
    file_manifest_record_id: int | None
    evidence_phase: SourceFileMutationEvidenceFailurePhase | None
    status: str
    failure_reason: str | None


class FileSanitizationError(Exception):
    def __init__(self, message: str, result: FileSanitizationResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class FileSanitizationEvidenceError(FileSanitizationError):
    pass


@dataclass
class _TransformState:
    in_quotes: bool = False
    pending_quote: bool = False
    pending_cr: bool = False
    normalized_newlines: int = 0
    remove: dict[str, int] | None = None
    preserve: dict[str, int] | None = None
    quoted: dict[str, int] | None = None
    replace: dict[str, int] | None = None
    line_repair: dict[str, int] | None = None
    line_buffer: list[str] | None = None
    line_length: int = 0


@dataclass
class _SourceLineState:
    """Streaming CSV state for prepending original physical line numbers."""

    physical_line: int = 1
    at_record_start: bool = True
    in_quotes: bool = False
    pending_quote: bool = False
    previous_cr: bool = False


@dataclass(frozen=True)
class _EncodingPlan:
    decoder_encoding: str
    resolved_encoding: str
    resolution_method: str
    source_bom: str
    bom_size: int


def sanitize_file(ctx: FileSanitizationContext, file_reference: GovernedFileReference) -> FileSanitizationResult:
    if not isinstance(ctx, FileSanitizationContext):
        raise TypeError("sanitize_file requires FileSanitizationContext.")
    if not isinstance(file_reference, GovernedFileReference):
        raise TypeError("sanitize_file requires GovernedFileReference.")
    source = file_reference.current_path
    destination = ctx.destination_path
    _validate_paths(ctx, source, destination)
    if not source.is_file():
        raise FileNotFoundError(f"Sanitization source file not found: {source}")
    replaced_destination = destination.is_file()
    overwrite = ctx.collision_policy is FileSanitizationCollisionPolicy.OVERWRITE
    if destination.exists() and not destination.is_file():
        raise FileSanitizationError(f"Sanitization destination is not a regular file: {destination}")
    if replaced_destination and not overwrite:
        raise FileExistsError(f"Sanitization destination already exists: {destination}")
    plan = _encoding_plan(source)
    result = _empty_result(ctx, file_reference, source, destination, plan, replaced_destination)
    if ctx.dry_run:
        return replace(result, status="planned")
    try:
        source_hash, source_size, state, destination_hash = _transform_and_publish(ctx, source, destination, plan, overwrite)
    except FileSanitizationError as exc:
        if exc.result is not None:
            raise
        raise FileSanitizationError(str(exc), replace(result, status="failed")) from exc
    except (LookupError, OSError, UnicodeError, ValueError) as exc:
        raise FileSanitizationError(str(exc), replace(result, status="failed")) from exc
    result = replace(
        result,
        source_sha256=source_hash,
        source_size=source_size,
        destination_sha256=destination_hash,
        destination_size=destination.stat().st_size,
        remove_counts_by_rule=_frozen_counts(state.remove),
        preserve_counts_by_rule=_frozen_counts(state.preserve),
        preserve_if_quoted_counts_by_rule=_frozen_counts(state.quoted),
        replacement_counts_by_rule=_frozen_counts(state.replace),
        line_repair_counts_by_rule=_frozen_counts(state.line_repair),
        normalized_true_line_ending_count=state.normalized_newlines,
        output_bytes_differ=source_hash != destination_hash,
        filesystem_applied=True,
        status="published",
    )
    run_fields = {
        **dict(ctx.mutation_run_log_fields or {}),
        "source_sha256": result.source_sha256,
        "source_size": result.source_size,
        "source_encoding": result.resolved_source_encoding,
        "source_encoding_resolution_method": (
            result.source_encoding_resolution_method
        ),
        "source_bom": result.source_bom,
        "source_bom_present": result.source_bom_present,
        "destination_sha256": result.destination_sha256,
        "destination_size": result.destination_size,
        "destination_encoding": result.destination_encoding,
        "output_encoding_changed": result.output_encoding_changed,
        "global_policy_name": result.global_policy_name,
        "global_policy_version": result.global_policy_version,
        "feed_policy_name": result.feed_policy_name,
        "feed_policy_version": result.feed_policy_version,
        "effective_policy_digest": result.effective_policy_digest,
        "remove_counts_by_rule": dict(result.remove_counts_by_rule),
        "preserve_counts_by_rule": dict(result.preserve_counts_by_rule),
        "preserve_if_quoted_counts_by_rule": dict(result.preserve_if_quoted_counts_by_rule),
        "replacement_counts_by_rule": dict(result.replacement_counts_by_rule),
        "line_repair_counts_by_rule": dict(result.line_repair_counts_by_rule),
        "normalized_true_line_ending_count": result.normalized_true_line_ending_count,
        "output_bytes_differ": result.output_bytes_differ,
    }
    try:
        mutation_evidence = log_source_file_mutation(
            ctx.state_ctx,
            action="replace" if replaced_destination else "create",
            status="success",
            source_path=source,
            destination_path=destination,
            application_name=ctx.application_name,
            file_id=file_reference.file_id,
            classification=file_reference.classification,
            reason="file_sanitization",
            message="Sanitized one governed received text file under configured policy.",
            run_log_fields=run_fields,
        )
    except SourceFileMutationEvidenceError as exc:
        failure = replace(result, mutation_run_log_id=exc.run_log_id, evidence_phase=exc.phase, status="evidence_failed", failure_reason=str(exc))
        raise FileSanitizationEvidenceError(str(exc), failure) from exc
    return replace(
        result,
        complete_evidence_acknowledged=True,
        mutation_run_log_id=getattr(
            mutation_evidence,
            "run_log_id",
            None,
        ),
        file_manifest_record_id=int(mutation_evidence),
        status="success",
    )


def _transform_and_publish(ctx: FileSanitizationContext, source: Path, destination: Path, plan: _EncodingPlan, overwrite: bool) -> tuple[str, int, _TransformState, str]:
    source_hash = sha256_file(source)
    source_size = source.stat().st_size
    state = _new_state(ctx.policy)
    rules = {codepoint: (action, rule) for codepoint, action, rule in ctx.policy.character_rules}
    decoder = codecs.getincrementaldecoder(plan.decoder_encoding)(errors="strict")
    encoder = codecs.getincrementalencoder(_OUTPUT_ENCODING)(errors="strict")
    source_lines = _SourceLineState() if ctx.add_source_line_number else None
    with source.open("rb") as reader, stage_stream_write(destination, tier="flushed") as staged:
        prefix = reader.read(plan.bom_size)
        if len(prefix) != plan.bom_size:
            raise UnicodeError("Source ended inside its configured byte-order mark.")
        while raw := reader.read(_CHUNK_BYTES):
            rendered = _render(decoder.decode(raw, final=False), state, rules, ctx.policy, final=False)
            if source_lines is not None:
                rendered = _add_source_line_numbers(rendered, source_lines)
            staged.write(encoder.encode(rendered, final=False))
        rendered = _render(decoder.decode(b"", final=True), state, rules, ctx.policy, final=True)
        if source_lines is not None:
            rendered = _add_source_line_numbers(rendered, source_lines)
        staged.write(encoder.encode(rendered, final=True))
        if sha256_file(source) != source_hash or source.stat().st_size != source_size:
            raise FileSanitizationError(f"Sanitization source changed while it was being read: {source}")
        staged.install(overwrite=overwrite)
    return source_hash, source_size, state, sha256_file(destination)


def _add_source_line_numbers(text: str, state: _SourceLineState) -> str:
    """Prepend ``source_line_number`` while preserving streaming CSV quoting."""
    output: list[str] = []
    for character in text:
        if state.pending_quote:
            state.pending_quote = False
            if character == '"':
                if state.at_record_start:
                    _start_numbered_record(output, state)
                output.append(character)
                state.previous_cr = False
                continue
            state.in_quotes = False

        if state.at_record_start:
            _start_numbered_record(output, state)

        if character == '"':
            if state.in_quotes:
                state.pending_quote = True
            else:
                state.in_quotes = True
            output.append(character)
            state.previous_cr = False
            continue

        output.append(character)
        if character == "\r":
            state.physical_line += 1
            state.previous_cr = True
            if not state.in_quotes:
                state.at_record_start = True
            continue
        if character == "\n":
            if not state.previous_cr:
                state.physical_line += 1
            state.previous_cr = False
            if not state.in_quotes:
                state.at_record_start = True
            continue
        state.previous_cr = False
    return "".join(output)


def _start_numbered_record(output: list[str], state: _SourceLineState) -> None:
    prefix = (
        "source_line_number"
        if state.physical_line == 1
        else str(state.physical_line)
    )
    output.extend((prefix, ","))
    state.at_record_start = False


def _render(text: str, state: _TransformState, rules: Mapping[str, tuple[str, _CharacterRule]], policy: EffectiveSanitizationPolicy, *, final: bool) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if state.pending_quote:
            state.pending_quote = False
            if character == '"':
                _process_character(character, output, state, rules, policy)
                index += 1
                continue
            state.in_quotes = False
        if state.pending_cr:
            state.pending_cr = False
            if character == "\n" and not state.in_quotes and _unruled_newline("\r", rules) and _unruled_newline("\n", rules):
                _external_newline(output, state, policy)
                index += 1
                continue
            _process_character("\r", output, state, rules, policy)
        if character == '"':
            if state.in_quotes:
                state.pending_quote = True
            else:
                state.in_quotes = True
            _process_character(character, output, state, rules, policy)
            index += 1
            continue
        if character == "\r" and not state.in_quotes:
            state.pending_cr = True
            index += 1
            continue
        _process_character(character, output, state, rules, policy)
        index += 1
    if final:
        if state.pending_quote:
            state.pending_quote = False
            state.in_quotes = False
        if state.pending_cr:
            state.pending_cr = False
            _process_character("\r", output, state, rules, policy)
        if state.line_buffer is not None and state.line_buffer:
            output.append(_repair_line(state, policy))
    return "".join(output)


def _process_character(character: str, output: list[str], state: _TransformState, rules: Mapping[str, tuple[str, _CharacterRule]], policy: EffectiveSanitizationPolicy) -> None:
    key = f"U+{ord(character):04X}"
    configured = rules.get(key)
    if state.in_quotes and configured is not None and configured[0] == "preserve_if_quoted":
        _increment(state.quoted, key)
        _emit(output, state, character, policy)
        return
    if configured is not None and configured[0] == "preserve":
        _increment(state.preserve, key)
        _emit(output, state, character, policy)
        return
    if configured is not None and configured[0] == "remove":
        _increment(state.remove, key)
        return
    if configured is not None and configured[0] == "replace":
        _increment(state.replace, key)
        _emit(output, state, configured[1].replacement or "", policy)
        return
    if character in {"\r", "\n"} and not state.in_quotes:
        _external_newline(output, state, policy)
        return
    _emit(output, state, character, policy)


def _external_newline(output: list[str], state: _TransformState, policy: EffectiveSanitizationPolicy) -> None:
    state.normalized_newlines += 1
    if state.line_buffer is not None:
        output.append(_repair_line(state, policy))
    output.append("\n")


def _emit(output: list[str], state: _TransformState, value: str, policy: EffectiveSanitizationPolicy) -> None:
    if state.line_buffer is None:
        output.append(value)
        return
    state.line_buffer.append(value)
    state.line_length += len(value)
    maximum = policy.max_logical_line_characters
    if maximum is not None and state.line_length > maximum:
        raise FileSanitizationError(f"Logical line exceeds configured maximum of {maximum} characters.")


def _repair_line(state: _TransformState, policy: EffectiveSanitizationPolicy) -> str:
    line = "".join(state.line_buffer or [])
    if state.line_buffer is not None:
        state.line_buffer.clear()
        state.line_length = 0
    for rule in policy.line_rules:
        line, count = rule.compiled.subn(rule.replacement, line)
        if count:
            state.line_repair[rule.name] += count  # type: ignore[index]
    return line


def _new_state(policy: EffectiveSanitizationPolicy) -> _TransformState:
    counts = {action: {} for action in _TABLES}
    for codepoint, action, _rule in policy.character_rules:
        counts[action][codepoint] = 0
    return _TransformState(
        remove=counts["remove"],
        preserve=counts["preserve"],
        quoted=counts["preserve_if_quoted"],
        replace=counts["replace"],
        line_repair={rule.name: 0 for rule in policy.line_rules},
        line_buffer=[] if policy.line_rules else None,
    )


def _unruled_newline(character: str, rules: Mapping[str, tuple[str, _CharacterRule]]) -> bool:
    configured = rules.get(f"U+{ord(character):04X}")
    return configured is None or configured[0] == "preserve_if_quoted"


def _increment(counts: dict[str, int] | None, key: str) -> None:
    if counts is not None:
        counts[key] = counts.get(key, 0) + 1


def _frozen_counts(counts: Mapping[str, int] | None) -> Mapping[str, int]:
    return MappingProxyType(dict(counts or {}))


def _parse_policy_layer(raw: Mapping[str, Any], label: str) -> _PolicyLayer:
    if not isinstance(raw, Mapping):
        raise ValueError(f"The {label} sanitization policy must be a mapping.")
    unknown = set(raw) - _POLICY_FIELDS
    if unknown:
        raise ValueError(f"The {label} sanitization policy has unknown fields: {sorted(unknown)}")
    name = _required_text(raw, "policy_name", label)
    version = _required_text(raw, "policy_version", label)
    seen: set[str] = set()
    characters: list[tuple[str, str, _CharacterRule]] = []
    for action in _TABLES:
        table = raw.get(action)
        if not isinstance(table, Mapping):
            raise ValueError(f"The {label} policy {action!r} table must be a mapping.")
        for codepoint, entry in table.items():
            key = _validated_codepoint(codepoint)
            if key in seen:
                raise ValueError(f"Code point {key} appears in multiple {label} policy tables.")
            seen.add(key)
            if not isinstance(entry, Mapping):
                raise ValueError(f"Policy rule {key} must be a mapping.")
            expected = {"name", "reason", *( ["with"] if action == "replace" else [])}
            if set(entry) != expected:
                raise ValueError(f"Policy rule {key} requires exactly {sorted(expected)}.")
            replacement = entry.get("with") if action == "replace" else None
            if action == "replace" and not isinstance(replacement, str):
                raise ValueError(
                    f"Policy replacement rule {key} requires 'with' to be a string."
                )
            characters.append((key, action, _CharacterRule(key, _required_text(entry, "name", key), _required_text(entry, "reason", key), replacement)))
    line_table = raw.get("line_repair")
    if not isinstance(line_table, Mapping):
        raise ValueError(f"The {label} policy 'line_repair' table must be a mapping.")
    line_rules = tuple(_parse_line_rule(str(rule_name), entry) for rule_name, entry in line_table.items())
    maximum = raw.get("max_logical_line_characters")
    if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0):
        raise ValueError("max_logical_line_characters must be a positive integer.")
    return _PolicyLayer(name, version, tuple(characters), line_rules, maximum)


def _parse_line_rule(name: str, entry: Any) -> _LineRepairRule:
    if not name.strip() or not isinstance(entry, Mapping):
        raise ValueError("Every line_repair rule requires a nonblank name and mapping.")
    if set(entry) - {"pattern", "replacement", "reason", "flags"} or not {"pattern", "replacement", "reason"} <= set(entry):
        raise ValueError(f"Line-repair rule {name!r} is malformed.")
    flags_raw = entry.get("flags", [])
    if not isinstance(flags_raw, (list, tuple)) or any(flag not in _REGEX_FLAGS for flag in flags_raw):
        raise ValueError(f"Line-repair rule {name!r} has unsupported flags.")
    pattern_value = entry.get("pattern")
    if not isinstance(pattern_value, str) or not pattern_value.strip():
        raise ValueError(f"Line-repair rule {name!r} requires a nonblank pattern.")
    pattern = pattern_value
    replacement = entry["replacement"]
    if not isinstance(replacement, str):
        raise ValueError(f"Line-repair rule {name!r} replacement must be a string.")
    flags_value = sum((_REGEX_FLAGS[flag] for flag in flags_raw), re.NOFLAG)
    compiled = re.compile(pattern, flags_value)
    parsed = re._parser.parse(pattern, flags_value)  # type: ignore[attr-defined]
    if parsed.getwidth()[0] == 0:
        raise ValueError(f"Line-repair rule {name!r} may match an empty string.")
    compiled.sub(replacement, "")
    return _LineRepairRule(name, pattern, replacement, _required_text(entry, "reason", name), tuple(flags_raw), compiled)


def _required_text(mapping: Mapping[str, Any], field: str, label: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires non-empty {field!r}.")
    return value.strip()


def _validated_codepoint(value: Any) -> str:
    if not isinstance(value, str) or _CODEPOINT.fullmatch(value) is None:
        raise ValueError(f"Invalid Unicode code-point key: {value!r}.")
    number = int(value[2:], 16)
    canonical = f"U+{number:04X}"
    if value != canonical or number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
        raise ValueError(f"Invalid Unicode scalar key: {value!r}.")
    return value


def _encoding_plan(source: Path) -> _EncodingPlan:
    with source.open("rb") as handle:
        prefix = handle.read(4)
    detected = _detected_bom(prefix)
    if detected is not None:
        bom_name, bom_bytes, explicit_codec, _ = detected
        return _EncodingPlan(
            explicit_codec,
            explicit_codec,
            "bom",
            bom_name,
            len(bom_bytes),
        )
    unsupported_bom = _unsupported_bom(prefix)
    if unsupported_bom is not None:
        raise FileSanitizationError(
            f"Source begins with unsupported BOM signature {unsupported_bom}."
        )
    for encoding in ("utf-8", "cp1252"):
        if _strictly_decodes(source, encoding):
            method = (
                "utf8_validation"
                if encoding == "utf-8"
                else "windows_1252_fallback"
            )
            return _EncodingPlan(encoding, encoding, method, "absent", 0)
    raise FileSanitizationError(
        "Source encoding could not be resolved as BOM-declared Unicode, UTF-8, "
        "or Windows-1252 without decoding loss."
    )


def _strictly_decodes(source: Path, encoding: str) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        with source.open("rb") as handle:
            while raw := handle.read(_CHUNK_BYTES):
                decoder.decode(raw, final=False)
        decoder.decode(b"", final=True)
    except UnicodeError:
        return False
    return True


def _detected_bom(prefix: bytes) -> tuple[str, bytes, str, str] | None:
    for item in (("utf-32-le", codecs.BOM_UTF32_LE, "utf-32-le", "utf-32"), ("utf-32-be", codecs.BOM_UTF32_BE, "utf-32-be", "utf-32"), ("utf-8", codecs.BOM_UTF8, "utf-8", "utf-8"), ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16-le", "utf-16"), ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16-be", "utf-16")):
        if prefix.startswith(item[1]):
            return item
    return None


def _unsupported_bom(prefix: bytes) -> str | None:
    signatures = (
        ("UTF-7", (b"+/v8", b"+/v9", b"+/v+", b"+/v/")),
        ("UTF-1", (b"\xf7\x64\x4c",)),
        ("UTF-EBCDIC", (b"\xdd\x73\x66\x73",)),
        ("SCSU", (b"\x0e\xfe\xff",)),
        ("BOCU-1", (b"\xfb\xee\x28",)),
        ("GB18030", (b"\x84\x31\x95\x33",)),
    )
    for name, values in signatures:
        if any(prefix.startswith(value) for value in values):
            return name
    return None


def _validate_paths(ctx: FileSanitizationContext, source: Path, destination: Path) -> None:
    if source == destination:
        raise FileSanitizationError("File sanitization source and destination must be different paths.")
    for label, path in (("source", source), ("destination", destination)):
        if not any(path == root or root in path.parents for root in ctx.governed_roots):
            raise FileSanitizationError(f"Sanitization {label} is outside configured governed roots: {path}")


def _empty_result(ctx: FileSanitizationContext, file: GovernedFileReference, source: Path, destination: Path, plan: _EncodingPlan, replaced_destination: bool) -> FileSanitizationResult:
    policy = ctx.policy
    state = _new_state(policy)
    return FileSanitizationResult(
        file_id=file.file_id,
        source_path=source,
        source_sha256=None,
        source_size=None,
        resolved_source_encoding=plan.resolved_encoding,
        source_encoding_resolution_method=plan.resolution_method,
        source_bom=plan.source_bom,
        source_bom_present=plan.bom_size > 0,
        destination_path=destination,
        destination_sha256=None,
        destination_size=None,
        destination_encoding=_OUTPUT_ENCODING,
        output_encoding_changed=(
            plan.resolved_encoding != _OUTPUT_ENCODING or plan.bom_size > 0
        ),
        global_policy_name=policy.global_policy_name,
        global_policy_version=policy.global_policy_version,
        feed_policy_name=policy.feed_policy_name,
        feed_policy_version=policy.feed_policy_version,
        effective_policy_digest=policy.digest,
        remove_counts_by_rule=_frozen_counts(state.remove),
        preserve_counts_by_rule=_frozen_counts(state.preserve),
        preserve_if_quoted_counts_by_rule=_frozen_counts(state.quoted),
        replacement_counts_by_rule=_frozen_counts(state.replace),
        line_repair_counts_by_rule=_frozen_counts(state.line_repair),
        normalized_true_line_ending_count=0,
        output_bytes_differ=None,
        destination_replaced=replaced_destination,
        filesystem_applied=False,
        complete_evidence_acknowledged=False,
        mutation_run_log_id=None,
        file_manifest_record_id=None,
        evidence_phase=None,
        status="pending",
        failure_reason=None,
    )
