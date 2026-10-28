"""
Shared orchestration helpers for file_operator layout handlers.

These helpers are layout-agnostic: effective-setting resolution, the redaction
plan, the output writer, and the per-file summary. Raw file reading is provided
by ``rey_lib.files``; each layout handler parses the raw content itself.

Public API
----------
effective_max_sample_rows Resolve the governed row limit from args.
effective_encoding    Resolve encoding from args, else the code default.
effective_delimiter   Resolve delimiter from args, else the code default.
prepare_redaction     Build the (redact_columns, mask_types) plan.
write_outputs         Write the redacted sample and the profile JSON.
log_summary           Log a per-file completion summary.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rey_lib.files.data_file import DataFile
from rey_lib.files.file import File
from rey_lib.logs import AppLogger, log_artifact_reference, log_row_count, record_file_write
from rey_lib.redaction.registry import RedactionRegistry

from rey_lib.files.error_utils import InputError
from rey_lib.files.input_utils import parse_column_list

__all__: list[str] = [
    "effective_sample_size",
    "effective_max_sample_rows",
    "effective_encoding",
    "effective_delimiter",
    "effective_max_sample_values",
    "prepare_redaction",
    "write_outputs",
    "log_summary",
    "log_summary_counts",
    "DEFAULT_MAX_SAMPLE_ROWS",
    "DEFAULT_ENCODING",
    "DEFAULT_DELIMITER",
    "DEFAULT_MAX_SAMPLE_VALUES",
]

_log = AppLogger("file_operator.layouts")

# Code-level fallbacks used only when neither the YAML nor CLI specifies a value.
DEFAULT_MAX_SAMPLE_ROWS: int = 500
DEFAULT_ENCODING:    str = "utf-8-sig"
DEFAULT_DELIMITER:   str = ","
DEFAULT_MAX_SAMPLE_VALUES: int = 10


def effective_max_sample_rows(args: Any) -> int:
    """Return the one governed sample-row limit from canonical or legacy args."""
    canonical = getattr(args, "max_sample_rows", None)
    legacy = getattr(args, "sample_size", None)
    if canonical is not None and legacy is not None:
        raise InputError(
            "max_sample_rows and legacy sample_size cannot both be supplied."
        )
    value = canonical if canonical is not None else legacy
    return int(value) if value else DEFAULT_MAX_SAMPLE_ROWS


def effective_sample_size(args: Any) -> int:
    """Compatibility wrapper for the canonical sample-row limit resolver."""
    return effective_max_sample_rows(args)


def effective_encoding(args: Any) -> str:
    """Return the file encoding from args, falling back to the code default."""
    return getattr(args, "encoding", None) or DEFAULT_ENCODING


def effective_delimiter(args: Any) -> str:
    """Return the field delimiter from args, falling back to the code default."""
    return getattr(args, "delimiter", None) or DEFAULT_DELIMITER


def effective_max_sample_values(args: Any) -> int:
    """Return the per-column sample-value cap from args, else the code default.

    Resolved from the ``profiling.max_sample_values_per_column`` feed setting
    (surfaced on ``args`` by feed config), falling back to the code default.
    """
    val = getattr(args, "max_sample_values_per_column", None)
    return int(val) if val else DEFAULT_MAX_SAMPLE_VALUES


def prepare_redaction(
    args:              Any,
    *,
    available_columns: list[str],
    detect_masks:      Callable[[list[str]], dict[str, str]],
    source_name:       str,
) -> tuple[list[str], dict[str, str]]:
    """Return the single redaction plan used by tabular layouts.

    Parameters
    ----------
    args : Any
        Parsed CLI/feed arguments (redact_columns, redact_all, redact_masks).
    available_columns : list[str]
        Column names present in the file.
    detect_masks : Callable[[list[str]], dict[str, str]]
        Layout-specific mask detector for the chosen redaction columns.
    source_name : str
        Source file name (for logging).

    Returns
    -------
    tuple[list[str], dict[str, str]]
        ``(redact_columns, mask_types)``.
    """
    requested_columns = parse_column_list(args.redact_columns)
    if getattr(args, "redact_all", False):
        redact_columns = list(available_columns)
    else:
        available = set(available_columns)
        redact_columns = [col for col in requested_columns if col in available]

    mask_types = detect_masks(redact_columns)
    explicit_masks = getattr(args, "redact_masks", None) or {}
    mask_types = {**mask_types, **explicit_masks}

    _log.info(
        "%s: redaction plan — columns=%s masks=%s",
        source_name, redact_columns, mask_types,
    )
    return redact_columns, mask_types


def write_outputs(
    ctx:          Any, run_log,
    file_path:    Path,
    output_dir:   Path,
    sample_lines: list[str],
    profile:      dict[str, Any],
) -> None:
    """Write the two output artifacts for one processed file.

    Parameters
    ----------
    ctx : Any
        Application context — passed to write_file for state logging.
    file_path : Path
        Original source file — used to derive output file names.
    output_dir : Path
        Destination directory.  Created if absent.
    sample_lines : list[str]
        Raw redacted lines to write to the sample file.
    profile : dict[str, Any]
        Structured profile from ``profile_rows``.
    """
    stem = file_path.stem
    ext  = file_path.suffix.lower()
    _state = {
        "state_ctx": ctx,
        "app": "file_operator",
        "pipeline": getattr(ctx, "pipeline_name", None),
        "reason": "profiled",
    }

    source_path = str(file_path)

    sample_path = output_dir / f"{stem}.redacted.sample{ext}"
    File.for_path(sample_path).write_text("\n".join(sample_lines))
    record_file_write(sample_path, **_state)
    _log.info("wrote sample:  %s", sample_path)
    # The redacted sample is a first-class, safe-to-preview redactor artifact; the
    # console prefers it over the original source file
    # (SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector).
    log_artifact_reference(run_log, str(sample_path), role="redacted_sample", producer="redactor",
        artifact_group="profiles", producing_app="file_operator",
        artifact_type="redacted_file", source_path=source_path,
        viewer_type="file", safe_to_preview=True,
    )

    profile_path = output_dir / f"{stem}.profile.json"
    DataFile.for_path(profile_path, file_type="JSON").write(
        profile, sort_keys=False, atomic=False, trailing_newline=False)
    record_file_write(profile_path, **_state)
    _log.info("wrote profile: %s", profile_path)
    log_artifact_reference(run_log, str(profile_path), role="profile", producer="redactor",
        artifact_group="profiles", producing_app="file_operator",
        artifact_type="profile", source_path=source_path,
        viewer_type="file", safe_to_preview=True,
    )


def log_summary(
    ctx:      Any, run_log,
    name:     str,
    registry: RedactionRegistry,
    total:    int,
    sampled:  int,
) -> None:
    """Log a per-file completion summary with redaction counts."""
    log_summary_counts(ctx, run_log, name, registry.summary(), total, sampled)


def log_summary_counts(
    ctx:               Any, run_log,
    name:              str,
    redaction_summary: dict[str, int],
    total:             int,
    sampled:           int,
) -> None:
    """Emit one file-level summary and shared row-count evidence."""
    active_counts = {col: n for col, n in redaction_summary.items() if n > 0}
    _log.info(
        "%s: complete — total=%d  sampled=%d  redacted_values=%s",
        name, total, sampled, active_counts,
    )
    log_row_count(run_log,
        count_name="rows_total",
        count=total,
        subject=name,
    )
    log_row_count(run_log,
        count_name="rows_sampled",
        count=sampled,
        subject=name,
    )
    log_row_count(run_log,
        count_name="redacted_values",
        count=sum(active_counts.values()),
        subject=name,
        redacted_columns=sorted(active_counts),
    )
