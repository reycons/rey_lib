"""
Delimited-file layout functions the Loader profiles through (row 589, step 5).

Copied from the legacy file_operator ``layouts.delimited`` module, which stays
untouched as the reference. Only the profile path's dependency closure is
copied, function for function: ``build_csv_parts``,
``build_clean_single_file_profile`` and the private helpers they call. The
layout handler (``process``), its registration and the redacted single-file
profile join this module when a step's production path reaches them.

Redacts the configured fields between delimiters on each line, preserving the
raw line so only the sensitive values are substituted (original quoting and
spacing are kept). Raw reading is delegated to ``rey_lib.files``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.logs import get_logger
from rey_lib.files.csv import (
    CsvRead,
    parse_delimited_line,
    read_csv,
    sample_indices,
)
from rey_lib.profiling import (
    enrich_csv_profile,
    is_profile_excluded_column,
    normalized_header,
    profile_rows,
    validate_csv_profile,
)
from rey_lib.redaction.detector import SAMPLE_SIZE, detect_mask_type
from rey_lib.redaction.registry import RedactionRegistry

from rey_lib.load.inspection import InspectionRow
from rey_lib.load.layouts.common import (
    effective_delimiter,
    effective_encoding,
    effective_max_sample_values,
    effective_sample_size,
    prepare_redaction,
)
from rey_lib.load.profile_errors import ProfilingError

_log = get_logger(__name__)


@dataclass
class CsvParts:
    """Reusable read → redact → sample result for one delimited CSV file.

    Shared by the single-file profile path and the combined-feed profiler so
    both consume identical redacted/unredacted sampled rows.
    """

    file_name:         str
    header_fields:     list[str]
    header_line:       str
    header_index:      int
    delimiter:         str
    encoding:          str
    sampled_lines:     list[str]
    sampled_redacted:  list[dict[str, Any]]
    sampled_original:  list[dict[str, Any]]
    redact_columns:    list[str]
    blank_line_count:  int
    ragged_row_count:  int
    data_line_count:   int
    source_text_sha256: str
    inspection_rows: tuple[InspectionRow, ...]
    redaction_summary: dict[str, int] = field(default_factory=dict)


def build_csv_parts(
    file_path: Path,
    args: Any,
    *,
    redact_rows: bool = True,
) -> CsvParts:
    """Read, redact, and sample one delimited CSV file (no output written).

    The structural read is answered by ``rey_lib.files.csv``; this function
    supplies criteria, applies File Operator redaction to the answer, and
    assembles the application-facing result.

    Parameters
    ----------
    file_path : Path
        Source delimited file.
    args : Any
        Parsed CLI/feed arguments (delimiter, encoding, redaction, hints).

    Returns
    -------
    CsvParts
        Header, sampled redacted/unredacted rows, and structural counts.
    """
    delimiter   = effective_delimiter(args)
    encoding    = effective_encoding(args)
    sample_size = effective_sample_size(args)

    header_contains = getattr(args, "header_contains", None) or None
    source_read = read_csv(
        file_path,
        encoding=encoding,
        delimiter=delimiter,
        required_header=[header_contains] if header_contains else (),
        skip_blank_lines=bool(getattr(args, "skip_blank_lines", False)),
    )
    if header_contains and not source_read.header_matched_all:
        raise ProfilingError(
            f"Header containing '{header_contains}' not found in file."
        )
    if not source_read.has_header:
        raise ProfilingError(
            "Could not identify a delimited header row with sufficient "
            "structural confidence."
        )

    header_fields = list(source_read.header_fields)
    data_lines = [row.text for row in source_read.rows]
    header_index = (source_read.header_line_number or 1) - 1
    _log.info("%s: %d rows read.", file_path.name, len(data_lines))

    if redact_rows:
        redact_columns, mask_types = prepare_redaction(
            args,
            available_columns=header_fields,
            detect_masks=lambda columns: _detect_delimited_masks(
                columns, data_lines, delimiter
            ),
            source_name=file_path.name,
        )
    else:
        # Profile-only redaction starts from the completed normal profile and
        # must not route through sampled-row mask detection.
        redact_columns, mask_types = [], {}

    registry   = RedactionRegistry(redact_columns, mask_types=mask_types or None)
    redact_set = set(redact_columns)
    redact_idx: dict[int, str] = {
        i: h for i, h in enumerate(header_fields) if h in redact_set
    }

    redacted_lines, row_dicts, original_row_dicts = _redact_delimited_lines(
        data_lines, header_fields, redact_idx, registry, delimiter
    )
    inspection_rows = _build_inspection_rows(
        file_path.name,
        source_read,
        redacted_lines,
        delimiter,
    )

    idx = sample_indices(len(redacted_lines), sample_size)
    return CsvParts(
        file_name=file_path.name,
        header_fields=header_fields,
        header_line=delimiter.join(header_fields),
        header_index=header_index,
        delimiter=delimiter,
        encoding=encoding,
        sampled_lines=[redacted_lines[i] for i in idx],
        sampled_redacted=[row_dicts[i] for i in idx],
        sampled_original=[original_row_dicts[i] for i in idx],
        redact_columns=redact_columns,
        blank_line_count=source_read.blank_row_count,
        ragged_row_count=source_read.ragged_row_count,
        data_line_count=len(data_lines),
        source_text_sha256=source_read.source_text_sha256,
        inspection_rows=inspection_rows,
        redaction_summary=registry.summary(),
    )


def build_clean_single_file_profile(parts: CsvParts, args: Any) -> dict[str, Any]:
    """Build the internal value-bearing profile from the original sample."""
    return _build_profile(parts, args, redacted=False)


def _without_source_line_column(
    rows: list[dict[str, Any]],
    header_line_number: int,
) -> list[dict[str, Any]]:
    """Drop the prepended source-line cell from every sampled row.

    Keyed by the header fields, so the cell appears under whatever the header
    row carried in that position -- its name on physical line 1, its line
    number otherwise. Both forms are recognised by the one predicate.
    """
    return [
        {
            key: value for key, value in row.items()
            if not is_profile_excluded_column(key, header_line_number)
        }
        for row in rows
    ]


def _build_profile(
    parts: CsvParts,
    args: Any,
    *,
    redacted: bool,
    redacted_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Build one presentation of the shared CSV profile facts."""
    display_rows = parts.sampled_redacted if redacted else parts.sampled_original
    # The header's own physical line number. Sanitization prepends a
    # source_line_number cell to every record but only NAMES it on physical
    # line 1, so a file whose header sits below a preamble carries the number
    # there instead. Passing it lets the exclusion recognise that cell.
    header_line_number = parts.header_index + 1
    profile_header_fields = [
        field for field in parts.header_fields
        if not is_profile_excluded_column(field, header_line_number)
    ]
    profile_header_line = parts.delimiter.join(profile_header_fields)
    # The same cell has to leave the ROWS as well, not just the header.
    # profile_rows derives its columns from the row keys, and those keys are
    # the header fields -- so excluding it from one and not the other would
    # leave field_count and the field readings describing different widths, and
    # field_count is what completeness is counted against.
    profile_rows_input = _without_source_line_column(display_rows, header_line_number)
    base = profile_rows(
        profile_rows_input,
        parts.file_name,
        "delimited",
        redacted_columns=(
            redacted_columns
            if redacted_columns is not None
            else (parts.redact_columns if redacted else [])
        ),
        type_rows=_without_source_line_column(parts.sampled_original, header_line_number),
    )
    suffix = Path(parts.file_name).suffix
    # Delimiter and encoding are read instructions, so they are supplied to
    # enrichment and land in loader_hints alone rather than being restated here.
    base.update({
        "file_pattern": getattr(args, "feed_file_pattern", None)
        or (f"*{suffix}" if suffix else "*"),
        "header": profile_header_line,
        "detected_header": {
            "index": parts.header_index,
            "row_number": parts.header_index + 1,
            "line": profile_header_line,
            "ordered_columns": profile_header_fields,
        },
    })
    prof = enrich_csv_profile(
        base,
        display_rows,
        parts.sampled_original,
        source_file=parts.file_name,
        encoding=parts.encoding,
        delimiter=parts.delimiter,
        has_header=True,
        blank_line_count=parts.blank_line_count,
        ragged_row_count=parts.ragged_row_count,
        max_sample_values=effective_max_sample_values(args),
        feed=getattr(args, "feed", None),
        profile_scope=getattr(args, "profile_scope", "single_file"),
        source_files=getattr(args, "source_files", None) or [parts.file_name],
        file_count=getattr(args, "file_count", 1),
    )
    errors = validate_csv_profile(prof)
    if errors:
        raise ProfilingError(
            f"Generated CSV profile failed validation for '{parts.file_name}': "
            + " ".join(errors)
        )
    return prof


# ---------------------------------------------------------------------------
# Private — hint-aware reading
# ---------------------------------------------------------------------------


def _build_inspection_rows(
    source_file: str,
    source_read: CsvRead,
    redacted_data_lines: list[str],
    delimiter: str,
) -> tuple[InspectionRow, ...]:
    """Build persisted-safe inspection rows from the existing structural read.

    Every structural fact — parsed fields, header candidacy, blankness — comes
    from the read. Only redaction substitution and the File Operator region
    vocabulary are applied here.
    """

    redacted_by_line = dict(zip(source_read.data_line_numbers, redacted_data_lines))
    header_line_number = source_read.header_line_number
    rows: list[InspectionRow] = []
    for row in source_read.all_rows:
        text = redacted_by_line.get(row.physical_line_number, row.text)
        parsed_fields = tuple(parse_delimited_line(text, delimiter))
        header_identity = (
            sha256_text(
                json.dumps(
                    normalized_header(list(row.fields)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if row.is_header_candidate
            else None
        )
        if header_line_number is None or row.physical_line_number < header_line_number:
            region = "preamble"
        elif row.physical_line_number == header_line_number:
            region = "detected_header"
        else:
            region = "data"
        rows.append(
            InspectionRow(
                source_file=source_file,
                physical_line_number=row.physical_line_number,
                logical_row_number=row.physical_line_number,
                text=text,
                parsed_fields=parsed_fields,
                field_count=len(parsed_fields),
                line_length=len(row.text),
                delimiter_count=row.text.count(delimiter),
                parse_status="parsed",
                is_blank=row.is_blank,
                is_detected_header=row.physical_line_number == header_line_number,
                is_header_candidate=row.is_header_candidate,
                header_candidate_identity_sha256=header_identity,
                region=region,
            )
        )
    return tuple(rows)


def _detect_delimited_masks(
    header_fields: list[str],
    data_lines:    list[str],
    delimiter:     str,
) -> dict[str, str]:
    """Return a mask-type dict for every column by sampling raw data lines."""
    col_values: dict[str, list[str]] = {h: [] for h in header_fields}

    for line in data_lines[:SAMPLE_SIZE]:
        fields = parse_delimited_line(line, delimiter)
        for i, col in enumerate(header_fields):
            if i < len(fields) and fields[i].strip():
                col_values[col].append(fields[i].strip())

    return {col: detect_mask_type(vals) for col, vals in col_values.items()}


def _redact_delimited_lines(
    data_lines:    list[str],
    header_fields: list[str],
    redact_idx:    dict[int, str],
    registry:      RedactionRegistry,
    delimiter:     str,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Redact field values in raw delimited lines.

    Returns ``(redacted_lines, row_dicts, original_row_dicts)`` — redacted raw
    lines, redacted row dicts for samples, and original row dicts for type
    inference only.
    """
    redacted_lines: list[str]            = []
    row_dicts:      list[dict[str, Any]] = []
    original_rows:  list[dict[str, Any]] = []

    for line in data_lines:
        fields = parse_delimited_line(line, delimiter)
        row: dict[str, Any] = dict(zip(header_fields, fields))
        original_rows.append(dict(row))

        for i, col in redact_idx.items():
            if i < len(fields):
                replacement = registry.redact(col, fields[i])
                fields[i]   = replacement
                row[col]    = replacement

        redacted_lines.append(delimiter.join(fields))
        row_dicts.append(row)

    return redacted_lines, row_dicts, original_rows
