"""Parameterized file discovery and processing manifest.

Executes one installation-owned ``file_discoveries`` entry: it resolves the
configured parameterized source path, enumerates matching files, captures the
declared path and filename variables, validates them, and writes one
deterministic discovery manifest.

Ownership boundaries:

  * installation YAML owns each entry and its ordered ``variables`` list;
  * ``rey_lib.files.capture_path_variables`` owns path-segment capture;
  * ``rey_lib.files.visible_files`` owns enumeration and hidden-path rules;
  * ``rey_lib.files.run_artifact_path`` owns run-artifact naming and collision
    policy;
  * this module owns configuration validation, declarative filename-pattern
    compilation, discovery records, manifest construction, and evidence.

Discovery never reads, moves, renames, copies, converts, profiles, classifies,
or otherwise modifies a discovered file. The manifest carries metadata only.

The declarative ``filename.pattern`` is compiled into an internal matcher. That
matcher is an implementation detail and is never part of the configuration
contract: no regex is accepted from, or exposed to, installation YAML.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from rey_lib.files.file_utils import (
    capture_path_variables,
    run_artifact_path,
    visible_files,
)
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import (
    AppLogger,
    log_artifact_reference,
    log_row_count,
    log_validation_result,
)

from rey_lib.files.error_utils import RedactorError

_logger = AppLogger(__name__)

__all__ = [
    "DiscoveryVariable",
    "FileDiscoveryConfig",
    "resolve_file_discoveries",
    "run_file_discovery",
]

_CONFIG_SECTION = "file_discoveries"
_MANIFEST_ARTIFACT_SUFFIX = "file_discovery_manifest"
_MANIFEST_SCHEMA_VERSION = 1
_ALLOWED_SOURCES = ("path", "filename", "classification")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

# Closed, deterministic format validators. Adding a format is a deliberate code
# change here; configuration cannot introduce one.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_FORMAT_MATCHERS = {
    "mmmyy": f"(?:{'|'.join(_MONTHS)})[0-9]{{2}}",
}

_REJECT_FILENAME_MISMATCH = "filename_pattern_mismatch"
_REJECT_VALUE_NOT_ALLOWED = "value_not_allowed"
_REJECT_FORMAT_INVALID = "format_invalid"
_REJECT_MISSING_REQUIRED = "missing_required_variable"
_REJECT_EXCLUDED = "excluded"


@dataclass(frozen=True)
class DiscoveryVariable:
    """One declared discovery variable, in configured order."""

    name: str
    source: str
    required: bool
    values: tuple[str, ...] | None
    format: str | None

    @property
    def anchored(self) -> bool:
        """Return whether this variable proves its own deterministic boundary."""
        return bool(self.values) or bool(self.format)


@dataclass(frozen=True)
class FileDiscoveryConfig:
    """Validated configuration for one named file discovery."""

    name: str
    enabled: bool
    source_path_pattern: str
    file_pattern: str
    outbox: Path
    filename_pattern: str
    variables: tuple[DiscoveryVariable, ...]


def resolve_file_discoveries(ctx: Any) -> tuple[Any, ...]:
    """Return the configured discovery entries in declared order.

    Only the section shape is validated here — presence, list type, and unique
    names — because those make the whole configuration unusable. Each entry is
    validated independently when it is executed, so one malformed rule cannot
    prevent the others from running.
    """

    entries = getattr(ctx, _CONFIG_SECTION, None)
    if entries is None:
        raise RedactorError(
            "Resolved context is missing required 'file_discoveries' configuration."
        )
    if not isinstance(entries, list):
        raise RedactorError("Resolved ctx.file_discoveries must be a list.")
    if not entries:
        raise RedactorError("Resolved ctx.file_discoveries declares no discoveries.")

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        name = str(_get(entry, "name", "")).strip()
        if not name:
            raise RedactorError(
                f"ctx.file_discoveries[{index}] requires a non-empty 'name'."
            )
        if name in seen:
            raise RedactorError(
                f"ctx.file_discoveries declares duplicate discovery '{name}'."
            )
        seen.add(name)
    return tuple(entries)


def _discovery_config(entry: Any, name: str) -> FileDiscoveryConfig:
    """Validate and project one discovery entry into typed configuration."""

    source = _required_mapping(entry, "source", name)
    filename = _required_mapping(entry, "filename", name)
    variables = _variables(entry, name)

    config = FileDiscoveryConfig(
        name=name,
        enabled=_required_bool(entry, "enabled", name),
        source_path_pattern=_required_string(source, "path", name),
        file_pattern=_required_string(source, "file_pattern", name),
        outbox=Path(_required_string(entry, "outbox", name)).expanduser(),
        filename_pattern=_required_string(filename, "pattern", name),
        variables=variables,
    )
    _validate_placeholders(config)
    _compile_filename_matcher(config)
    return config


def run_file_discovery(ctx: Any, run_log) -> int:
    """Execute every configured discovery rule, one manifest per rule.

    Each enabled rule runs independently: a rule that fails is recorded and the
    remaining rules still execute, and the command returns nonzero when any rule
    failed. A disabled rule is skipped and recorded as skipped.

    Within a rule, a candidate file that does not satisfy the configured
    contract is a discovery result, not a failure: it becomes a rejected record
    and the rule still succeeds. Only an invalid rule configuration, an
    inaccessible source path, an unusable outbox, a failed manifest write, or a
    failed artifact registration fails a rule.
    """

    entries = resolve_file_discoveries(ctx)
    executed = 0
    skipped = 0
    failed = 0

    for entry in entries:
        name = str(_get(entry, "name", "")).strip()
        try:
            enabled = _required_bool(entry, "enabled", name)
        except RedactorError as exc:
            failed += 1
            _record_rule_failure(ctx, run_log, name, exc)
            continue

        if not enabled:
            skipped += 1
            log_validation_result(run_log,
                validation_name="file_discovery_rule",
                status="skipped",
                message=f"File discovery '{name}' is disabled.",
                discovery_name=name,
            )
            continue

        try:
            _run_discovery_rule(ctx, run_log, entry, name)
        except RedactorError as exc:
            failed += 1
            _record_rule_failure(ctx, run_log, name, exc)
            continue
        executed += 1

    log_row_count(run_log,
        count_name="file_discovery_rules_executed",
        count=executed,
        subject=_CONFIG_SECTION,
    )
    log_row_count(run_log,
        count_name="file_discovery_rules_skipped",
        count=skipped,
        subject=_CONFIG_SECTION,
    )
    log_row_count(run_log,
        count_name="file_discovery_rules_failed",
        count=failed,
        subject=_CONFIG_SECTION,
    )
    return 1 if failed else 0


def _run_discovery_rule(ctx: Any, run_log, entry: Any, name: str) -> Path:
    """Execute one discovery rule and return its written manifest path."""

    config = _discovery_config(entry, name)
    outbox = _prepare_outbox(config)
    matcher = _compile_filename_matcher(config)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for directory, path_values in _matching_directories(config):
        for source_file in visible_files(
            directory, config.file_pattern, recursive=False
        ):
            record = _classify_file(config, run_log, matcher, source_file, path_values)
            if record["status"] == "accepted":
                accepted.append(record)
            else:
                rejected.append(record)

    manifest_path = _write_manifest(ctx, run_log, config, outbox, accepted, rejected)

    log_row_count(run_log,
        count_name="discovered_files_accepted",
        count=len(accepted),
        subject=config.name,
    )
    log_row_count(run_log,
        count_name="discovered_files_rejected",
        count=len(rejected),
        subject=config.name,
    )
    log_validation_result(run_log,
        validation_name="file_discovery_rule",
        status="passed",
        message=(
            f"Discovered {len(accepted)} accepted and {len(rejected)} rejected "
            f"candidate file(s) for '{config.name}'."
        ),
        discovery_name=config.name,
        manifest=str(manifest_path),
    )
    return manifest_path


def _record_rule_failure(ctx: Any, run_log, name: str, exc: Exception) -> None:
    """Record one failed discovery rule without stopping the remaining rules."""
    log_validation_result(run_log,
        validation_name="file_discovery_rule",
        status="failed",
        message=str(exc),
        discovery_name=name or "<unnamed>",
    )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def _variables(
    entry: Any,
    config_name: str,
    *,
    config_kind: str = "File discovery",
) -> tuple[DiscoveryVariable, ...]:
    """Return the ordered declared variables, validated independently."""
    raw = _get(entry, "variables", None)
    if not isinstance(raw, list):
        raise RedactorError(
            f"{config_kind} '{config_name}' requires an ordered 'variables' list."
        )
    if not raw:
        raise RedactorError(f"{config_kind} '{config_name}' declares no variables.")

    seen: set[str] = set()
    variables: list[DiscoveryVariable] = []
    for index, item in enumerate(raw):
        label = f"{config_name}.variables[{index}]"
        name = _required_string(item, "name", label)
        if name in seen:
            raise RedactorError(f"{label} redeclares variable '{name}'.")
        seen.add(name)

        source = _required_string(item, "source", label)
        if source not in _ALLOWED_SOURCES:
            raise RedactorError(
                f"{label} source must be one of {', '.join(_ALLOWED_SOURCES)}."
            )

        required = _get(item, "required", True)
        if not isinstance(required, bool):
            raise RedactorError(f"{label} 'required' must be a boolean.")

        values = _get(item, "values", None)
        if values is not None:
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise RedactorError(
                    f"{label} 'values' must be a list of strings or null."
                )
            values = tuple(values)

        fmt = _get(item, "format", None)
        if fmt is not None:
            if not isinstance(fmt, str) or fmt not in _FORMAT_MATCHERS:
                raise RedactorError(
                    f"{label} 'format' must be one of "
                    f"{', '.join(sorted(_FORMAT_MATCHERS))}."
                )

        variables.append(
            DiscoveryVariable(
                name=name,
                source=source,
                required=required,
                values=values,
                format=fmt,
            )
        )
    return tuple(variables)


def _validate_placeholders(config: FileDiscoveryConfig) -> None:
    """Require every placeholder and declaration to correspond exactly once."""
    declared = {variable.name: variable for variable in config.variables}
    path_names = _placeholder_names(
        config.source_path_pattern, config.name, "source.path"
    )
    filename_names = _placeholder_names(
        config.filename_pattern, config.name, "filename.pattern"
    )

    for used, expected_source, where in (
        (path_names, "path", "source.path"),
        (filename_names, "filename", "filename.pattern"),
    ):
        for name in used:
            variable = declared.get(name)
            if variable is None:
                raise RedactorError(
                    f"File discovery '{config.name}' {where} uses undeclared "
                    f"placeholder '{name}'."
                )
            if variable.source != expected_source:
                raise RedactorError(
                    f"File discovery '{config.name}' variable '{name}' declares "
                    f"source '{variable.source}' but appears in {where}."
                )

    used_names = set(path_names) | set(filename_names)
    unused = [name for name in declared if name not in used_names]
    if unused:
        raise RedactorError(
            f"File discovery '{config.name}' declares unused variable(s): "
            f"{', '.join(sorted(unused))}."
        )


def _placeholder_names(pattern: str, config_name: str, where: str) -> list[str]:
    """Return placeholder names in order, rejecting malformed or repeated ones."""
    names: list[str] = []
    for match in _PLACEHOLDER.finditer(pattern):
        name = match.group(1)
        if not name:
            raise RedactorError(
                f"File discovery '{config_name}' {where} contains an empty placeholder."
            )
        if name in names:
            raise RedactorError(
                f"File discovery '{config_name}' {where} repeats placeholder '{name}'."
            )
        names.append(name)
    if pattern.count("{") != len(names) or pattern.count("}") != len(names):
        raise RedactorError(
            f"File discovery '{config_name}' {where} has an unbalanced placeholder brace."
        )
    return names


# ---------------------------------------------------------------------------
# Declarative filename pattern compilation
# ---------------------------------------------------------------------------


def _filename_elements(config: FileDiscoveryConfig) -> list[tuple[str, str]]:
    """Split the declarative filename pattern into ordered literal/variable parts."""
    elements: list[tuple[str, str]] = []
    position = 0
    for match in _PLACEHOLDER.finditer(config.filename_pattern):
        literal = config.filename_pattern[position : match.start()]
        if literal:
            elements.append(("literal", literal))
        elements.append(("variable", match.group(1)))
        position = match.end()
    trailing = config.filename_pattern[position:]
    if trailing:
        elements.append(("literal", trailing))
    return elements


def _compile_filename_matcher(config: FileDiscoveryConfig) -> re.Pattern[str]:
    """Compile the declarative filename pattern into a deterministic matcher.

    Ambiguity is rejected here, at configuration time, so a pattern can never be
    interpreted inconsistently from one file to the next. An unanchored variable
    must be followed by a literal, by a variable that proves its own boundary
    through configured ``values`` or a fixed ``format``, or by nothing at all.
    """
    declared = {variable.name: variable for variable in config.variables}
    elements = _filename_elements(config)
    parts: list[str] = []

    for index, (kind, value) in enumerate(elements):
        if kind == "literal":
            parts.append(re.escape(value))
            continue

        variable = declared[value]
        following = elements[index + 1] if index + 1 < len(elements) else None
        if not variable.anchored:
            if following is not None and following[0] == "variable":
                if not declared[following[1]].anchored:
                    raise RedactorError(
                        f"File discovery '{config.name}' filename.pattern is "
                        f"ambiguous: '{value}' is immediately followed by "
                        f"'{following[1]}' and neither declares values or a format."
                    )

        body = f"(?P<{value}>{_variable_matcher(variable)})"
        if variable.required:
            parts.append(body)
            continue

        # An optional variable absorbs its immediately preceding literal, so
        # omitting the variable also omits its separator and leaves exactly one
        # deterministic interpretation.
        preceding = parts.pop() if parts and elements[index - 1][0] == "literal" else ""
        parts.append(f"(?:{preceding}{body})?")

    return re.compile(f"^{''.join(parts)}$")


def _variable_matcher(variable: DiscoveryVariable) -> str:
    """Return the internal matcher body for one declared variable."""
    if variable.values:
        ordered = sorted(variable.values, key=len, reverse=True)
        return "|".join(re.escape(value) for value in ordered)
    if variable.format:
        return _FORMAT_MATCHERS[variable.format]
    return "[^/]+?"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _prepare_outbox(config: FileDiscoveryConfig) -> Path:
    """Return the configured outbox, created with parents when absent.

    Writability is proven by the manifest write itself rather than by a
    separate probe, so there is exactly one place the outbox can fail late.
    """
    outbox = config.outbox
    if outbox.exists() and not outbox.is_dir():
        raise RedactorError(
            f"File discovery '{config.name}' outbox exists but is not a "
            f"directory: '{outbox}'."
        )
    _logger.debug(
        "attempting outbox creation discovery=%s outbox=%s", config.name, outbox,
    )
    try:
        outbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RedactorError(
            f"File discovery '{config.name}' outbox cannot be created: "
            f"'{outbox}': {exc}"
        ) from exc
    return outbox.resolve()


def _matching_directories(
    config: FileDiscoveryConfig,
) -> list[tuple[Path, dict[str, str]]]:
    """Return each existing directory matching the parameterized source path."""
    pattern = PurePosixPath(config.source_path_pattern)
    anchor = pattern.anchor
    relative = pattern.relative_to(anchor) if anchor else pattern
    glob = "/".join(
        "*" if _PLACEHOLDER.fullmatch(part) else part for part in relative.parts
    )
    if not glob:
        raise RedactorError(f"File discovery '{config.name}' source.path is empty.")

    base = Path(anchor) if anchor else Path.cwd()
    _logger.debug(
        "attempting source scan name=%s base=%s glob=%s", config.name, base, glob,
    )
    try:
        candidates = sorted(base.glob(glob))
    except OSError as exc:
        raise RedactorError(
            f"File discovery '{config.name}' source path is inaccessible: "
            f"'{config.source_path_pattern}': {exc}"
        ) from exc

    matched: list[tuple[Path, dict[str, str]]] = []
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
        except OSError as exc:
            raise RedactorError(
                f"File discovery '{config.name}' source path is inaccessible: "
                f"'{candidate}': {exc}"
            ) from exc
        values = capture_path_variables(candidate, config.source_path_pattern)
        if values is None:
            continue
        matched.append((candidate, values))
    return matched


def _classify_file(
    config: FileDiscoveryConfig, run_log,
    matcher: re.Pattern[str],
    source_file: Path,
    path_values: Mapping[str, str],
) -> dict[str, Any]:
    """Return the accepted or rejected discovery record for one candidate file.

    A rejected record keeps every variable captured reliably before the
    rejection, so an operator can see how far the file got. Variables that were
    never captured are recorded as null, preserving the declared order.
    """
    base = {"source_file": str(source_file), "filename": source_file.name}

    def _reject(code: str, reason: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            **base,
            "status": "rejected",
            "reason_code": code,
            "reason": reason,
            "variables": _ordered_variables(config, variables),
        }

    match = matcher.fullmatch(source_file.name)
    if match is None:
        # Path-sourced values were captured from the directory and stay reliable.
        return _reject(
            _REJECT_FILENAME_MISMATCH,
            "Filename does not match the configured filename pattern.",
            dict(path_values),
        )

    captured = dict(path_values)
    captured.update({key: value for key, value in match.groupdict().items()})
    variables, reason_code, reason = _validate_captured_variables(
        config.variables,
        captured,
    )
    if reason_code is not None:
        return _reject(reason_code, str(reason), variables)
    return {**base, "status": "accepted", "variables": variables}


def _validate_captured_variables(
    variables: tuple[DiscoveryVariable, ...],
    captured: Mapping[str, str | None],
) -> tuple[dict[str, str | None], str | None, str | None]:
    """Validate captured values in declaration order.

    Returns the values validated before any rejection plus a stable reason code
    and reason. This is the existing discovery validation contract factored
    into one reusable boundary for manifest-driven classification.
    """
    validated: dict[str, str | None] = {}
    for variable in variables:
        value = captured.get(variable.name)
        if value is None or (variable.required and value == ""):
            if variable.required:
                return (
                    validated,
                    _REJECT_MISSING_REQUIRED,
                    f"Required variable '{variable.name}' was not captured.",
                )
            validated[variable.name] = None
            continue
        if variable.values and value not in variable.values:
            return (
                validated,
                _REJECT_VALUE_NOT_ALLOWED,
                (
                    f"Variable '{variable.name}' value '{value}' is not a "
                    "configured value."
                ),
            )
        if variable.format and not re.fullmatch(
            _FORMAT_MATCHERS[variable.format], value
        ):
            return (
                validated,
                _REJECT_FORMAT_INVALID,
                (
                    f"Variable '{variable.name}' value '{value}' does not match "
                    f"format '{variable.format}'."
                ),
            )
        validated[variable.name] = value

    return validated, None, None


def _ordered_variables(
    config: FileDiscoveryConfig,
    captured: Mapping[str, Any],
) -> dict[str, Any]:
    """Project captured values in declared order, with null for the uncaptured."""
    return {variable.name: captured.get(variable.name) for variable in config.variables}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _write_manifest(
    ctx: Any, run_log,
    config: FileDiscoveryConfig,
    outbox: Path,
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> Path:
    """Write the discovery manifest atomically and register it once."""
    # The rule name leads the filename so a shared outbox stays self-describing
    # and two rules can never contend for the same manifest name.
    path = run_artifact_path(
        outbox,
        f"{config.name}.{_MANIFEST_ARTIFACT_SUFFIX}",
        _run_timestamp(ctx),
        "json",
    )
    payload = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "discovery_name": config.name,
        "generated_at": _run_timestamp(ctx),
        "source_path_pattern": config.source_path_pattern,
        "file_pattern": config.file_pattern,
        "filename_pattern": config.filename_pattern,
        "declared_variables": [variable.name for variable in config.variables],
        "files": sorted(accepted, key=lambda record: record["source_file"]),
        "rejected": sorted(rejected, key=lambda record: record["source_file"]),
    }
    _logger.debug(
        "attempting manifest write discovery=%s path=%s accepted=%s rejected=%s",
        config.name, path, len(accepted), len(rejected),
    )
    try:
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")
    except OSError as exc:
        raise RedactorError(
            f"File discovery '{config.name}' manifest cannot be written to "
            f"'{path}': {exc}"
        ) from exc

    log_artifact_reference(run_log,
        str(path),
        role="file_discovery_manifest",
        producer="file_operator",
        artifact_group="file_discovery",
        producing_app="file_operator",
        artifact_type="json",
        viewer_type="json",
        safe_to_preview=True,
        discovery_name=config.name,
        accepted_count=len(accepted),
        rejected_count=len(rejected),
    )
    return path


def _run_timestamp(ctx: Any) -> str:
    """Return the run timestamp used for artifact naming and manifest metadata."""
    value = str(
        getattr(ctx, "run_timestamp", "") or getattr(ctx, "run_started_at", "") or ""
    ).strip()
    if not value:
        raise RedactorError(
            "File discovery requires the run timestamp for manifest identity."
        )
    return value


# ---------------------------------------------------------------------------
# Shared accessors
# ---------------------------------------------------------------------------


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    keys = getattr(value, "keys", None)
    if callable(keys):
        if name not in keys():
            return default
        try:
            return value[name]
        except (KeyError, TypeError):
            return getattr(value, name, default)
    getter = getattr(value, "get", None)
    if callable(getter):
        return getter(name, default)
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
        raise RedactorError(f"File discovery '{label}' is missing required '{key}'.")
    return _mapping(mapping[key], f"{label}.{key}")


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


def _optional_bool(value: Any, key: str, label: str, *, default: bool) -> bool:
    result = _get(value, key, None)
    if result is None:
        return default
    if not isinstance(result, bool):
        raise RedactorError(f"{label} field '{key}' must be a boolean.")
    return result
