"""
Explicit CSV feed grouping and combined profiling for file_operator.

The optional ``feeds`` block in ``redact.yaml`` declares which inbox files
belong to which feed using glob ``file_pattern`` rules. Grouping is never
guessed from filenames:

  - 0 matching feed patterns -> unmatched (handled per ``on_unmatched_file``)
  - exactly 1 matching pattern -> processed under that feed
  - 2+ matching patterns      -> ambiguous; the run fails fast

``single_file`` feeds produce one profile per source file (with feed
provenance); ``combined_files`` feeds aggregate every matched file into one
stronger profile per feed, after validating a shared (``same_header``) header.
Raw files move only after their feed's profile(s) succeed.

This module is CSV-only and changes no fixed-width, Excel, SQL, or load
behavior. Profiling facts live in ``rey_lib.profiling``.

Public API
----------
match_files_to_feeds   Map inbox files to feeds (0/1/many) with ambiguity.
run_feed_groups        Process an inbox using explicit feed grouping.
"""

from __future__ import annotations

import argparse
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files.file_routing import FileRoutingError
from rey_lib.files.data_file import DataFile
from rey_lib.files.file import File
from rey_lib.files.file_utils import move_file, visible_files
from rey_lib.logs import AppLogger, log_artifact_reference, log_validation_result, record_file_write
from rey_lib.profiling import (
    enrich_csv_profile,
    is_profile_excluded_column,
    profile_rows,
    same_header_errors,
    validate_csv_profile,
)

from rey_lib.files.error_utils import InputError, RedactorError
from rey_lib.files.file_types import update_file_type_artifact
from rey_lib.files.feed_config import (
    FEED_CONFIG_FILENAME,
    FeedConfig,
    FeedGroup,
    validate_feed_groups,
)
from rey_lib.files.inspection import write_inspection_artifact
from rey_lib.files.layouts.common import effective_max_sample_values
from rey_lib.files.layouts.delimited import CsvParts, build_csv_parts

__all__ = ["match_files_to_feeds", "run_feed_groups", "FeedMatch"]

_log = AppLogger(__name__)

# A column's max length is flagged when the longest file value is at least this
# multiple of the shortest non-zero value across matched files.
_MAX_LENGTH_DIFF_RATIO = 3


@dataclass
class FeedMatch:
    """One feed and the inbox files that matched its pattern."""

    feed:  FeedGroup
    files: list[Path] = field(default_factory=list)


def match_files_to_feeds(
    files: list[Path],
    feeds: list[FeedGroup],
) -> tuple[dict[str, FeedMatch], list[Path], list[str]]:
    """Map inbox files to feeds by glob pattern.

    Parameters
    ----------
    files : list[Path]
        Candidate inbox files.
    feeds : list[FeedGroup]
        Configured feed groups.

    Returns
    -------
    tuple[dict[str, FeedMatch], list[Path], list[str]]
        ``(groups_by_feed_name, unmatched_files, ambiguous_errors)``. A file
        matching 2+ feed patterns produces an ambiguity error and is not grouped.
    """
    groups: dict[str, FeedMatch] = {}
    unmatched: list[Path] = []
    ambiguous: list[str] = []

    for file_path in sorted(files, key=lambda p: p.name):
        matched = [f for f in feeds if fnmatch.fnmatch(file_path.name, f.file_pattern)]
        if not matched:
            unmatched.append(file_path)
        elif len(matched) > 1:
            ambiguous.append(
                "Ambiguous feed pattern match. "
                f"File: {file_path.name}. "
                f"Matched feeds: {', '.join(f.name for f in matched)}. "
                "A source file may belong to only one feed group."
            )
        else:
            feed = matched[0]
            groups.setdefault(feed.name, FeedMatch(feed)).files.append(file_path)

    return groups, unmatched, ambiguous


def run_feed_groups(
    ctx: Any, run_log,
    args: argparse.Namespace,
    feed_cfg: FeedConfig,
    *,
    inbox: Path,
    output_dir: Path,
    processed_path: Path,
    rejected_path: Path,
) -> int:
    """Process an inbox using explicit feed grouping. Returns an exit code.

    Parameters
    ----------
    ctx : Any
        Application context (state logging / movement).
    args : argparse.Namespace
        Base arguments (feed redaction settings already applied).
    feed_cfg : FeedConfig
        Loaded feed config with a non-empty ``feeds`` block.
    inbox : Path
        Inbox directory holding source files.
    output_dir : Path
        Destination for profiles and redacted samples.
    processed_path : Path
        Destination for source files after success.
    rejected_path : Path
        Destination for source files after failure or rejection.

    Returns
    -------
    int
        ``0`` when every group succeeds; ``1`` when a group failed (each such
        failure is reported where it was handled).

    Raises
    ------
    ConfigError
        The feeds config is invalid -- one error carrying every finding.
    InputError
        Feed grouping cannot proceed (ambiguous matches, or unmatched files
        under on_unmatched_file=error) -- one error carrying every problem.
    """
    config_errors = validate_feed_groups(feed_cfg.feeds)
    if config_errors:
        log_validation_result(run_log,
            validation_name="feed_config",
            status="failed",
            message="Invalid feeds config.",
            error_count=len(config_errors),
            errors=config_errors,
        )
        # The operation cannot continue: one error carrying every finding,
        # raised for the application boundary to report once (rule 79).
        raise ConfigError(
            f"invalid feeds config ({len(config_errors)} error(s)): "
            + "; ".join(str(err) for err in config_errors)
        )

    files = [
        p for p in visible_files(inbox, recursive=False)
        if p.name != FEED_CONFIG_FILENAME
    ]
    groups, unmatched, ambiguous = match_files_to_feeds(files, feed_cfg.feeds)

    # Every grouping problem is collected and recorded before deciding; the
    # operation then fails once, through one InputError (rule 79).
    problems: list[str] = []
    if ambiguous:
        log_validation_result(run_log,
            validation_name="feed_grouping",
            status="failed",
            message="Ambiguous feed pattern matches.",
            error_count=len(ambiguous),
            errors=ambiguous,
        )
        problems.extend(str(err) for err in ambiguous)  # do not process while patterns overlap

    if not _handle_unmatched(ctx, run_log, unmatched, feed_cfg.on_unmatched_file, rejected_path):
        problems.append("unmatched files present and on_unmatched_file=error")

    if problems:
        raise InputError(
            f"feed grouping failed ({len(problems)} problem(s)): " + "; ".join(problems)
        )

    status = 0
    for feed_name, match in groups.items():
        _log.info(
            "feed '%s' — pattern=%s mode=%s files=%s",
            feed_name, match.feed.file_pattern, match.feed.profile_mode,
            [p.name for p in match.files],
        )
        try:
            if match.feed.profile_mode == "combined_files":
                _run_combined_feed(ctx, run_log, args, match, output_dir, processed_path)
            else:
                _run_single_file_feed(ctx, run_log, args, match, output_dir, processed_path)
        except (InputError, Exception) as exc:  # noqa: BLE001 — group fails atomically
            # Handled -- the group is rejected and the next one runs -- so
            # reported here (rule 79).
            error = exc if isinstance(exc, AppError) else RedactorError(
                f"feed '{feed_name}' failed")
            if error is not exc:
                error.__cause__ = exc
            error.report(f"processing feed {feed_name}", source=__name__)
            _move_group(ctx, match.files, rejected_path, "rejected")
            status = 1

    return status


# ---------------------------------------------------------------------------
# Private — per-mode processing
# ---------------------------------------------------------------------------

def _run_single_file_feed(
    ctx: Any, run_log,
    args: argparse.Namespace,
    match: FeedMatch,
    output_dir: Path,
    processed_path: Path,
) -> None:
    """Process a single_file feed: one profile per file, then move each file."""
    from rey_lib.files.layouts.delimited import build_single_file_profile

    for file_path in match.files:
        file_args = argparse.Namespace(**vars(args))
        file_args.feed = _feed_dict(match.feed)
        file_args.feed_file_pattern = match.feed.file_pattern
        file_args.profile_scope = "single_file"
        file_args.source_files = [file_path.name]
        file_args.file_count = 1

        parts = build_csv_parts(file_path, file_args)
        prof = build_single_file_profile(parts, file_args)
        _warn_if_any(file_path.name, prof)
        _write_profile_outputs(ctx, run_log, output_dir, file_path.stem, file_path.suffix.lower(),
            [parts.header_line] + parts.sampled_lines, prof,
        )
        inspection_path = write_inspection_artifact(ctx, run_log,
            output_dir,
            source_file=file_path.name,
            source_text_sha256=parts.source_text_sha256,
            delimiter=parts.delimiter,
            encoding=parts.encoding,
            rows=parts.inspection_rows,
        )
        update_file_type_artifact(ctx, run_log,
            output_dir,
            header_fields=parts.header_fields,
            header_index=parts.header_index,
            header_line=parts.header_line,
            source_files=[file_path.name],
            profile=prof,
            profile_artifacts=[f"{file_path.stem}.profile.json"],
            inspection_artifacts=[inspection_path.name],
            filename_regex=getattr(args, "classification_filename_regex", None),
        )
        _move_group(ctx, [file_path], processed_path, "processed")


def _run_combined_feed(
    ctx: Any, run_log,
    args: argparse.Namespace,
    match: FeedMatch,
    output_dir: Path,
    processed_path: Path,
) -> None:
    """Process a combined_files feed: one aggregated profile, then move all files.

    Raises
    ------
    InputError
        On header mismatch or profile validation failure — the whole group is
        rejected by the caller (no partial processing).
    """
    parts_list = [build_csv_parts(f, args) for f in match.files]

    header_pairs = [(p.file_name, p.header_fields) for p in parts_list]
    header_errors = same_header_errors(header_pairs)
    if header_errors:
        log_validation_result(run_log,
            validation_name="combined_feed_headers",
            status="failed",
            message="Matched files do not share the same normalized header.",
            feed_name=match.feed.name,
            error_count=len(header_errors),
            errors=header_errors,
        )
        raise InputError(
            f"Combined CSV profile failed. Feed: {match.feed.name}. "
            "Reason: matched files do not share the same normalized header. "
            + " ".join(header_errors)
        )

    prof = _build_combined_profile(match.feed, parts_list, args)
    _warn_if_any(match.feed.name, prof)

    combined_lines = [parts_list[0].header_line]
    for parts in parts_list:
        combined_lines.extend(parts.sampled_lines)
    _write_profile_outputs(ctx, run_log, output_dir, match.feed.name, ".csv", combined_lines, prof
    )
    inspection_paths = [
        write_inspection_artifact(ctx, run_log,
            output_dir,
            source_file=parts.file_name,
            source_text_sha256=parts.source_text_sha256,
            delimiter=parts.delimiter,
            encoding=parts.encoding,
            rows=parts.inspection_rows,
        )
        for parts in parts_list
    ]
    update_file_type_artifact(ctx, run_log,
        output_dir,
        header_fields=parts_list[0].header_fields,
        header_index=parts_list[0].header_index,
        header_line=parts_list[0].header_line,
        source_files=[parts.file_name for parts in parts_list],
        profile=prof,
        profile_artifacts=[f"{match.feed.name}.profile.json"],
        inspection_artifacts=[path.name for path in inspection_paths],
        filename_regex=getattr(args, "classification_filename_regex", None),
    )
    _move_group(ctx, match.files, processed_path, "processed")


def _build_combined_profile(
    feed: FeedGroup,
    parts_list: list[CsvParts],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Aggregate matched files into one validated combined profile."""
    combined_redacted = [row for p in parts_list for row in p.sampled_redacted]
    combined_original = [row for p in parts_list for row in p.sampled_original]
    redact_columns = parts_list[0].redact_columns
    delimiter = parts_list[0].delimiter
    encoding = parts_list[0].encoding
    profile_header_fields = [
        field for field in parts_list[0].header_fields
        if not is_profile_excluded_column(field)
    ]
    profile_header_line = delimiter.join(profile_header_fields)

    base = profile_rows(
        combined_redacted,
        feed.name,
        "delimited",
        redacted_columns=redact_columns,
        type_rows=combined_original,
    )
    # Delimiter and encoding are read instructions, supplied to enrichment and
    # stated once in loader_hints rather than restated among the dataset facts.
    base.update({
        "file_pattern": feed.file_pattern,
        "header": profile_header_line,
        "detected_header": {
            "index": parts_list[0].header_index,
            "row_number": parts_list[0].header_index + 1,
            "line": profile_header_line,
            "ordered_columns": profile_header_fields,
        },
    })

    prof = enrich_csv_profile(
        base,
        combined_redacted,
        combined_original,
        source_file=f"{feed.name}.csv",
        encoding=encoding,
        delimiter=delimiter,
        has_header=True,
        blank_line_count=sum(p.blank_line_count for p in parts_list),
        ragged_row_count=sum(p.ragged_row_count for p in parts_list),
        max_sample_values=effective_max_sample_values(args),
        feed=_feed_dict(feed),
        profile_scope="combined_files",
        source_files=[p.file_name for p in parts_list],
        file_count=len(parts_list),
        combined_row_count=sum(p.data_line_count for p in parts_list),
        extra_warnings=_cross_file_warnings(parts_list, args),
    )
    errors = validate_csv_profile(prof)
    if errors:
        raise InputError(
            f"Combined CSV profile failed. Feed: {feed.name}. "
            "Reason: profile failed validation. " + " ".join(errors)
        )
    return prof


def _cross_file_warnings(
    parts_list: list[CsvParts],
    args: argparse.Namespace,
) -> list[str]:
    """Return warnings for columns that diverge across matched files.

    Detects mixed date formats, mixed negative formats, blank-in-some-files,
    and large max-length differences across the matched files.
    """
    if len(parts_list) < 2:
        return []

    per_file = [_per_file_columns(p, args) for p in parts_list]
    warnings: list[str] = []

    for column in parts_list[0].header_fields:
        date_formats: set[str] = set()
        negative_formats: set[str] = set()
        has_values: list[bool] = []
        max_lengths: list[int] = []
        for cols in per_file:
            col = cols.get(column)
            if col is None:
                continue
            if col.get("date_format_hint"):
                date_formats.add(col["date_format_hint"])
            if col.get("negative_format"):
                negative_formats.add(col["negative_format"])
            has_values.append(col.get("non_blank_count", 0) > 0)
            max_lengths.append(int(col.get("max_length", 0)))

        if len(date_formats) > 1:
            warnings.append(
                f"Column {column} contains multiple date formats across matched files."
            )
        if {"leading_minus", "parentheses"} <= negative_formats:
            warnings.append(
                f"Column {column} contains both leading-minus and parentheses "
                "negative formats across matched files."
            )
        if any(has_values) and not all(has_values):
            warnings.append(
                f"Column {column} is blank in some matched files but populated in others."
            )
        nonzero = [n for n in max_lengths if n > 0]
        if nonzero and max(nonzero) >= _MAX_LENGTH_DIFF_RATIO * min(nonzero):
            warnings.append(
                f"Column {column} has a large max length difference across matched files."
            )

    return warnings


def _per_file_columns(parts: CsvParts, args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Return one file's per-column enriched facts keyed by raw column name."""
    base = profile_rows(
        parts.sampled_redacted,
        parts.file_name,
        "delimited",
        redacted_columns=parts.redact_columns,
        type_rows=parts.sampled_original,
    )
    base.update({"header": parts.header_line})
    enriched = enrich_csv_profile(
        base, parts.sampled_redacted, parts.sampled_original,
        source_file=parts.file_name, encoding=parts.encoding, delimiter=parts.delimiter,
        max_sample_values=effective_max_sample_values(args),
    )
    return {col["raw_name"]: col for col in enriched["columns"]}


# ---------------------------------------------------------------------------
# Private — unmatched handling, movement, output
# ---------------------------------------------------------------------------

def _handle_unmatched(
    ctx: Any, run_log,
    unmatched: list[Path],
    action: str,
    rejected_path: Path,
) -> bool:
    """Apply the configured unmatched-file action. Returns False to fail the run."""
    if not unmatched:
        return True

    for file_path in unmatched:
        _log.warning(
            "CSV feed grouping warning. File: %s. Reason: no feed file_pattern "
            "matched this file. Configured behavior: %s",
            file_path.name, action,
        )

    if action == "error":
        # Recorded here; the caller fails the operation through one InputError.
        log_validation_result(run_log,
            validation_name="unmatched_files",
            status="failed",
            message="Unmatched files present and on_unmatched_file=error.",
            file_count=len(unmatched),
            files=[path.name for path in unmatched],
        )
        return False
    if action in {"reject", "warn", "skip"}:
        log_validation_result(run_log,
            validation_name="unmatched_files",
            status="warning",
            message=f"Unmatched files handled with on_unmatched_file={action}.",
            file_count=len(unmatched),
            files=[path.name for path in unmatched],
            action=action,
        )
    if action == "reject":
        _move_group(ctx, unmatched, rejected_path, "rejected")
    # "warn" and "skip" leave files in place and continue.
    return True


def _move_group(ctx: Any, files: list[Path], dest_dir: Path, label: str) -> None:
    """Move every file in a group to ``dest_dir`` (all-or-nothing per group)."""
    for file_path in files:
        try:
            dest = move_file(
                file_path, dest_dir, state_ctx=ctx, app="data_engine",
                pipeline=getattr(ctx, "pipeline_name", None), reason=label,
            )
            _log.info("moved to %s: %s", label, dest)
        except Exception as exc:  # noqa: BLE001
            # Handled -- the next file still moves -- so reported here (rule 79).
            error = FileRoutingError(f"could not move {file_path.name} to {label}")
            error.__cause__ = exc
            error.report(f"moving {file_path.name} to {label}", source=__name__)


def _write_profile_outputs(
    ctx: Any, run_log,
    output_dir: Path,
    stem: str,
    ext: str,
    sample_lines: list[str],
    profile: dict[str, Any],
) -> None:
    """Write the redacted sample and the profile JSON for a stem.

    Each file creates its destination directory as it writes, so no direct
    mkdir is needed here.
    """
    state = {"state_ctx": ctx, "app": "data_engine",
             "pipeline": getattr(ctx, "pipeline_name", None), "reason": "profiled"}
    sample_path = output_dir / f"{stem}.redacted.sample{ext}"
    File.for_path(sample_path).write_text("\n".join(sample_lines))
    record_file_write(sample_path, **state)
    log_artifact_reference(run_log, str(sample_path), role="redacted_sample", producer="redactor",
        artifact_group="profiles", producing_app="data_engine",
        artifact_type="redacted_file", viewer_type="file", safe_to_preview=True,
    )
    profile_path = output_dir / f"{stem}.profile.json"
    DataFile.for_path(profile_path, file_type="JSON").write(
        profile, sort_keys=False, atomic=False, trailing_newline=False)
    record_file_write(profile_path, **state)
    log_artifact_reference(run_log, str(profile_path), role="profile", producer="redactor",
        artifact_group="profiles", producing_app="data_engine",
        artifact_type="profile", viewer_type="file", safe_to_preview=True,
    )
    _log.info("wrote profile: %s", profile_path)


def _warn_if_any(label: str, profile: dict[str, Any]) -> None:
    """Log any profile warnings with the feed/file label for context."""
    warnings = profile.get("llm_hints", {}).get("warnings") or []
    if warnings:
        _log.warning("%s: profile warnings — %s", label, warnings)


def _feed_dict(feed: FeedGroup) -> dict[str, Any]:
    """Return the JSON-serialisable feed provenance block for a profile."""
    block: dict[str, Any] = {
        "name": feed.name,
        "file_pattern": feed.file_pattern,
        "profile_mode": feed.profile_mode,
    }
    if feed.combine_strategy:
        block["combine_strategy"] = feed.combine_strategy
    return block
