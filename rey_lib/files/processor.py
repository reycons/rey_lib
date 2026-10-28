"""
Per-file processing pipeline for file_operator.

Orchestrates the redact → sample → write pipeline for each source file.
Batch execution is also handled here so that ``main.py`` remains purely an
entry-point orchestrator.

Layout-specific work lives in the ``file_operator.layouts`` package: each file
type (delimited, excel, fixed_width, …) is its own handler module that
registers itself, so adding a new file type requires no change here — the
dispatch resolves the handler from the registry.

After each file is processed the source file is moved:

  - success → ``args.processed_path``
  - failure → ``args.rejected_path``

When running in single-file mode (``--input``) no movement occurs.

Output per file:

  {stem}.redacted.sample{ext}   — sampled, redacted raw lines
  {stem}.profile.json            — structural column profile

Public API
----------
run_batch      Process a list of files, returning an exit code.
process_file   Run the full pipeline for a single file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_routing import FileRoutingError
from rey_lib.files.file_utils import move_file
from rey_lib.logs import (
    AppLogger,
    log_input_discovered,
    log_row_count,
    log_validation_result,
)

from rey_lib.files.error_utils import LayoutError, RedactorError
from rey_lib.files.input_utils import resolve_layout
from rey_lib.files.layouts import get_handler, registered_layouts

__all__: list[str] = ["run_batch", "process_file"]

_log = AppLogger(__name__)


def run_batch(files: list[Path], ctx: Any, run_log, args: Any) -> int:
    """Process a list of source files and return an exit code.

    Each file is processed independently.  On success the source file is
    moved to ``args.processed_path``; on failure it moves to
    ``args.rejected_path``.  Neither move occurs when those attributes are
    absent (single-file mode).

    Parameters
    ----------
    files : list[Path]
        Source files to process.
    ctx : Namespace
        Application context from ``build_ctx`` (used for logging only).
    args : argparse.Namespace
        Parsed CLI arguments, with path attributes set by ``main.py``.

    Returns
    -------
    int
        ``0`` when all files succeed; ``1`` when one or more fail.
    """
    processed_path: Path | None = getattr(args, "processed_path", None)
    rejected_path:  Path | None = getattr(args, "rejected_path",  None)

    success = failed = 0

    for file_path in files:
        log_input_discovered(run_log,
            input_name=file_path.name,
            path=str(file_path),
            exists=file_path.exists(),
            safe_to_preview=False,
            source="data_engine",
        )

    log_row_count(run_log,
        count_name="files_queued",
        count=len(files),
        subject="data_engine",
    )

    for file_path in files:
        try:
            process_file(ctx, run_log, file_path, args)
            success += 1
            if processed_path:
                _move(ctx, run_log, file_path, processed_path, "processed")
        except Exception as exc:  # noqa: BLE001 — failure reported, batch continues
            # Handled -- counted, recorded, rejected, the next file runs -- so
            # reported here (rule 79).
            error = exc if isinstance(exc, AppError) else RedactorError(
                f"processing '{file_path.name}' failed")
            if error is not exc:
                error.__cause__ = exc
            error.report(f"processing {file_path.name}", source=__name__)
            failed += 1
            log_validation_result(run_log,
                validation_name="file_processing",
                status="failed",
                message="File processing failed.",
                file_name=file_path.name,
                path=str(file_path),
            )
            if rejected_path:
                _move(ctx, run_log, file_path, rejected_path, "rejected")

    _log.info("Complete: success=%d  failed=%d", success, failed)
    log_row_count(run_log,
        count_name="files_processed",
        count=success,
        subject="data_engine",
    )
    log_row_count(run_log,
        count_name="files_failed",
        count=failed,
        subject="data_engine",
    )
    return 0 if failed == 0 else 1


def process_file(ctx: Any, run_log: Any, file_path: Path, args: Any) -> None:
    """Run the full pipeline for a single source file.

    Determines the layout, resolves the matching handler from the registry,
    and delegates the redact → sample → write pipeline to it.

    Parameters
    ----------
    ctx : Namespace
        Application context from ``build_ctx``.
    run_log : Any
        The run log the layout handler records through.
    file_path : Path
        Source file to process.
    args : argparse.Namespace
        Parsed CLI arguments.

    Raises
    ------
    LayoutError
        When the layout cannot be determined or has no registered handler.
    """
    layout_name = resolve_layout(file_path, args.layout)
    output_dir  = Path(args.output_dir)

    _log.info("processing: %s  layout=%s", file_path.name, layout_name)

    handler = get_handler(layout_name)
    if handler is None:
        raise LayoutError(
            f"No handler registered for layout '{layout_name}' "
            f"(file '{file_path.name}'). Registered: {registered_layouts()}."
        )

    handler(ctx, run_log, file_path, output_dir, args)


# ---------------------------------------------------------------------------
# Private — file movement
# ---------------------------------------------------------------------------

def _move(ctx: Any, run_log: Any, file_path: Path, dest_dir: Path,
          label: str) -> None:
    """Move a source file to a destination directory, creating it if absent.

    Parameters
    ----------
    ctx : Any
        Application context — passed to move_file for state logging.
    file_path : Path
        File to move.
    dest_dir : Path
        Destination directory.
    label : str
        Human-readable label used in log messages (e.g. ``'processed'``).
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = move_file(
            file_path,
            dest_dir,
            state_ctx=ctx,
            run_log=run_log,
            app="data_engine",
            pipeline=getattr(ctx, "pipeline_name", None),
            reason=label,
            metadata={
                "source_name": getattr(ctx, "source_name", ""),
                "pipeline_step_name": getattr(ctx, "pipeline_step_name", ""),
                "pipeline_step_id": getattr(ctx, "pipeline_step_id", ""),
            },
        )
        _log.info("moved to %s: %s", label, dest_path)
    except Exception as exc:  # noqa: BLE001
        # Handled -- processing continues -- so reported here (rule 79).
        error = FileRoutingError(f"could not move {file_path.name} to {label}")
        error.__cause__ = exc
        error.report(f"moving {file_path.name} to {label}", source=__name__)
