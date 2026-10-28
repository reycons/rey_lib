"""Deterministic file-type artifacts for profiled delimited files.

The classifier consumes the ordered headers and profile already produced by
the existing File Operator pass. It never reads a source file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.files.file_utils import read_text_file
from rey_lib.files.primitive_file_io import atomic_write_text
from rey_lib.logs import AppLogger, log_artifact_reference
from rey_lib.profiling import normalized_header

from rey_lib.files.error_utils import InputError
from rey_lib.files.inspection import update_file_type_analysis_artifact

__all__ = ["classify_filenames", "update_file_type_artifact"]

_log = AppLogger(__name__)


def update_file_type_artifact(
    ctx: Any, run_log,
    output_dir: Path,
    *,
    header_fields: list[str],
    header_index: int,
    header_line: str,
    source_files: list[str],
    profile: dict[str, Any],
    profile_artifacts: list[str],
    inspection_artifacts: list[str] | None = None,
    filename_regex: str | None = None,
) -> Path:
    """Create or update the file-type artifact for one ordered CSV layout."""
    classifications = classify_filenames(source_files, filename_regex)
    normalized = normalized_header(header_fields)
    signature = sha256_text(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    )
    file_type = f"file_type_{signature[:16]}"
    artifact_path = Path(output_dir) / f"{file_type}.json"

    existing = _load_existing(artifact_path)
    created = existing is None
    if existing is not None:
        if (
            existing.get("signature") != signature
            or existing.get("normalized_headers") != normalized
        ):
            raise InputError(
                f"File-type artifact identity mismatch: '{artifact_path}'."
            )
        artifact = existing
    else:
        artifact = {
            "file_type": file_type,
            "signature": signature,
            "layout": "delimited",
            "ordered_headers": list(header_fields),
            "normalized_headers": normalized,
            "detected_header": {
                "index": header_index,
                "row_number": header_index + 1,
                "line": header_line,
            },
            "matching_files": [],
            "profile_artifacts": [],
            "columns": list(profile.get("columns") or []),
        }

    artifact["matching_files"] = sorted(
        set(artifact.get("matching_files") or []).union(source_files)
    )
    artifact["profile_artifacts"] = sorted(
        set(artifact.get("profile_artifacts") or []).union(profile_artifacts)
    )
    if inspection_artifacts is not None:
        artifact["inspection_artifacts"] = sorted(
            set(artifact.get("inspection_artifacts") or []).union(
                inspection_artifacts
            )
        )
    if filename_regex is not None:
        artifact["filename_classifications"] = _merge_filename_classifications(
            artifact.get("filename_classifications"),
            classifications,
        )

    rendered = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(artifact_path, rendered + "\n")

    action = "created" if created else "matched"
    _log.info(
        "file type %s — type=%s files=%s artifact=%s",
        action,
        file_type,
        sorted(source_files),
        artifact_path,
    )
    log_artifact_reference(run_log,
        str(artifact_path),
        role="file_type",
        producer="data_engine",
        artifact_group="profiles",
        producing_app="data_engine",
        artifact_type="file_type",
        viewer_type="file",
        safe_to_preview=True,
        source_files=sorted(source_files),
        classification_action=action,
        file_type=file_type,
    )
    if inspection_artifacts is not None:
        update_file_type_analysis_artifact(ctx, run_log, output_dir, artifact_path)
    return artifact_path


def classify_filenames(
    source_files: list[str],
    filename_regex: str | None,
) -> list[dict[str, Any]]:
    """Extract generic named captures from source basenames."""
    if filename_regex is None:
        return []
    try:
        compiled = re.compile(filename_regex)
    except re.error as exc:
        raise InputError(f"Invalid configured filename classification regex: {exc}") from exc

    classifications: list[dict[str, Any]] = []
    for filename in sorted(set(source_files)):
        match = compiled.fullmatch(filename)
        if match is None:
            raise InputError(
                f"Filename classification failed: '{filename}' does not match "
                "the configured classification.filename_regex."
            )
        classifications.append({
            "filename": filename,
            "values": match.groupdict(),
        })
    return classifications


def _merge_filename_classifications(
    existing: object,
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge classification entries by filename with deterministic ordering."""
    by_filename: dict[str, dict[str, Any]] = {}
    if isinstance(existing, list):
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("filename")
            values = entry.get("values")
            if isinstance(filename, str) and isinstance(values, dict):
                by_filename[filename] = {
                    "filename": filename,
                    "values": dict(values),
                }
    for entry in additions:
        by_filename[entry["filename"]] = entry
    return [by_filename[filename] for filename in sorted(by_filename)]


def _load_existing(path: Path) -> dict[str, Any] | None:
    """Load an existing file-type artifact, rejecting invalid JSON/shapes."""
    if not path.exists():
        return None
    try:
        value = json.loads(read_text_file(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read file-type artifact '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"File-type artifact '{path}' must contain a JSON object.")
    return value
