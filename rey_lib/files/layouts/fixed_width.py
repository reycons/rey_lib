"""
Fixed-width layout handler.

Reads raw lines via ``rey_lib.files`` and redacts fixed character ranges,
preserving each line so only the sensitive ranges are substituted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs import AppLogger
from rey_lib.profiling.file_profiler import profile_rows
from rey_lib.redaction.redactors import redact_fixed_width
from rey_lib.redaction.registry import RedactionRegistry

from rey_lib.files.input_utils import parse_ranges
from rey_lib.files.layouts import layout
from rey_lib.files.layouts.common import (
    effective_encoding,
    effective_sample_size,
    log_summary,
    write_outputs,
)
from rey_lib.files.csv import sample_indices
from rey_lib.files.file_utils import read_text_file

_log = AppLogger(__name__)


@layout("fixed_width")
def process(ctx: Any, run_log, file_path: Path, output_dir: Path, args: Any) -> None:
    """Fixed-width pipeline: read raw lines → redact ranges → sample → write.

    Parameters
    ----------
    ctx : Any
        Application context (used for output state logging).
    file_path : Path
        Source fixed-width file.
    output_dir : Path
        Destination directory for output artifacts.
    args : Any
        Parsed CLI/feed arguments (encoding, redact_ranges, sample_size).
    """
    encoding    = effective_encoding(args)
    sample_size = effective_sample_size(args)
    ranges, col_names = parse_ranges(args.redact_ranges)

    raw_lines = read_text_file(file_path, encoding=encoding).splitlines()
    _log.info("%s: %d lines read.", file_path.name, len(raw_lines))

    explicit_masks = getattr(args, "redact_masks", None) or {}
    registry       = RedactionRegistry(col_names, mask_types=explicit_masks or None)
    redacted_lines = redact_fixed_width(raw_lines, ranges, registry, col_names)

    idx           = sample_indices(len(redacted_lines), sample_size)
    sampled_lines = [redacted_lines[i] for i in idx]

    fw_rows          = _fixed_width_to_dicts(raw_lines, ranges, col_names)
    redacted_fw_rows = _fixed_width_to_dicts(redacted_lines, ranges, col_names)
    prof = profile_rows(
        redacted_fw_rows,
        file_path.name,
        "fixed_width",
        redacted_columns=col_names,
        type_rows=fw_rows,
    )
    write_outputs(ctx, run_log, file_path, output_dir, sampled_lines, prof)

    log_summary(ctx, run_log, file_path.name, registry, len(raw_lines), len(sampled_lines))


def _fixed_width_to_dicts(
    lines:     list[str],
    ranges:    list[tuple[int, int]],
    col_names: list[str],
) -> list[dict[str, Any]]:
    """Extract fixed-width fields from raw text lines into row dicts.

    Ranges are 1-based and inclusive (e.g. ``(2, 13)`` extracts characters
    2 through 13 of each line).
    """
    rows: list[dict[str, Any]] = []
    for line in lines:
        row: dict[str, Any] = {}
        for (start, end), col in zip(ranges, col_names):
            # Slice is zero-based; range spec is 1-based inclusive.
            row[col] = line[start - 1:end]
        rows.append(row)
    return rows
