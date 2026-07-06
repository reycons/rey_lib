"""
Generic file I/O utilities.

Provides readers and writers for CSV and XLSX formats, output path
construction, and file listing. All functions are format-agnostic and
have no knowledge of any application's data model or transformation logic.

Row filtering is the caller's responsibility — an optional callable can be
passed to get_reader() and is applied per row. This module never imports
application-specific modules.

Public API
----------
input_files(folder, pattern)
    Return sorted list of files matching a glob pattern.
converted_output_path(base_dir, filename_pattern, substitutions)
    Construct an output path by substituting tokens into a filename pattern.
get_reader(infile, file_type, encoding, row_filter)
    Return a row iterator for the given file type.
write_file(outfile, content, file_type)
    Write content to a file and log the creation to the file-operation state.
move_file(src, dest_dir)
    Move a file to a destination directory, creating dest_dir if needed.    
"""

from __future__ import annotations

import csv
from fnmatch import fnmatch
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime
from datetime import timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Iterator, Optional, TextIO

from rey_lib.logs import get_logger

__all__ = [
    "bounded_text_preview",
    "bytes_sha256",
    "discover_inbox_files",
    "folder_children",
    "input_files",
    "input_tree_files",
    "is_hidden_path",
    "file_sha256",
    "matched_tree_files",
    "matches_file_pattern",
    "move_to_failed",
    "move_to_processing",
    "move_to_stage",
    "move_to_success",
    "pattern_to_glob",
    "converted_output_path",
    "run_artifact_path",
    "get_reader",
    "write_file",
    "append_jsonl",
    "delete_file",
    "export_db_root",
    "export_object_file_path",
    "export_build_manifest_path",
    "export_build_sql_path",
    "export_relative_posix",
    "cleanup_stale_files",
    "scan_column_lengths",
    "move_file",
    "copy_file",
    "file_operation_log_path",
    "file_movement_log_path",
    "find_named_files",
    "find_original_relative_path",
    "iter_file_operations",
    "iter_file_movements",
    "log_file_operation",
    "log_file_move",
    "open_text_file",
    "apply_file_movements",
    "resolve_safe_file",
    "read_text_file",
    "read_bytes_file",
    "visible_files",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def input_files(
    folder: Path,
    pattern: str | Iterable[str],
    *,
    recursive: bool = False,
) -> list[Path]:
    """
    Return a sorted list of files matching one or more glob patterns in folder.

    Parameters
    ----------
    folder : Path
        Directory to scan.
    pattern : str | Iterable[str]
        Glob pattern or patterns (e.g. '*.csv' or ['*.ps1', '*.tr1']).
        Any ``{token}`` placeholders are converted to ``*`` before globbing,
        matching rey_loader transform-file behavior.
    recursive : bool
        When true, patterns are matched recursively under folder.

    Returns
    -------
    list[Path]
        Sorted list of matching file paths. Empty list if folder does
        not exist or no files match.
    """
    folder = Path(folder)
    if not folder.exists():
        _logger.debug("input_files: folder does not exist: %s", folder)
        return []

    matches: dict[str, Path] = {}
    for glob_pattern in _coerce_glob_patterns(pattern):
        iterator = folder.rglob(glob_pattern) if recursive else folder.glob(glob_pattern)
        for file_path in iterator:
            if file_path.is_file():
                matches[str(file_path)] = file_path
    return sorted(matches.values())


def visible_files(
    folder: Path,
    pattern: str | Iterable[str] = "*",
    *,
    recursive: bool = True,
) -> list[Path]:
    """Return non-hidden files in ``folder`` matching one or more glob patterns."""
    root = Path(folder)
    if not root.exists():
        _logger.debug("visible_files: folder does not exist: %s", root)
        return []

    return [
        path
        for path in input_files(root, pattern, recursive=recursive)
        if not is_hidden_path(path, root)
    ]


def input_tree_files(
    folder: Path,
    *,
    skip_suffixes: Iterable[str] = (".yaml", ".yml"),
) -> list[Path]:
    """Return non-hidden input files recursively under ``folder``."""
    root = Path(folder)
    if not root.exists():
        _logger.debug("input_tree_files: folder does not exist: %s", root)
        return []
    suffixes = {suffix.lower() for suffix in skip_suffixes}
    return sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and not is_hidden_path(path, root)
        and path.suffix.lower() not in suffixes
    )


def matched_tree_files(
    folder: Path,
    pattern: str | Iterable[str],
    *,
    base_dir: Path | None = None,
    skip_suffixes: Iterable[str] = (".yaml", ".yml"),
) -> list[Path]:
    """Return recursive non-hidden files matching configured file patterns."""
    match_base = Path(base_dir) if base_dir is not None else Path(folder)
    return [
        file_path
        for file_path in input_tree_files(folder, skip_suffixes=skip_suffixes)
        if matches_file_pattern(file_path, pattern, match_base)
    ]


def find_named_files(folder: Path, filename: str) -> list[Path]:
    """Return recursive non-hidden files with the exact filename."""
    root = Path(folder)
    if not root.exists():
        _logger.debug("find_named_files: folder does not exist: %s", root)
        return []
    return [
        path
        for path in sorted(root.rglob(filename))
        if path.is_file() and not is_hidden_path(path, root)
    ]


def file_sha256(path: Path | str) -> str:
    """Return the SHA-256 hex digest for a file."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def bytes_sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def read_text_file(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    """Read a text file through the shared file utility boundary."""
    return Path(path).read_text(encoding=encoding, errors=errors)


def read_bytes_file(path: Path | str) -> bytes:
    """Read raw file bytes through the shared file utility boundary."""
    return Path(path).read_bytes()


def open_text_file(
    path: Path | str,
    mode: str = "r",
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
) -> TextIO:
    """Open a text file through the shared file utility boundary."""
    file_path = Path(path).expanduser()
    if any(flag in mode for flag in ("a", "w", "x", "+")):
        file_path.parent.mkdir(parents=True, exist_ok=True)
    if errors is None:
        return file_path.open(mode, encoding=encoding)
    return file_path.open(mode, encoding=encoding, errors=errors)


def is_hidden_path(path: Path, root_path: Path) -> bool:
    """Return true when ``path`` has hidden relative path segments."""
    return any(part.startswith(".") for part in Path(path).relative_to(root_path).parts)


def folder_children(path: Path, root_path: Path | None = None) -> list[dict[str, Any]]:
    """Return a recursive non-hidden folder tree for display or inspection."""
    root = Path(root_path) if root_path is not None else Path(path)
    children: list[dict[str, Any]] = []

    children_iter = sorted(
        Path(path).iterdir(),
        key=lambda item: (item.is_file(), item.name.lower()),
    )
    for child in children_iter:
        if child.name.startswith("."):
            continue

        relative_path = child.relative_to(root).as_posix()
        if child.is_dir():
            children.append(
                {
                    "type": "directory",
                    "name": child.name,
                    "path": str(child),
                    "relative_path": relative_path,
                    "file_count": len(visible_files(child)),
                    "children": folder_children(child, root),
                }
            )
        elif child.is_file():
            children.append(
                {
                    "type": "file",
                    "name": child.name,
                    "path": str(child),
                    "relative_path": relative_path,
                }
            )

    return children


def resolve_safe_file(raw_path: Path | str, root_path: Path | str) -> Path:
    """Resolve a file path and require it to live under ``root_path``."""
    root = Path(root_path).expanduser().resolve()
    path = Path(raw_path).expanduser().resolve()

    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path is outside root: {path}") from exc

    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    return path


def bounded_text_preview(
    path: Path | str,
    max_bytes: int,
    *,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Return a bounded text preview for one file."""
    file_path = Path(path)
    data = file_path.read_bytes()
    truncated = len(data) > max_bytes
    content = data[:max_bytes].decode(encoding, errors="replace")

    return {
        "name": file_path.name,
        "path": str(file_path),
        "size_bytes": len(data),
        "truncated": truncated,
        "content": content,
    }


def pattern_to_glob(file_pattern: str) -> str:
    """
    Convert a configured file pattern with tokens to a filesystem glob.

    Examples
    --------
    ``tran_{yyyymmdd}.csv`` becomes ``tran_*.csv``.
    ``*.csv`` remains ``*.csv``.
    """
    return re.sub(r"\{[^}]+\}", "*", file_pattern)


def matches_file_pattern(
    file_path: Path,
    pattern: str | Iterable[str],
    base_dir: Path | None = None,
) -> bool:
    """Return true when ``file_path`` matches one configured file pattern."""
    names = [file_path.name]
    if base_dir is not None:
        try:
            names.append(file_path.relative_to(base_dir).as_posix())
        except ValueError:
            pass

    for glob_pattern in _coerce_glob_patterns(pattern):
        if any(fnmatch(name, glob_pattern) for name in names):
            return True
    return False


def discover_inbox_files(source_cfg: Any) -> list[Path]:
    """Return files in ``source_cfg.paths.inbox_path`` matching ``file_pattern``."""
    inbox = Path(source_cfg.paths.inbox_path).expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    pattern = getattr(source_cfg, "file_pattern", "*")
    return input_files(inbox, pattern)


def move_to_processing(
    file_path: Path,
    source_cfg: Any,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: "str | None" = None,
) -> Path:
    """Move ``file_path`` to ``source_cfg.paths.processing_path``."""
    return move_to_stage(file_path, source_cfg, "processing", state_ctx=state_ctx, app=app, pipeline=pipeline)


def move_to_success(
    file_path: Path,
    source_cfg: Any,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: "str | None" = None,
) -> Path:
    """Move ``file_path`` to ``source_cfg.paths.success_path``."""
    return move_to_stage(file_path, source_cfg, "success", state_ctx=state_ctx, app=app, pipeline=pipeline)


def move_to_failed(
    file_path: Path,
    source_cfg: Any,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: "str | None" = None,
) -> Path:
    """Move ``file_path`` to ``source_cfg.paths.failed_path``."""
    return move_to_stage(file_path, source_cfg, "failed", state_ctx=state_ctx, app=app, pipeline=pipeline)


def move_to_stage(
    file_path: Path,
    source_cfg: Any,
    stage: str,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: "str | None" = None,
) -> Path:
    """Move ``file_path`` to a configured stage path on ``source_cfg.paths``."""
    dest_dir = Path(getattr(source_cfg.paths, f"{stage}_path")).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    return move_file(file_path, dest_dir, state_ctx=state_ctx, app=app, pipeline=pipeline, reason=stage)


def _coerce_glob_patterns(pattern: str | Iterable[str]) -> list[str]:
    """Normalize one or more configured file patterns to glob strings."""
    if isinstance(pattern, str):
        raw_patterns = [pattern]
    else:
        raw_patterns = [str(item) for item in pattern]

    glob_patterns = {
        pattern_to_glob(item.strip())
        for item in raw_patterns
        if item and item.strip()
    }
    return sorted(glob_patterns or {"*"})


def converted_output_path(
    base_dir: Path,
    filename_pattern: str,
    substitutions: dict[str, str],
) -> Path:
    """
    Construct a full output path by substituting tokens into a filename pattern.

    Parameters
    ----------
    base_dir : Path
        Directory where the output file will be written.
    filename_pattern : str
        Filename pattern string with {token} placeholders,
        e.g. '{source}_{name}_{start_date}_{end_date}.csv'.
    substitutions : dict[str, str]
        Token values to substitute into the pattern.

    Returns
    -------
    Path
        Absolute path for the output file.
    """
    filename = filename_pattern.format(**substitutions)
    return Path(base_dir) / filename


def run_artifact_path(
    base_dir: Path | str,
    artifact_name: str,
    run_timestamp: str,
    extension: str,
) -> Path:
    """
    Construct the path for a run-created artifact (SGC_Rey_Run_ID_Standard).

    This is the single, centralized place run-artifact filenames are built. Every
    run-created artifact embeds the run timestamp immediately before the extension,
    following the universal pattern ``<artifact_name>.<run_timestamp>.<extension>``
    (e.g. ``run_log.20260706_091845.jsonl``). The append-only run log is not an
    exception to this rule.

    Collision handling: a previous run is never silently overwritten. If a file with
    the same run_timestamp already exists, a short filename-safe suffix is inserted
    after the run_timestamp segment.

    Parameters
    ----------
    base_dir : Path | str
        Directory the artifact is written to (created by the caller).
    artifact_name : str
        Base artifact name, e.g. ``"run_log"`` or ``"rey_loader.run_log"``.
    run_timestamp : str
        Filename-safe run timestamp (``YYYYMMDD_HHMMSS``) from runtime context.
    extension : str
        File extension, with or without a leading dot (e.g. ``"jsonl"``, ``".md"``).

    Returns
    -------
    Path
        Absolute artifact path, with a short suffix appended to the run_timestamp
        segment only when a same-timestamp file already exists.
    """
    directory = Path(base_dir)
    ext = extension.lstrip(".")
    candidate = directory / f"{artifact_name}.{run_timestamp}.{ext}"
    if not candidate.exists():
        return candidate.resolve()

    # Collision: append a short, filename-safe suffix rather than overwrite.
    suffix = uuid.uuid4().hex[:4]
    collided = directory / f"{artifact_name}.{run_timestamp}_{suffix}.{ext}"
    return collided.resolve()


def get_reader(
    infile: Path,
    file_type: str = "CSV",
    encoding: str = "utf-8-sig",
    row_filter: Optional[Callable[[dict[str, str]], bool]] = None,
    header_line: Optional[str] = None,
    delimiter: str = ",",
) -> Generator[dict[str, str], None, None]:
    """
    Return a row iterator for the given file based on file_type.

    Rows for which row_filter returns False are excluded. If row_filter
    is None, all rows are yielded.

    Parameters
    ----------
    infile : Path
        Source file to read.
    file_type : str
        File format — 'CSV' or 'XLSX'. Case-insensitive.
    encoding : str
        Character encoding for CSV files. Defaults to 'utf-8-sig' to
        handle BOM-prefixed files from Windows applications.
    row_filter : Optional[Callable[[dict[str, str]], bool]]
        Optional predicate. Called with each raw row dict; rows where
        this returns False are skipped. None means no filtering.
    header_line : Optional[str]
        Exact header line to locate before reading rows. When provided,
        CSV reading begins only after this header is found in the file.
    delimiter : str
        Field delimiter character. Defaults to ','.

    Yields
    ------
    dict[str, str]
        One raw row dict per data row.

    Raises
    ------
    ValueError
        If file_type is not 'CSV' or 'XLSX'.
    """
    _DELIMITED = {"CSV", "DELIMITED_HEADER", "DELIMITED_NO_HEADER"}

    fmt = file_type.upper()
    if fmt in _DELIMITED:
        yield from _csv_reader(
            infile,
            encoding=encoding,
            row_filter=row_filter,
            header_line=header_line,
            delimiter=delimiter,
        )
    elif fmt == "XLSX":
        yield from _xlsx_reader(infile, row_filter=row_filter)
    else:
        raise ValueError(f"Unsupported file_type '{file_type}'.")


def write_file(
    outfile: Path,
    content: Any,
    file_type: str = "CSV",
    *,
    sort_keys: bool = False,
    state_ctx: Any = None,
    app: str = "",
    pipeline: "str | None" = None,
    reason: str = "",
) -> Path:
    """
    Write content to a file and log the creation to the file-operation state.

    Parameters
    ----------
    outfile : Path
        Destination file path. Parent directories are created if needed.
    content : Any
        Content to write. ``list[dict]`` for CSV/XLSX; ``str`` for TEXT;
        any JSON-serialisable value for JSON.
    file_type : str
        Output format — 'CSV', 'XLSX', 'TEXT', or 'JSON'. Case-insensitive.
    sort_keys : bool
        For JSON output, sort object keys for deterministic files (e.g. state
        files). Ignored for other formats.
    state_ctx : Any
        Optional context with ``config_root``. When provided, a creation
        record is appended to the file-operation state log after the file is written.
    app : str
        App name stamped on the state record.
    pipeline : str | None
        Pipeline name stamped on the state record.
    reason : str
        Reason label stamped on the state record.

    Returns
    -------
    Path
        Absolute path of the written file.

    Raises
    ------
    ValueError
        If file_type is unsupported or content is empty for tabular formats.
    """
    outfile = Path(outfile)
    fmt = file_type.upper()

    if fmt in ("CSV", "XLSX"):
        if not content:
            raise ValueError("write_file called with empty rows list.")
        if fmt == "CSV":
            _csv_writer(outfile, content)
        else:
            _xlsx_writer(outfile, content)
    elif fmt == "TEXT":
        outfile.parent.mkdir(parents=True, exist_ok=True)
        outfile.write_text(str(content), encoding="utf-8")
    elif fmt == "JSON":
        outfile.parent.mkdir(parents=True, exist_ok=True)
        outfile.write_text(
            json.dumps(content, default=str, indent=2, sort_keys=sort_keys),
            encoding="utf-8",
        )
    else:
        raise ValueError(f"Unsupported file_type '{file_type}'. Must be CSV, XLSX, TEXT, or JSON.")

    if state_ctx is not None:
        try:
            log_file_operation(
                state_ctx,
                app=app,
                pipeline=pipeline,
                source=outfile,
                destination=outfile,
                action="create",
                reason=reason,
                original_source=outfile,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Could not write file creation state for '%s': %s", outfile.name, exc)

    return outfile.resolve()


def append_jsonl(path: Path | str, record: dict[str, Any]) -> Path:
    """
    Append one record as a JSON line to a JSONL file.

    The single, centralized place JSONL/run-log records are appended
    (SGC_Rey_System_File_Creation_Standard), so no subsystem opens JSONL files
    directly. Parent directories are created as needed.

    Parameters
    ----------
    path : Path | str
        Destination JSONL file.
    record : dict[str, Any]
        One record, serialised as a single JSON line.

    Returns
    -------
    Path
        The JSONL file path.
    """
    outfile = Path(path)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    with outfile.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return outfile


def delete_file(path: Path | str) -> bool:
    """Delete one file if it exists and return whether anything was removed."""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return False
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")
    file_path.unlink()
    return True


def export_db_root(output_root: Path | str, provider: str, database: str) -> Path:
    """Return export root folder for one provider/database pair."""
    return Path(output_root).expanduser() / provider / database


def export_object_file_path(
    db_root: Path | str,
    schema: str,
    object_type: str,
    file_name: str,
) -> Path:
    """Return full object SQL file path under export root."""
    return Path(db_root) / schema / object_type / file_name


def export_build_manifest_path(db_root: Path | str) -> Path:
    """Return build manifest path under export root."""
    return Path(db_root) / "build" / "build_manifest.json"


def export_build_sql_path(db_root: Path | str) -> Path:
    """Return build SQL path under export root."""
    return Path(db_root) / "build" / "build_database.sql"


def export_relative_posix(path: Path | str, root: Path | str) -> str:
    """Return path relative to root in forward-slash format."""
    return Path(path).relative_to(Path(root)).as_posix()


def cleanup_stale_files(
    root: Path | str,
    keep_files: set[Path | str],
) -> list[str]:
    """Delete files under root that are not in keep_files and return removed relpaths."""
    root_path = Path(root)
    keep_resolved = {Path(path).resolve() for path in keep_files}
    removed: list[str] = []

    for existing in visible_files(root_path, "*", recursive=True):
        if not existing.is_file():
            continue
        if existing.resolve() in keep_resolved:
            continue
        if delete_file(existing):
            removed.append(existing.relative_to(root_path).as_posix())

    return removed

def move_file(
    src: Path,
    dest_dir: Path,
    dest_name: Optional[str] = None,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: str | None = None,
    reason: str = "",
    original_source: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """
    Move a file to a destination directory.

    Creates the destination directory if it does not exist. If a file
    with the same name already exists in dest_dir it is overwritten.

    Parameters
    ----------
    src : Path
        Full path of the file to move.
    dest_dir : Path
        Destination directory. Created if it does not exist.
    dest_name : Optional[str]
        Destination filename. If None, keeps src.name.
    state_ctx : Any
        Optional context with ``config_root``. When provided, a file-operation
        record is appended after the move succeeds.

    Returns
    -------
    Path
        Full path of the file in its new location.

    Raises
    ------
    FileNotFoundError
        If src does not exist.
    OSError
        If the move fails for any reason.
    """
    src      = Path(src)
    dest_dir = Path(dest_dir)

    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (dest_name if dest_name else src.name)
    src.replace(dest)
    _logger.debug("Moved: %s → %s", src, dest)
    if state_ctx is not None:
        try:
            log_file_operation(
                state_ctx,
                app=app,
                pipeline=pipeline,
                source=src,
                destination=dest,
                reason=reason,
                original_source=original_source,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Could not write file movement state for '%s': %s", src.name, exc)
    return dest


def copy_file(
    src: Path,
    dest_dir: Path,
    dest_name: Optional[str] = None,
    *,
    state_ctx: Any = None,
    app: str = "",
    pipeline: str | None = None,
    reason: str = "",
    original_source: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """
    Copy a file byte-for-byte to a destination directory.

    Creates the destination directory if it does not exist and overwrites any
    existing file of the same name. The copy is byte-identical to the source —
    content, delimiter, quoting, encoding, blank lines, line endings, and
    whitespace are all preserved; no parsing or re-serialisation occurs.

    Parameters mirror :func:`move_file`. When ``state_ctx`` is provided a
    file-operation record is appended after the copy succeeds.

    Parameters
    ----------
    src : Path
        Full path of the file to copy.
    dest_dir : Path
        Destination directory. Created if it does not exist.
    dest_name : Optional[str]
        Destination filename. If None, keeps src.name.
    state_ctx : Any
        Optional context for the file-operation record.

    Returns
    -------
    Path
        Full path of the copied file.

    Raises
    ------
    FileNotFoundError
        If src does not exist.
    OSError
        If the copy fails for any reason.
    """
    src      = Path(src)
    dest_dir = Path(dest_dir)

    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (dest_name if dest_name else src.name)
    shutil.copyfile(src, dest)
    _logger.debug("Copied: %s → %s", src, dest)
    if state_ctx is not None:
        try:
            log_file_operation(
                state_ctx,
                app=app,
                pipeline=pipeline,
                source=src,
                destination=dest,
                reason=reason,
                original_source=original_source,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Could not write file copy state for '%s': %s", src.name, exc)
    return dest


def file_operation_log_path(ctx: Any) -> Path:
    """Return the configured file-operation JSONL path for ``ctx``."""
    paths = getattr(ctx, "paths", None)
    if hasattr(paths, "resolve"):
        return paths.resolve("file_operations_state")
    raise ValueError(
        "ctx.paths is required — build ctx with build_ctx_from_path."
    )


def file_movement_log_path(ctx: Any) -> Path:
    """Compatibility alias for the configured file-operation JSONL path."""
    return file_operation_log_path(ctx)


def log_file_operation(
    ctx: Any,
    *,
    source: Path | str,
    destination: Path | str,
    app: str = "",
    pipeline: str | None = None,
    action: str = "move",
    reason: str = "",
    original_source: Path | str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one successful file operation event to the configured JSONL state."""
    src = Path(source).expanduser()
    dest = Path(destination).expanduser()
    original = Path(original_source).expanduser() if original_source else src
    environment_root = _display_root(ctx)

    record: dict[str, Any] = {
        "operation_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app": app,
        "pipeline": pipeline or getattr(ctx, "pipeline_name", None),
        "operation": action,
        "action": action,
        "reason": reason,
        "source": _display_path(src, environment_root),
        "destination": _display_path(dest, environment_root),
        "original_source": _display_path(original, environment_root),
        "source_abs": str(src.resolve()),
        "destination_abs": str(dest.resolve()),
        "original_source_abs": str(original.resolve()),
        "file_fingerprint": _file_operation_fingerprint(dest if dest.exists() else src),
    }
    if run_id:
        record["run_id"] = run_id
    if metadata:
        record["metadata"] = metadata

    path = file_operation_log_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return record


def log_file_move(
    ctx: Any,
    *,
    source: Path | str,
    destination: Path | str,
    app: str = "",
    pipeline: str | None = None,
    action: str = "move",
    reason: str = "",
    original_source: Path | str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility alias for ``log_file_operation``."""
    return log_file_operation(
        ctx,
        source=source,
        destination=destination,
        app=app,
        pipeline=pipeline,
        action=action,
        reason=reason,
        original_source=original_source,
        run_id=run_id,
        metadata=metadata,
    )


def iter_file_operations(ctx: Any) -> Iterator[dict[str, Any]]:
    """Yield file-operation state records, skipping blank or malformed lines."""
    path = file_operation_log_path(ctx)
    if not path.exists():
        return

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                if "operation" not in record and "action" in record:
                    record["operation"] = record["action"]
                yield record


def iter_file_movements(ctx: Any) -> Iterator[dict[str, Any]]:
    """Compatibility alias for file-operation state records."""
    yield from iter_file_operations(ctx)


def _file_operation_fingerprint(path: Path) -> dict[str, Any]:
    """Return a stable fingerprint for a file-operation target when available."""
    file_path = Path(path).expanduser()
    fingerprint: dict[str, Any] = {
        "name": file_path.name,
        "exists": file_path.exists(),
    }
    if not file_path.is_file():
        return fingerprint

    stat = file_path.stat()
    fingerprint.update(
        {
            "size_bytes": stat.st_size,
            "sha256": file_sha256(file_path),
        }
    )
    return fingerprint


def find_original_relative_path(ctx: Any, *, pipeline: str, file_name: str) -> Path | None:
    """Find the latest original inbox-relative path for ``file_name``."""
    suffix = f"data/pipelines/{pipeline}/inbox/"
    found: Path | None = None

    for record in iter_file_movements(ctx):
        if record.get("pipeline") not in {pipeline, None, ""}:
            continue
        for key in ("original_source", "original_source_abs", "source", "source_abs"):
            raw = str(record.get(key, ""))
            if not raw.endswith(file_name):
                continue
            normalized = raw.replace("\\", "/")
            if suffix not in normalized:
                continue
            relative = normalized.split(suffix, 1)[1]
            if relative and "/" in relative:
                found = Path(relative)

    return found


def apply_file_movements(paths: Any, file_movements: Any) -> int:
    """Apply file movement rules for one data source.

    This pipeline is independent from transform/load and is intended for
    pre-transform intake (e.g. downloads folder -> inbox folder).

    Supported name transforms:
        - date_range_from_column

    Parameters
    ----------
    paths : Any
        Data-source paths namespace.
    file_movements : Any
        file_movements namespace containing filename_pattern, name_transforms,
        and success movement instructions.

    Returns
    -------
    int
        Number of files moved.

    Raises
    ------
    ValueError
        If required movement config is missing or invalid.
    """
    if file_movements is None:
        return 0

    pattern: str = getattr(file_movements, "filename_pattern", "*")
    success: Any = getattr(file_movements, "success", None)
    if not success:
        return 0

    first_move = getattr(success[0], "move", None)
    if first_move is None:
        raise ValueError("file_movements.success[0].move is required.")

    source_key = getattr(first_move, "from", None)
    dest_key = getattr(first_move, "to", None)
    if source_key is None or dest_key is None:
        raise ValueError("file_movements.success[0].move requires 'from' and 'to'.")

    source_dir = _resolve_path_key(paths, source_key)
    dest_dir = _resolve_path_key(paths, dest_key)
    files = input_files(source_dir, pattern)

    if not files:
        return 0

    name_transforms: list[Any] = list(getattr(file_movements, "name_transforms", []) or [])

    moved = 0
    for file_path in files:
        dest_name = _resolve_dest_name(file_path, name_transforms)
        move_file(file_path, dest_dir, dest_name=dest_name)
        moved += 1

    return moved


def scan_column_lengths(
    files: list[Path],
    file_type: str = "CSV",
    encoding: str = "utf-8-sig",
) -> dict[str, int]:
    """
    Scan a list of files and return the maximum observed length per column.

    Reads every row in every file and tracks the longest value seen for
    each column name. Used to size VARCHAR columns when auto-creating a
    staging table — caller adds a buffer before passing to DDL.

    Returns an empty dict if files is empty or no rows are found.

    Parameters
    ----------
    files : list[Path]
        Files to scan. All must share the same column structure.
    file_type : str
        File format — 'CSV' or 'XLSX'. Case-insensitive.
    encoding : str
        Character encoding for CSV files.

    Returns
    -------
    dict[str, int]
        Mapping of column name → maximum observed value length in characters.
        Columns with all-blank values have length 0.
    """
    max_lengths: dict[str, int] = {}

    for file_path in files:
        for row in get_reader(file_path, file_type=file_type, encoding=encoding):
            for col, val in row.items():
                length = len(val) if val else 0
                if col not in max_lengths or length > max_lengths[col]:
                    max_lengths[col] = length

    return max_lengths

# ---------------------------------------------------------------------------
# Private — CSV reader / writer
# ---------------------------------------------------------------------------

def _csv_reader(
    infile: Path,
    encoding: str,
    row_filter: Optional[Callable[[dict[str, str]], bool]],
    header_line: Optional[str],
    delimiter: str = ",",
) -> Generator[dict[str, str], None, None]:
    """
    Yield data rows from a CSV file.

    Skips blank lines and mid-file header-repeat rows. Applies row_filter
    when provided. The file handle is managed via a context manager.
    """
    with infile.open(newline="", encoding=encoding, errors="replace") as fh:
        header = ""
        if header_line is None:
            # Skip blank lines to find the first non-empty header line.
            for line in fh:
                stripped = line.strip()
                if stripped:
                    header = stripped
                    break
        else:
            # Scan until the matched header line is found.
            for line in fh:
                stripped = line.strip()
                if stripped == header_line:
                    header = stripped
                    break

        if not header:
            return

        fieldnames = [c.strip() for c in header.split(delimiter)]
        reader     = csv.DictReader(fh, fieldnames=fieldnames, delimiter=delimiter)

        for row in reader:
            # Skip blank rows, including rows that contain only whitespace.
            if not any((v or "").strip() for v in row.values()):
                continue

            # Skip repeated header rows embedded mid-file.
            first_col = fieldnames[0] if fieldnames else ""
            if row.get(first_col, "").strip() == first_col:
                continue

            if row_filter is not None and not row_filter(row):
                continue

            yield row


def _resolve_path_key(paths: Any, key: str) -> Path:
    """Resolve a named path key from a data-source paths namespace."""
    value = getattr(paths, key, None)
    if value is None:
        raise ValueError(f"Path key '{key}' not found in data source paths.")
    return Path(value)


def _resolve_dest_name(file_path: Path, name_transforms: list[Any]) -> str:
    """Resolve final destination filename after applying name transforms."""
    if not name_transforms:
        return file_path.name

    transform = name_transforms[0]
    transform_type: str = getattr(transform, "type", "")

    if transform_type == "date_range_from_column":
        return _apply_date_range_from_column(file_path, transform)

    raise ValueError(f"Unsupported name_transform type: '{transform_type}'.")


def _apply_date_range_from_column(file_path: Path, transform: Any) -> str:
    """Create destination filename using min/max date values from one CSV column."""
    source_column: str = getattr(transform, "source_column")
    date_format: str = getattr(transform, "date_format")
    output_template: str = getattr(transform, "output_template")
    output_format: str = getattr(transform, "output_format")

    start_date, end_date = _read_date_range_from_column(
        file_path=file_path,
        source_column=source_column,
        date_format=date_format,
    )
    return output_template.format(
        start_date=start_date.strftime(output_format),
        end_date=end_date.strftime(output_format),
    )


def _read_date_range_from_column(
    file_path: Path,
    source_column: str,
    date_format: str,
) -> tuple[datetime, datetime]:
    """Read CSV rows and return (min_date, max_date) from source_column."""
    dates: list[datetime] = []

    with file_path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if source_column not in (reader.fieldnames or []):
            fh.seek(0)
            reader = _find_header_reader(fh, source_column)

        for row in reader:
            raw = (row.get(source_column, "") or "").strip()
            if not raw:
                continue
            try:
                dates.append(datetime.strptime(raw, date_format))
            except ValueError:
                _logger.debug(
                    "Skipping unparseable %s value '%s' in %s",
                    source_column,
                    raw,
                    file_path.name,
                )

    if not dates:
        raise ValueError(
            f"No parseable values found for column '{source_column}' in '{file_path.name}'."
        )

    return min(dates), max(dates)


def _find_header_reader(fh: TextIO, source_column: str) -> csv.DictReader:
    """Return DictReader positioned at the header line that contains source_column."""
    for line in fh:
        if source_column in line:
            remaining = line + fh.read()
            return csv.DictReader(StringIO(remaining))
    raise ValueError(f"Column '{source_column}' not found in file.")


def _display_root(ctx: Any) -> Path | None:
    """Return the installation root path used to make file paths relative in records."""
    paths = getattr(ctx, "paths", None)
    if hasattr(paths, "resolve"):
        try:
            return paths.resolve("root")
        except Exception:
            pass
    return None


def _display_path(path: Path, environment_root: Path | None) -> str:
    resolved = path.resolve()
    if environment_root is not None:
        try:
            return resolved.relative_to(environment_root).as_posix()
        except ValueError:
            pass
    return str(resolved)


def _csv_writer(outfile: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to a CSV file using the key order of the first row.

    Uses QUOTE_NONE so values are written exactly as-is — no extra quoting
    or escaping is applied by the CSV writer. Values that already contain
    quote characters (e.g. constants configured with quote: '"') are written
    verbatim.
    """
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    with outfile.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=list(rows[0].keys()),
            quoting=csv.QUOTE_NONE,
            quotechar="\x00",
            escapechar="\\",
        )
        writer.writeheader()
        writer.writerows(rows)

    _logger.debug("Wrote %d row(s) to %s", len(rows), outfile.name)


# ---------------------------------------------------------------------------
# Private — XLSX reader / writer
# ---------------------------------------------------------------------------




def _xlsx_reader(
    infile: Path,
    row_filter: Optional[Callable[[dict[str, str]], bool]],
) -> Generator[dict[str, str], None, None]:
    """
    Yield rows from an XLSX file as string dicts.

    pandas is imported on demand — it is a heavy optional dependency.
    """
    import pandas as pd  # noqa: PLC0415

    df = pd.read_excel(infile)
    for record in df.to_dict(orient="records"):
        row = {str(k): str(v) if v is not None else "" for k, v in record.items()}
        if row_filter is not None and not row_filter(row):
            continue
        yield row


def _xlsx_writer(outfile: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to an XLSX file. pandas imported on demand."""
    import pandas as pd  # noqa: PLC0415

    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(rows).to_excel(outfile, index=False)
    _logger.debug("Wrote %d row(s) to %s", len(rows), outfile.name)
