"""Application-neutral Excel workbook to CSV conversion.

This module owns one primitive conversion boundary:

* open a supported workbook once through FastExcel/Calamine;
* extract defined tables where FastExcel exposes them;
* otherwise extract eligible worksheets;
* serialize each extracted object with Polars using a fixed CSV contract; and
* publish all resulting files without overwriting existing destinations.

The module has no knowledge of Rey configuration, installations, workflows,
pipelines, feeds, or application-specific records.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal
import unicodedata

from rey_lib.errors.error_utils import AppError
SUPPORTED_WORKBOOK_EXTENSIONS = frozenset({".xls", ".xlsx", ".xlsb", ".xlsm"})
_TABLE_CAPABLE_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
_INVALID_NAME_CHARS = re.compile(r"""[.\s/\\<>:"|?*\x00-\x1f\x7f]+""")

__all__ = [
    "SUPPORTED_WORKBOOK_EXTENSIONS",
    "ConvertedTableArtifact",
    "EmptyWorkbookError",
    "UnsupportedWorkbookError",
    "WorkbookConversionError",
    "WorkbookConversionResult",
    "WorkbookConversionWarning",
    "WorkbookDependencyError",
    "WorkbookEncryptedError",
    "WorkbookExtractionError",
    "WorkbookOpenError",
    "WorkbookOutputCollisionError",
    "WorkbookWriteError",
    "convert_workbook_to_csv",
    "is_supported_workbook",
]


@dataclass(frozen=True)
class WorkbookConversionWarning:
    """Neutral warning produced by a successful workbook conversion."""

    code: str
    message: str
    sheet_name: str | None = None
    table_name: str | None = None


@dataclass(frozen=True)
class ConvertedTableArtifact:
    """Metadata for one CSV artifact produced from a table or worksheet."""

    output_path: Path
    artifact_name: str
    extraction_kind: Literal["defined_table", "worksheet"]
    sheet_name: str
    sheet_index: int
    table_name: str | None
    row_count: int
    column_count: int
    column_names: tuple[str, ...]
    polars_schema: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class WorkbookConversionResult:
    """Successful result returned by :func:`convert_workbook_to_csv`."""

    source_path: Path
    source_extension: str
    workbook_name: str
    outputs: tuple[ConvertedTableArtifact, ...]
    warnings: tuple[WorkbookConversionWarning, ...]
    conversion_status: Literal["success", "success_with_warnings"]


class WorkbookConversionError(AppError):
    """Base class for neutral workbook conversion failures."""

    code = "workbook_conversion_failed"

    def __init__(
        self,
        message: str,
        source_path: Path | str,
        *,
        sheet_name: str | None = None,
        table_name: str | None = None,
        output_path: Path | str | None = None,
    ) -> None:
        super().__init__(message)
        self.source_path = Path(source_path)
        self.sheet_name = sheet_name
        self.table_name = table_name
        self.output_path = Path(output_path) if output_path is not None else None


class WorkbookDependencyError(WorkbookConversionError):
    """A required workbook conversion dependency is unavailable."""

    code = "workbook_dependency_missing"


class UnsupportedWorkbookError(WorkbookConversionError):
    """The source path does not have a supported workbook extension."""

    code = "unsupported_workbook"


class WorkbookOpenError(WorkbookConversionError):
    """The workbook could not be opened or parsed."""

    code = "workbook_open_failed"


class WorkbookEncryptedError(WorkbookOpenError):
    """The workbook is password protected and cannot be opened."""

    code = "workbook_encrypted"


class EmptyWorkbookError(WorkbookConversionError):
    """The workbook produced no eligible tabular output."""

    code = "empty_workbook"


class WorkbookExtractionError(WorkbookConversionError):
    """A workbook table or worksheet could not be extracted."""

    code = "workbook_extraction_failed"


class WorkbookOutputCollisionError(WorkbookConversionError):
    """Two outputs collide or an output destination already exists."""

    code = "workbook_output_collision"


class WorkbookWriteError(WorkbookConversionError):
    """A CSV artifact could not be staged or published."""

    code = "workbook_write_failed"


@dataclass(frozen=True)
class _PlannedOutput:
    """Internal extracted frame awaiting transactional publication."""

    frame: Any
    artifact_name: str
    extraction_kind: Literal["defined_table", "worksheet"]
    sheet_name: str
    sheet_index: int
    table_name: str | None


def is_supported_workbook(source_path: Path | str) -> bool:
    """Return whether ``source_path`` has a supported workbook extension."""

    return Path(source_path).suffix.lower() in SUPPORTED_WORKBOOK_EXTENSIONS


def convert_workbook_to_csv(
    source_path: Path | str,
    output_dir: Path | str,
    *,
    include_hidden_sheets: bool = False,
    include_empty_sheets: bool = False,
    overwrite: bool = False,
) -> WorkbookConversionResult:
    """Convert one supported workbook into deterministic flat CSV artifacts.

    The source workbook is opened once. XLSX-family workbooks emit defined
    tables and use worksheet fallback only for sheets that contain no defined
    tables. Legacy XLS and binary XLSB workbooks use worksheet fallback because
    FastExcel does not expose table enumeration for those formats.

    All output is staged before publication. Existing files are replaced only
    when the caller's declared output folder authorizes ``overwrite``. Any
    failure restores replaced files and removes outputs newly published by this
    call.
    """

    source = Path(source_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be a boolean.")
    extension = source.suffix.lower()

    if extension not in SUPPORTED_WORKBOOK_EXTENSIONS:
        raise UnsupportedWorkbookError(
            f"Unsupported workbook extension '{source.suffix}'.",
            source,
        )
    if not source.is_file():
        raise WorkbookOpenError(f"Workbook does not exist: {source}", source)

    fastexcel, _polars = _load_dependencies(source)
    reader = _open_workbook(fastexcel, source)
    planned, warnings = _extract_outputs(
        reader,
        source,
        extension=extension,
        include_hidden_sheets=include_hidden_sheets,
        include_empty_sheets=include_empty_sheets,
    )
    if not planned:
        raise EmptyWorkbookError(
            "Workbook contains no eligible non-empty worksheets or defined tables.",
            source,
        )

    planned = sorted(
        planned,
        key=lambda item: (
            item.sheet_index,
            0 if item.extraction_kind == "defined_table" else 1,
            (item.table_name or "").casefold(),
            item.artifact_name.casefold(),
        ),
    )
    _validate_output_names(source, destination, planned, overwrite=overwrite)
    outputs = _stage_and_publish(
        source,
        destination,
        planned,
        overwrite=overwrite,
    )

    return WorkbookConversionResult(
        source_path=source,
        source_extension=extension,
        workbook_name=source.name,
        outputs=tuple(outputs),
        warnings=tuple(warnings),
        conversion_status="success_with_warnings" if warnings else "success",
    )


def _load_dependencies(source: Path) -> tuple[Any, Any]:
    """Import optional conversion dependencies only when the API is invoked."""

    try:
        import fastexcel  # noqa: PLC0415
        import polars  # noqa: PLC0415
    except ImportError as exc:
        raise WorkbookDependencyError(
            "Workbook conversion requires the rey_lib 'files' dependencies "
            "'fastexcel' and 'polars'.",
            source,
        ) from exc
    return fastexcel, polars


def _open_workbook(fastexcel: Any, source: Path) -> Any:
    """Open one workbook and translate dependency errors to the public contract."""

    try:
        return fastexcel.read_excel(source)
    except Exception as exc:  # noqa: BLE001 - third-party exception boundary
        message = str(exc)
        if "password protected" in message.casefold():
            raise WorkbookEncryptedError(
                f"Workbook is password protected: {source}",
                source,
            ) from exc
        raise WorkbookOpenError(f"Could not open workbook '{source}': {message}", source) from exc


def _extract_outputs(
    reader: Any,
    source: Path,
    *,
    extension: str,
    include_hidden_sheets: bool,
    include_empty_sheets: bool,
) -> tuple[list[_PlannedOutput], list[WorkbookConversionWarning]]:
    """Extract tables/sheets from an already-open workbook reader."""

    sheet_names = list(reader.sheet_names)
    if not sheet_names:
        return [], []

    planned: list[_PlannedOutput] = []
    warnings: list[WorkbookConversionWarning] = []
    workbook_component = _sanitize_component(source.stem)

    for sheet_index, sheet_name in enumerate(sheet_names):
        visibility = _sheet_visibility(reader, source, sheet_name)
        if visibility != "visible" and not include_hidden_sheets:
            warnings.append(
                WorkbookConversionWarning(
                    code="sheet_excluded",
                    message=f"Excluded {visibility} worksheet '{sheet_name}'.",
                    sheet_name=sheet_name,
                )
            )
            continue

        table_names = _table_names(reader, source, extension, sheet_name)
        if table_names:
            for table_name in table_names:
                frame = _load_table(reader, source, sheet_name, table_name)
                if _is_empty_frame(frame) and not include_empty_sheets:
                    warnings.append(
                        WorkbookConversionWarning(
                            code="empty_table_excluded",
                            message=f"Excluded empty table '{table_name}' on '{sheet_name}'.",
                            sheet_name=sheet_name,
                            table_name=table_name,
                        )
                    )
                    continue
                planned.append(
                    _PlannedOutput(
                        frame=frame,
                        artifact_name=_artifact_name(
                            workbook_component,
                            sheet_name,
                            table_name,
                        ),
                        extraction_kind="defined_table",
                        sheet_name=sheet_name,
                        sheet_index=sheet_index,
                        table_name=table_name,
                    )
                )
            # A worksheet containing defined tables never falls back to a whole
            # worksheet artifact, including when every defined table is empty.
            continue

        frame = _load_sheet(reader, source, sheet_name)
        if _is_empty_frame(frame) and not include_empty_sheets:
            warnings.append(
                WorkbookConversionWarning(
                    code="empty_sheet_excluded",
                    message=f"Excluded empty worksheet '{sheet_name}'.",
                    sheet_name=sheet_name,
                )
            )
            continue
        planned.append(
            _PlannedOutput(
                frame=frame,
                artifact_name=_artifact_name(workbook_component, sheet_name),
                extraction_kind="worksheet",
                sheet_name=sheet_name,
                sheet_index=sheet_index,
                table_name=None,
            )
        )

    return planned, warnings


def _sheet_visibility(reader: Any, source: Path, sheet_name: str) -> str:
    """Read sheet visibility without materializing the full worksheet."""

    try:
        return str(reader.load_sheet(sheet_name, n_rows=0).visible)
    except Exception as exc:  # noqa: BLE001 - third-party exception boundary
        raise WorkbookExtractionError(
            f"Could not inspect worksheet '{sheet_name}': {exc}",
            source,
            sheet_name=sheet_name,
        ) from exc


def _table_names(
    reader: Any,
    source: Path,
    extension: str,
    sheet_name: str,
) -> list[str]:
    """Return deterministic table names where FastExcel exposes the API."""

    if extension not in _TABLE_CAPABLE_EXTENSIONS:
        return []
    try:
        return sorted(reader.table_names(sheet_name), key=lambda value: (value.casefold(), value))
    except Exception as exc:  # noqa: BLE001 - third-party exception boundary
        raise WorkbookExtractionError(
            f"Could not enumerate tables on worksheet '{sheet_name}': {exc}",
            source,
            sheet_name=sheet_name,
        ) from exc


def _load_table(reader: Any, source: Path, sheet_name: str, table_name: str) -> Any:
    """Load one defined table exactly once and convert it directly to Polars."""

    try:
        table = reader.load_table(table_name)
        if str(table.sheet_name) != sheet_name:
            raise ValueError(
                f"table belongs to worksheet '{table.sheet_name}', expected '{sheet_name}'"
            )
        return table.to_polars()
    except Exception as exc:  # noqa: BLE001 - third-party exception boundary
        raise WorkbookExtractionError(
            f"Could not load table '{table_name}' on worksheet '{sheet_name}': {exc}",
            source,
            sheet_name=sheet_name,
            table_name=table_name,
        ) from exc


def _load_sheet(reader: Any, source: Path, sheet_name: str) -> Any:
    """Load one worksheet fallback and convert it directly to Polars."""

    try:
        return reader.load_sheet(sheet_name).to_polars()
    except Exception as exc:  # noqa: BLE001 - third-party exception boundary
        raise WorkbookExtractionError(
            f"Could not load worksheet '{sheet_name}': {exc}",
            source,
            sheet_name=sheet_name,
        ) from exc


def _is_empty_frame(frame: Any) -> bool:
    """Return true when a Polars frame has no rows or no columns."""

    return int(frame.height) == 0 or int(frame.width) == 0


def _artifact_name(workbook_component: str, sheet_name: str, table_name: str | None = None) -> str:
    """Build one deterministic dot-notation artifact name."""

    components = [workbook_component, _sanitize_component(sheet_name)]
    if table_name is not None:
        components.append(_sanitize_component(table_name))
    return ".".join(components) + ".csv"


def _sanitize_component(value: str) -> str:
    """Sanitize one filename component while keeping dot boundaries unambiguous."""

    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    sanitized = _INVALID_NAME_CHARS.sub("_", normalized).strip(" _")
    return sanitized or "unnamed"


def _validate_output_names(
    source: Path,
    destination: Path,
    planned: list[_PlannedOutput],
    *,
    overwrite: bool,
) -> None:
    """Preflight internal and filesystem output collisions."""

    seen: dict[str, str] = {}
    for item in planned:
        collision_key = item.artifact_name.casefold()
        previous = seen.get(collision_key)
        if previous is not None:
            raise WorkbookOutputCollisionError(
                f"Workbook outputs '{previous}' and '{item.artifact_name}' collide.",
                source,
                output_path=destination / item.artifact_name,
            )
        seen[collision_key] = item.artifact_name
        target = destination / item.artifact_name
        if target.exists() and not overwrite:
            raise WorkbookOutputCollisionError(
                f"Workbook output already exists: {target}",
                source,
                output_path=target,
            )


def _stage_and_publish(
    source: Path,
    destination: Path,
    planned: list[_PlannedOutput],
    *,
    overwrite: bool,
) -> list[ConvertedTableArtifact]:
    """Stage all CSVs, then atomically publish under folder collision policy."""

    try:
        destination.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=".workbook_conversion.", dir=destination))
    except OSError as exc:
        raise WorkbookWriteError(
            f"Could not prepare workbook output directory '{destination}': {exc}",
            source,
            output_path=destination,
        ) from exc

    staged: list[tuple[_PlannedOutput, Path, Path]] = []
    published: list[Path] = []
    backups: dict[Path, Path] = {}
    try:
        for item in planned:
            staged_path = staging_dir / item.artifact_name
            final_path = destination / item.artifact_name
            try:
                item.frame.write_csv(
                    staged_path,
                    float_scientific=False,  # Prevent scientific notation in output
                    include_bom=False,
                    include_header=True,
                    separator=",",
                    line_terminator="\n",
                    quote_style="necessary",
                    null_value="",
                )
            except Exception as exc:  # noqa: BLE001 - third-party/filesystem boundary
                raise WorkbookWriteError(
                    f"Could not stage CSV output '{final_path}': {exc}",
                    source,
                    sheet_name=item.sheet_name,
                    table_name=item.table_name,
                    output_path=final_path,
                ) from exc
            staged.append((item, staged_path, final_path))

        for index, (item, staged_path, final_path) in enumerate(staged):
            try:
                if overwrite:
                    if final_path.exists():
                        backup = staging_dir / f".backup.{index}"
                        os.replace(final_path, backup)
                        backups[final_path] = backup
                    os.replace(staged_path, final_path)
                else:
                    # A hard link exposes the complete staged file atomically
                    # and fails when the destination already exists.
                    os.link(staged_path, final_path)
                    staged_path.unlink()
                published.append(final_path)
            except FileExistsError as exc:
                raise WorkbookOutputCollisionError(
                    f"Workbook output already exists: {final_path}",
                    source,
                    sheet_name=item.sheet_name,
                    table_name=item.table_name,
                    output_path=final_path,
                ) from exc
            except OSError as exc:
                raise WorkbookWriteError(
                    f"Could not publish CSV output '{final_path}': {exc}",
                    source,
                    sheet_name=item.sheet_name,
                    table_name=item.table_name,
                    output_path=final_path,
                ) from exc

        return [
            _artifact_result(item, final_path)
            for item, _staged_path, final_path in staged
        ]
    except BaseException:
        # Publication is all-or-nothing even for unexpected interruption.
        for path in reversed(published):
            path.unlink(missing_ok=True)
        for final_path, backup in backups.items():
            if backup.exists():
                os.replace(backup, final_path)
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _artifact_result(item: _PlannedOutput, output_path: Path) -> ConvertedTableArtifact:
    """Build immutable neutral metadata for one published frame."""

    return ConvertedTableArtifact(
        output_path=output_path,
        artifact_name=item.artifact_name,
        extraction_kind=item.extraction_kind,
        sheet_name=item.sheet_name,
        sheet_index=item.sheet_index,
        table_name=item.table_name,
        row_count=int(item.frame.height),
        column_count=int(item.frame.width),
        column_names=tuple(str(name) for name in item.frame.columns),
        polars_schema=tuple((str(name), str(dtype)) for name, dtype in item.frame.schema.items()),
    )
