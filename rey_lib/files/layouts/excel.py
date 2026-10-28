"""
Excel (.xlsx / .xlsm / .xls) layout handler.

Reads worksheet rows and redacts configured columns. The redacted sample is
written as comma-joined lines so downstream profiling sees tabular data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs import AppLogger
from rey_lib.profiling.file_profiler import profile_rows
from rey_lib.redaction.detector import SAMPLE_SIZE, detect_mask_type
from rey_lib.redaction.registry import RedactionRegistry

from rey_lib.files.error_utils import InputError
from rey_lib.files.layouts import layout
from rey_lib.files.layouts.common import (
    effective_sample_size,
    log_summary,
    prepare_redaction,
    write_outputs,
)
from rey_lib.files.csv import sample_indices

_log = AppLogger(__name__)


@layout("excel")
def process(ctx: Any, run_log, file_path: Path, output_dir: Path, args: Any) -> None:
    """Excel pipeline: read rows → redact columns → sample → write.

    Parameters
    ----------
    ctx : Any
        Application context (used for output state logging).
    file_path : Path
        Source Excel file.
    output_dir : Path
        Destination directory for output artifacts.
    args : Any
        Parsed CLI/feed arguments (worksheet, redaction, sample_size).
    """
    sample_size = effective_sample_size(args)
    worksheet   = getattr(args, "worksheet", None) or None

    rows = _read_excel(file_path, worksheet)
    _log.info("%s: %d rows read.", file_path.name, len(rows))

    redact_columns, mask_types = prepare_redaction(
        args,
        available_columns=list(rows[0].keys()) if rows else [],
        detect_masks=lambda columns: _detect_dict_masks(columns, rows),
        source_name=file_path.name,
    )

    registry   = RedactionRegistry(redact_columns, mask_types=mask_types or None)
    redact_set = set(redact_columns)

    redacted_rows: list[dict[str, Any]] = []
    output_lines:  list[str]            = []

    for row in rows:
        redacted: dict[str, Any] = {}
        for col, val in row.items():
            raw = str(val) if val is not None else ""
            redacted[col] = registry.redact(col, raw) if col in redact_set else raw
        redacted_rows.append(redacted)
        output_lines.append(",".join(str(v) for v in redacted.values()))

    idx           = sample_indices(len(output_lines), sample_size)
    sampled_lines = [output_lines[i] for i in idx]
    sampled_dicts = [redacted_rows[i] for i in idx]
    sampled_original_dicts = [rows[i] for i in idx]

    header_line      = ",".join(rows[0].keys()) if rows else ""
    all_sample_lines = ([header_line] + sampled_lines) if header_line else sampled_lines

    prof = profile_rows(
        sampled_dicts,
        file_path.name,
        "excel",
        redacted_columns=redact_columns,
        type_rows=sampled_original_dicts,
    )
    write_outputs(ctx, run_log, file_path, output_dir, all_sample_lines, prof)

    log_summary(ctx, run_log, file_path.name, registry, len(rows), len(sampled_lines))


def _read_excel(file_path: Path, worksheet: str | None) -> list[dict[str, Any]]:
    """Read an Excel worksheet into a list of row dicts (header = first row)."""
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required for Excel support: pip install openpyxl"
        ) from exc

    try:
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    except Exception as exc:  # openpyxl raises several different error types
        raise InputError(f"Cannot open Excel file '{file_path}': {exc}") from exc

    ws = wb[worksheet] if worksheet else wb.active
    rows_iter = ws.iter_rows(values_only=True)

    raw_headers = next(rows_iter, None)
    if raw_headers is None:
        return []

    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(raw_headers)]
    return [dict(zip(headers, row)) for row in rows_iter]


def _detect_dict_masks(columns: list[str], rows: list[dict]) -> dict[str, str]:
    """Return a mask-type dict for every column by sampling row dicts."""
    col_values: dict[str, list[str]] = {col: [] for col in columns}

    for row in rows[:SAMPLE_SIZE]:
        for col in columns:
            val = str(row.get(col, "")).strip()
            if val:
                col_values[col].append(val)

    return {col: detect_mask_type(vals) for col, vals in col_values.items()}
