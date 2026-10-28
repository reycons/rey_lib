"""Line-preserving inspection and per-file-type analysis artifacts.

Delimited handlers supply already-read, already-redacted rows.  This module
only persists that evidence and prepares deterministic analysis inputs from
the authoritative File Operator file-type artifact.  It never reads source
files and does not invoke Analyzer or register workflow processes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rey_lib.encryption import sha256_bytes, sha256_file
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import log_artifact_reference
from rey_lib.profiling import build_structural_analysis

from rey_lib.files.error_utils import InputError

__all__ = [
    "InspectionRow",
    "write_inspection_artifact",
    "update_file_type_analysis_artifact",
]

_ANALYSIS_MANIFEST = "file_type_analysis_manifest.json"


@dataclass(frozen=True)
class InspectionRow:
    """One safe persisted row with stable source position."""

    source_file: str
    physical_line_number: int
    logical_row_number: int
    text: str
    parsed_fields: tuple[str, ...]
    field_count: int
    line_length: int
    delimiter_count: int
    parse_status: str
    is_blank: bool
    is_detected_header: bool
    is_header_candidate: bool
    header_candidate_identity_sha256: str | None
    region: str


def write_inspection_artifact(
    ctx: Any, run_log,
    output_dir: Path,
    *,
    source_file: str,
    source_text_sha256: str,
    delimiter: str,
    encoding: str,
    rows: tuple[InspectionRow, ...],
) -> Path:
    """Write one JSONL inspection artifact without reopening the source file."""

    output_path = Path(output_dir) / f"{Path(source_file).stem}.inspection.jsonl"
    if output_path.exists():
        existing_metadata, _, _ = _read_inspection_jsonl(output_path)
        existing_source = _required_string(
            existing_metadata,
            "source_file",
            output_path,
        )
        if existing_source != source_file:
            raise InputError(
                f"Inspection artifact collision at '{output_path}': existing "
                f"source is '{existing_source}', new source is '{source_file}'."
            )
    metadata = {
        "record_type": "inspection_metadata",
        "schema_version": 2,
        "source_file": source_file,
        "source_text_sha256": source_text_sha256,
        "delimiter": delimiter,
        "encoding": encoding,
        "row_count": len(rows),
        "physical_line_count": max(
            (row.physical_line_number for row in rows),
            default=0,
        ),
    }
    write_jsonl_file(
        output_path,
        [metadata, *({"record_type": "inspection_row", **asdict(row)} for row in rows)],
    )
    log_artifact_reference(run_log,
        str(output_path),
        role="inspection",
        producer="file_operator",
        artifact_group="profiles",
        producing_app="file_operator",
        artifact_type="inspection_jsonl",
        viewer_type="file",
        safe_to_preview=True,
        source_files=[source_file],
        row_count=len(rows),
        source_text_sha256=source_text_sha256,
    )
    return output_path


def update_file_type_analysis_artifact(
    ctx: Any, run_log,
    output_dir: Path,
    file_type_artifact_path: Path,
) -> Path:
    """Regenerate one analysis input from one authoritative file-type artifact."""

    output_dir = Path(output_dir)
    file_type_artifact = _read_json_object(
        file_type_artifact_path,
        label="file-type artifact",
    )
    file_type = _required_string(
        file_type_artifact,
        "file_type",
        file_type_artifact_path,
    )
    matching_files = _required_string_list(
        file_type_artifact,
        "matching_files",
        file_type_artifact_path,
    )
    inspection_artifacts = _required_string_list(
        file_type_artifact,
        "inspection_artifacts",
        file_type_artifact_path,
    )
    if len(inspection_artifacts) != len(matching_files):
        raise InputError(
            f"File-type artifact '{file_type_artifact_path}' must contain one "
            "inspection_artifact for every matching_file."
        )

    inspections_by_source: dict[str, dict[str, Any]] = {}
    for artifact_name in inspection_artifacts:
        inspection_path = output_dir / artifact_name
        metadata, rows, inspection_sha256 = _read_inspection_jsonl(inspection_path)
        source_file = _required_string(metadata, "source_file", inspection_path)
        if source_file in inspections_by_source:
            raise InputError(
                f"Duplicate inspection source '{source_file}' in file type "
                f"'{file_type}'."
            )
        inspections_by_source[source_file] = {
            "source_file": source_file,
            "inspection_artifact": artifact_name,
            "inspection_artifact_sha256": inspection_sha256,
            "source_text_sha256": _required_string(
                metadata,
                "source_text_sha256",
                inspection_path,
            ),
            "delimiter": _required_string(
                metadata,
                "delimiter",
                inspection_path,
            ),
            "encoding": _required_string(
                metadata,
                "encoding",
                inspection_path,
            ),
            "row_count": len(rows),
            "_rows": rows,
        }

    missing_sources = sorted(set(matching_files) - set(inspections_by_source))
    unexpected_sources = sorted(set(inspections_by_source) - set(matching_files))
    if missing_sources or unexpected_sources:
        raise InputError(
            f"File-type artifact '{file_type_artifact_path}' inspection coverage "
            f"does not match matching_files; missing={missing_sources}, "
            f"unexpected={unexpected_sources}."
        )

    analysis_path = output_dir / f"{file_type}.analysis.json"
    # The reduction and its rendering belong to rey_lib. Writing, digesting,
    # registering and logging stay here, because the manifest and the artifact
    # vocabulary are File Operator's.
    reduced = build_structural_analysis(
        file_type=file_type,
        file_type_artifact=file_type_artifact,
        file_type_artifact_name=file_type_artifact_path.name,
        matching_files=matching_files,
        inspection_artifacts=inspection_artifacts,
        inspections_by_source=inspections_by_source,
    )
    analysis, analysis_text = reduced.analysis, reduced.text
    atomic_write_text(analysis_path, analysis_text)
    analysis_bytes = analysis_text.encode("utf-8")
    analysis_sha256 = sha256_bytes(analysis_bytes)
    _update_analysis_manifest(
        output_dir,
        file_type=file_type,
        file_type_artifact=file_type_artifact_path.name,
        analysis_artifact=analysis_path.name,
        analysis_artifact_sha256=analysis_sha256,
        analysis_artifact_bytes=len(analysis_bytes),
        completeness=analysis["completeness"],
        matching_files=matching_files,
    )
    log_artifact_reference(run_log,
        str(analysis_path),
        role="file_type_analysis",
        producer="file_operator",
        artifact_group="profiles",
        producing_app="file_operator",
        artifact_type="file_type_analysis",
        viewer_type="file",
        safe_to_preview=True,
        file_type=file_type,
        source_files=matching_files,
        artifact_sha256=analysis_sha256,
        artifact_bytes=len(analysis_bytes),
    )
    return analysis_path


def _update_analysis_manifest(
    output_dir: Path,
    *,
    file_type: str,
    file_type_artifact: str,
    analysis_artifact: str,
    analysis_artifact_sha256: str,
    analysis_artifact_bytes: int,
    completeness: dict[str, Any],
    matching_files: list[str],
) -> None:
    manifest_path = output_dir / _ANALYSIS_MANIFEST
    if manifest_path.exists():
        manifest = _read_json_object(manifest_path, label="analysis manifest")
        if manifest.get("schema_version") != 1:
            raise InputError(
                f"Analysis manifest '{manifest_path}' has unsupported schema_version."
            )
        entries = manifest.get("file_types")
        if not isinstance(entries, list):
            raise InputError(
                f"Analysis manifest '{manifest_path}' field 'file_types' "
                "must be a list."
            )
    else:
        entries = []

    by_file_type: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("file_type"), str):
            raise InputError(
                f"Analysis manifest '{manifest_path}' contains an invalid entry."
            )
        by_file_type[entry["file_type"]] = entry
    by_file_type[file_type] = {
        "file_type": file_type,
        "file_type_artifact": file_type_artifact,
        "analysis_artifact": analysis_artifact,
        "analysis_artifact_sha256": analysis_artifact_sha256,
        "analysis_artifact_bytes": analysis_artifact_bytes,
        "bounded": bool(completeness.get("bounded")),
        "truncated": bool(completeness.get("truncated")),
        "pattern_records": dict(completeness.get("patterns") or {}),
        "ordered_context_records": dict(completeness.get("ordered_context") or {}),
        "matching_files": sorted(matching_files),
    }
    rendered = {
        "schema_version": 1,
        "file_types": [by_file_type[key] for key in sorted(by_file_type)],
    }
    atomic_write_text(
        manifest_path,
        json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _read_inspection_jsonl(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    # Reading JSONL belongs to the format primitive; what stays here is what
    # an inspection artifact specifically must contain. Absent and malformed
    # stay distinguishable, as the callers of this function report them
    # differently.
    if not Path(path).is_file():
        raise InputError(f"Cannot read inspection artifact '{path}': not a file.")
    try:
        records = [dict(item.record) for item in read_jsonl_file(path)]
    except JsonlReadError as exc:
        raise InputError(f"Cannot parse inspection artifact '{path}': {exc}") from exc
    except OSError as exc:
        raise InputError(f"Cannot read inspection artifact '{path}': {exc}") from exc
    if not records:
        raise InputError(f"Inspection artifact '{path}' is empty.")
    metadata = records[0]
    if metadata.get("record_type") != "inspection_metadata":
        raise InputError(
            f"Inspection artifact '{path}' must begin with inspection_metadata."
        )
    rows = records[1:]
    if any(row.get("record_type") != "inspection_row" for row in rows):
        raise InputError(
            f"Inspection artifact '{path}' contains a non-row record after metadata."
        )
    declared_count = metadata.get("row_count")
    if declared_count != len(rows):
        raise InputError(
            f"Inspection artifact '{path}' row_count does not match its records."
        )
    return metadata, rows, sha256_file(path)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(read_text_file(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read {label} '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label.title()} '{path}' must contain a JSON object.")
    return value


def _required_string(value: dict[str, Any], key: str, path: Path) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise InputError(f"Artifact '{path}' field '{key}' must be a string.")
    return result


def _required_string_list(
    value: dict[str, Any],
    key: str,
    path: Path,
) -> list[str]:
    result = value.get(key)
    if (
        not isinstance(result, list)
        or not result
        or not all(isinstance(item, str) and item for item in result)
    ):
        raise InputError(
            f"Artifact '{path}' field '{key}' must be a non-empty string list."
        )
    if len(set(result)) != len(result):
        raise InputError(f"Artifact '{path}' field '{key}' must be unique.")
    return sorted(result)
