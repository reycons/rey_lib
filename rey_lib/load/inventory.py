"""
Governed source-file inventory, the Loader's.

Inventories the source sets declared by one workflow process configuration:
each enabled source is enumerated by glob, configured exclusions are rejected,
and every accepted candidate is handed to ``FileManifest.inventory``, which
records it.

Copied from the legacy file_operator implementation (``source_inventory`` and
the inventory half of ``discovery``) into its final Loader-owned location, with
behaviour, ordering, logging and config interpretation unchanged. What changed
is only what the new boundary requires:

  * one candidate is recorded by ``FileManifest.inventory`` -- the stable-size
    check, the checksum, the routine call and the created flags are its -- so
    the step no longer names an insert procedure of its own;
  * failures are ``InventoryError``, this capability's error.

Ownership boundaries:

  * the workflow process configuration owns the inventory scope;
  * this module owns source resolution, enumeration, exclusions, the per-source
    lifecycle and the counts;
  * ``FileManifest.inventory`` owns one candidate's facts and its persistence;
  * ``rey_lib.logs`` owns the validation results and counts it writes.

Inventory never moves, renames, copies, converts, deletes, or otherwise
modifies a source file. It reads each file once to compute its checksum.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import visible_files
from rey_lib.files.manifest import FileManifest
from rey_lib.logs import (
    FileManifestError,
    get_logger,
    log_row_count,
    log_validation_result,
)

__all__ = [
    "InventoryEnumeration",
    "InventoryError",
    "InventorySourceConfig",
    "enumerate_inventory_files",
    "inventory_source_config",
    "resolve_inventory_source_configs",
    "resolve_inventory_sources",
    "run_source_inventory",
]

_logger = get_logger(__name__)

_CONFIG_SECTION = "inventory_source_files"
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_REJECT_EXCLUDED = "excluded"


class InventoryError(AppError):
    """An inventory scope or source that cannot be inventoried as configured."""


@dataclass
class _InventoryCounts:
    """Mutable per-source and aggregate counters for one inventory run."""

    inventoried: int = 0
    #: A file that already had an identity, whose baseline observation was
    #: missing and has been recorded again. Not a restoration: the deleted
    #: evidence is gone, and this is a new observation in this run.
    inventory_recorded: int = 0
    already_inventoried: int = 0
    rejected: int = 0
    failed: int = 0

    def absorb(self, other: "_InventoryCounts") -> None:
        """Accumulate one source's counters into this aggregate."""
        self.inventoried += other.inventoried
        self.inventory_recorded += other.inventory_recorded
        self.already_inventoried += other.already_inventoried
        self.rejected += other.rejected
        self.failed += other.failed


def run_source_inventory(ctx: Any, run_log: Any, process_config: Any) -> int:
    """
    Inventory every configured source set declared by the process configuration.

    The whole scope is validated before any enumeration: a malformed enabled
    source is a configuration failure, not a rejected file, so no partial scope
    is ever inventoried. Once the scope is valid each source runs
    independently -- one source's failure is recorded and the remaining sources
    still run.

    Parameters
    ----------
    ctx : Any
        Resolved application context.
    run_log : Any
        The run log validation results and counts are written to.
    process_config : Any
        The effective workflow process configuration carrying ``sources``.

    Returns
    -------
    int
        Zero when every source completed and every eligible file was either
        inventoried or already present; one when any source failed or any file
        could not be inventoried.
    """
    configs = resolve_inventory_source_configs(process_config)

    totals = _InventoryCounts()
    failed_sources = 0

    for config in configs:
        try:
            counts = _inventory_source(ctx, run_log, config)
        except (InventoryError, FileManifestError, OSError) as exc:
            failed_sources += 1
            _record_source_failure(ctx, run_log, config.name, exc)
            continue
        totals.absorb(counts)

    _log_totals(ctx, run_log, totals, failed_sources)
    return 1 if failed_sources or totals.failed else 0


def _inventory_source(
    ctx: Any, run_log: Any,
    config: "InventorySourceConfig",
) -> _InventoryCounts:
    """Enumerate one source set and inventory each newly governed file."""
    enumeration = enumerate_inventory_files(config)
    counts = _InventoryCounts(rejected=len(enumeration.rejected))

    for record in enumeration.accepted:
        outcome = _inventory_file(ctx, run_log, config, record)
        if outcome == "inventoried":
            counts.inventoried += 1
        elif outcome == "inventory_recorded":
            counts.inventory_recorded += 1
        elif outcome == "already_inventoried":
            counts.already_inventoried += 1
        else:
            counts.failed += 1

    log_validation_result(run_log,
        validation_name="source_inventory_source",
        status="passed" if not counts.failed else "failed",
        message=(
            f"Inventoried {counts.inventoried} file(s) for '{config.name}'; "
            f"{counts.inventory_recorded} observation(s) recorded for files "
            f"whose baseline was missing, "
            f"{counts.already_inventoried} already inventoried, "
            f"{counts.rejected} rejected, {counts.failed} failed."
        ),
        source_name=config.name,
    )
    return counts


def _inventory_file(
    ctx: Any, run_log: Any,
    config: "InventorySourceConfig",
    record: dict[str, Any],
) -> str:
    """
    Inventory one accepted candidate through ``FileManifest.inventory``.

    Returns ``"inventoried"``, ``"inventory_recorded"``, ``"already_inventoried"``
    or ``"failed"``. A failure is recorded with its exact reason and the run
    goes on.
    """
    path = str(record["source_file"])

    control = getattr(ctx, "shared_control", None)
    if control is None:
        _record_file_failure(ctx, run_log, config.name, path,
            "the governed manifest is held in the control database, and this "
            "context exposes no shared Control to reach it through")
        return "failed"

    # The inventory record identifies the configured inventory source, not a
    # classified feed. Deriving a feed here would reintroduce the classification
    # coupling that a later lifecycle step owns.
    outcome = FileManifest(control).inventory(
        path,
        source_name=config.name,
        evidence=None,
        producer={"application": str(getattr(ctx, "app_name", "") or "")},
    )
    if outcome.status == "failed":
        _record_file_failure(ctx, run_log, config.name, path, outcome.reason or "")
    return outcome.status


def _record_source_failure(ctx: Any, run_log: Any, source_name: str, exc: Exception) -> None:
    """Record one failed source without stopping the remaining sources."""
    log_validation_result(run_log,
        validation_name="source_inventory_source",
        status="failed",
        message=str(exc),
        source_name=source_name or "<unnamed>",
    )


def _record_file_failure(
    ctx: Any, run_log: Any,
    source_name: str,
    path: str,
    reason: Exception | str,
) -> None:
    """Record one file that could not be inventoried, with its exact reason."""
    log_validation_result(run_log,
        validation_name="source_inventory_file",
        status="failed",
        message=f"'{path}' was not inventoried: {reason}",
        source_name=source_name,
        path=path,
    )


def _log_totals(ctx: Any, run_log: Any, totals: _InventoryCounts, failed_sources: int) -> None:
    """Emit the deterministic aggregate counts for the whole inventory run."""
    for count_name, count in (
        ("source_files_inventoried", totals.inventoried),
        ("source_files_inventory_recorded", totals.inventory_recorded),
        ("source_files_already_inventoried", totals.already_inventoried),
        ("source_files_rejected", totals.rejected),
        ("source_files_failed", totals.failed),
        ("inventory_sources_failed", failed_sources),
    ):
        log_row_count(run_log, count_name=count_name, count=count, subject=_CONFIG_SECTION)


# ---------------------------------------------------------------------------
# Source configuration and enumeration
#
# Inventory sources are declared in the workflow process configuration and
# locate files by glob. Inventory reports filesystem facts only -- it does not
# classify, capture variables, or interpret filenames; a later lifecycle step
# owns classification and appends its own evidence.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventorySourceConfig:
    """Validated configuration for one workflow-declared inventory source."""

    name: str
    enabled: bool
    source_path_pattern: str
    file_patterns: tuple[str, ...]
    recursive: bool
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class InventoryEnumeration:
    """Deterministic accepted and rejected candidates for one inventory source."""

    config: InventorySourceConfig
    accepted: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]


def resolve_inventory_sources(process_config: Any) -> tuple[Any, ...]:
    """Return the ordered 'sources' entries from the workflow process config.

    Only the section shape is validated here -- presence, list type, and unique
    names -- so one malformed source cannot prevent the others from running.
    Each entry is validated independently when it is executed.
    """

    sources = _get(process_config, "sources", None)
    if sources is None:
        raise InventoryError(
            "Inventory process configuration is missing required 'sources'."
        )
    if not isinstance(sources, list):
        raise InventoryError("Inventory process 'sources' must be a list.")
    if not sources:
        raise InventoryError("Inventory process 'sources' declares no sources.")

    seen: set[str] = set()
    for index, entry in enumerate(sources):
        name = str(_get(entry, "name", "")).strip()
        if not name:
            raise InventoryError(
                f"Inventory process sources[{index}] requires a non-empty 'name'."
            )
        if name in seen:
            raise InventoryError(
                f"Inventory process sources declares duplicate source '{name}'."
            )
        seen.add(name)
    return tuple(sources)


def resolve_inventory_source_configs(
    process_config: Any,
) -> tuple[InventorySourceConfig, ...]:
    """Validate every declared source and return the enabled, valid ones.

    Every enabled source is validated independently so one malformed source
    does not mask the others, and every failure is reported together. A
    malformed enabled source is a configuration failure, not a rejected file:
    the whole inventory fails before any enumeration begins rather than
    silently running a partial scope. A disabled source is skipped and is not
    validated further.
    """

    entries = resolve_inventory_sources(process_config)
    configs: list[InventorySourceConfig] = []
    failures: list[str] = []

    for entry in entries:
        name = str(_get(entry, "name", "")).strip()
        try:
            if not _required_bool(entry, "enabled", name):
                continue
            configs.append(inventory_source_config(entry, name))
        except InventoryError as exc:
            failures.append(f"{name}: {exc}")

    if failures:
        raise InventoryError(
            "Inventory configuration is invalid; no source was enumerated. "
            + " | ".join(failures)
        )
    return tuple(configs)


def inventory_source_config(entry: Any, name: str) -> InventorySourceConfig:
    """Validate and project one inventory source entry into typed configuration."""

    source = _required_mapping(entry, "source", name)
    path_pattern = _required_string(source, "path", name)
    if _PLACEHOLDER.search(path_pattern):
        raise InventoryError(
            f"Inventory source '{name}' source.path must not declare "
            f"placeholders; inventory locates files by glob and captures nothing."
        )

    return InventorySourceConfig(
        name=name,
        enabled=_required_bool(entry, "enabled", name),
        source_path_pattern=path_pattern,
        file_patterns=_inventory_file_patterns(source, name),
        recursive=_optional_bool(source, "recursive", name, default=False),
        exclusions=_inventory_exclusions(entry, name),
    )


def enumerate_inventory_files(config: InventorySourceConfig) -> InventoryEnumeration:
    """Enumerate every candidate file for one inventory source.

    Locating a file is a glob over the configured source path; the glob already
    resolves each matching directory, so a matched directory is accepted
    directly. The result carries filesystem facts only -- inventory does not
    classify, capture variables, or interpret filenames. A configured exclusion
    is the only rejection, and it is a deterministic record rather than a
    failure.
    """

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for directory in _inventory_directories(config):
        for source_file in visible_files(
            directory, config.file_patterns, recursive=config.recursive
        ):
            record = _inventory_candidate(config, source_file)
            if record["status"] == "accepted":
                accepted.append(record)
            else:
                rejected.append(record)

    return InventoryEnumeration(
        config=config,
        accepted=tuple(sorted(accepted, key=lambda item: item["source_file"])),
        rejected=tuple(sorted(rejected, key=lambda item: item["source_file"])),
    )


def _inventory_directories(config: InventorySourceConfig) -> list[Path]:
    """Return each existing directory matched by the configured source path.

    The pattern is an ordinary glob. Its wildcard segments are resolved by the
    glob itself, so a matched directory needs no further verification.
    """
    pattern = PurePosixPath(config.source_path_pattern)
    anchor = pattern.anchor
    relative = pattern.relative_to(anchor) if anchor else pattern
    glob = str(relative)
    if not glob or glob == ".":
        raise InventoryError(f"Inventory source '{config.name}' source.path is empty.")

    base = Path(anchor) if anchor else Path.cwd()
    _logger.debug(
        "attempting source scan name=%s base=%s glob=%s", config.name, base, glob,
    )
    try:
        candidates = sorted(base.glob(glob))
    except OSError as exc:
        raise InventoryError(
            f"Inventory source '{config.name}' source path is inaccessible: "
            f"'{config.source_path_pattern}': {exc}"
        ) from exc

    matched: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_dir():
                matched.append(candidate)
        except OSError as exc:
            raise InventoryError(
                f"Inventory source '{config.name}' source path is inaccessible: "
                f"'{candidate}': {exc}"
            ) from exc
    return matched


def _inventory_candidate(
    config: InventorySourceConfig,
    source_file: Path,
) -> dict[str, Any]:
    """Return the filesystem facts for one enumerated candidate.

    Inventory neither classifies nor interprets a filename. A configured
    exclusion is the only reason a candidate is rejected.
    """

    base = {"source_file": str(source_file), "filename": source_file.name}

    excluded = _matched_exclusion(config, source_file.name)
    if excluded is not None:
        return {
            **base,
            "status": "rejected",
            "reason_code": _REJECT_EXCLUDED,
            "reason": f"Filename matches configured exclusion '{excluded}'.",
        }

    return {**base, "status": "accepted"}


def _matched_exclusion(config: InventorySourceConfig, filename: str) -> str | None:
    """Return the first configured exclusion the filename matches, if any."""
    for pattern in config.exclusions:
        if fnmatch.fnmatch(filename, pattern):
            return pattern
    return None


def _inventory_file_patterns(source: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """Return the configured file patterns from exactly one accepted form."""
    single = _get(source, "file_pattern", None)
    plural = _get(source, "file_patterns", None)
    if single is not None and plural is not None:
        raise InventoryError(
            f"Inventory source '{name}' declares both 'file_pattern' and "
            f"'file_patterns'; declare exactly one."
        )
    if single is not None:
        return (_required_string(source, "file_pattern", name),)
    if not isinstance(plural, list) or not plural:
        raise InventoryError(
            f"Inventory source '{name}' requires 'file_pattern' or a non-empty "
            f"'file_patterns' list."
        )
    patterns: list[str] = []
    for index, pattern in enumerate(plural):
        if not isinstance(pattern, str) or not pattern.strip():
            raise InventoryError(
                f"Inventory source '{name}' file_patterns[{index}] must be a "
                f"non-empty string."
            )
        patterns.append(pattern.strip())
    return tuple(patterns)


def _inventory_exclusions(entry: Any, name: str) -> tuple[str, ...]:
    """Return the configured exclusion globs, which are optional."""
    raw = _get(entry, "exclusions", None)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InventoryError(f"Inventory source '{name}' 'exclusions' must be a list.")
    exclusions: list[str] = []
    for index, pattern in enumerate(raw):
        if not isinstance(pattern, str) or not pattern.strip():
            raise InventoryError(
                f"Inventory source '{name}' exclusions[{index}] must be a "
                f"non-empty string."
            )
        exclusions.append(pattern.strip())
    return tuple(exclusions)


# ---------------------------------------------------------------------------
# Configuration accessors
# ---------------------------------------------------------------------------


def _get(value: Any, name: str, default: Any = None) -> Any:
    """Read one field from a mapping, a namespace or a mapping-like config."""
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
    """Return ``value`` as a mapping, or refuse it."""
    if isinstance(value, Mapping):
        return value
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): getattr(value, key) for key in keys()}
    if hasattr(value, "__dict__"):
        return vars(value)
    raise InventoryError(f"{label} must be a mapping.")


def _required_mapping(value: Any, key: str, label: str) -> Mapping[str, Any]:
    """Return the required mapping field ``key``."""
    mapping = _mapping(value, label)
    if key not in mapping:
        raise InventoryError(f"File discovery '{label}' is missing required '{key}'.")
    return _mapping(mapping[key], f"{label}.{key}")


def _required_string(value: Any, key: str, label: str) -> str:
    """Return the required non-empty string field ``key``."""
    result = _get(value, key, None)
    if not isinstance(result, str) or not result.strip():
        raise InventoryError(f"{label} requires non-empty string field '{key}'.")
    return result.strip()


def _required_bool(value: Any, key: str, label: str) -> bool:
    """Return the required boolean field ``key``."""
    result = _get(value, key, None)
    if not isinstance(result, bool):
        raise InventoryError(f"{label} requires boolean field '{key}'.")
    return result


def _optional_bool(value: Any, key: str, label: str, *, default: bool) -> bool:
    """Return the optional boolean field ``key``, or ``default``."""
    result = _get(value, key, None)
    if result is None:
        return default
    if not isinstance(result, bool):
        raise InventoryError(f"{label} field '{key}' must be a boolean.")
    return result
