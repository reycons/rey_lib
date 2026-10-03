"""Kicking a failed file's ORIGINAL out of processing (rule 75, row 589).

Rule 75 (``a_failed_file_is_kicked_out``): any per-file failure in a file
workflow step kicks that file out -- the governed ORIGINAL where classification
put it, never a derived copy -- through the governed move path, before the step
records its failure. Steps 5 (profile) and 6 (prepare) share this one mechanism.

    FileManifest.history(file) -> the latest live, successful move
      -> its result is moved_to_processing?  (else: the original has left
         processing, and nothing is moved)
      -> ManifestSource at that mutation -> DataFile (base_path, classification)
      -> the declared destination resolved from that governed record
         (e.g. '{kickouts}/<file_name>' -> <base_path>/work/kickouts/<file_name>)
      -> Transform(move, role=kickouts, name=<file name>, operation=<step's>)

It never raises: the step's failure is the error the file already has, and a
move that cannot be made must not replace it. Routing records its own
failed-move mutation, so a move that does not take effect is still evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.data.errors import DataStructureError
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.files import FileRoutingError
from rey_lib.files.manifest import FileManifest
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.record_templates import resolve_record_template
from rey_lib.load.transform import Transform
from rey_lib.logs import FileManifestError, get_logger

__all__ = ["kick_out_original"]

_log = get_logger(__name__)

#: The result a move into processing records; the original is where it says.
_IN_PROCESSING = "moved_to_processing"


def kick_out_original(
    ctx: Any,
    *,
    destination: Any,
    file_manifest_id: int,
    source: Path,
    operation: str,
) -> None:
    """Move the ORIGINAL of a file a step could not handle to its kickouts.

    Args:
        ctx: The runtime context: its shared Control, app name and data path.
        destination: The step's declared destination template, a full path
            such as ``{kickouts}/<file_name>`` (after workflow tokens), resolved
            against the original's own governed record. Absent or blank leaves
            the file where it is.
        file_manifest_id: The governed file that failed.
        source: The path the step failed on (the derived copy), for messages.
        operation: The failing step's operation; the move's evidence states it.
    """
    if not isinstance(destination, str) or not destination.strip():
        _log.warning(
            "'%s' failed in %s and no kickouts destination is declared for this "
            "step, so it stays where it is.", source.name, operation,
        )
        return

    control = getattr(ctx, "shared_control", None)
    try:
        history = FileManifest(control).history(int(file_manifest_id))
        moves = [
            mutation for mutation in history
            if mutation.get("deleted_in") is None
            and mutation.get("status") == "success"
            and mutation.get("action") == "move"
        ]
        if not moves or moves[-1].get("result") != _IN_PROCESSING:
            _log.warning(
                "'%s' failed in %s, and its original (governed file %s) is not "
                "in processing, so there is nothing to kick out.",
                source.name, operation, file_manifest_id,
            )
            return
        original = ManifestSource.create(
            control, file_mutation_id=int(moves[-1]["file_mutation_id"]))
        data_file = original.data_file()
        resolution = resolve_record_template(destination.strip(), {
            "file_name": original.governed_context().get("file_name"),
            "base_path": data_file.base_path,
            "classification": data_file.classification,
        })
        if not resolution.resolved:
            _log.warning(
                "'%s' failed in %s, and its kickouts destination cannot be "
                "resolved: field %r is missing or empty.",
                source.name, operation, resolution.missing_field,
            )
            return
        target = Path(resolution.path)
        Transform(
            values={
                "role": "kickouts",
                "route": str(target.parent),
                "name": target.name,
                # The failing step is the operation; the move is what it does
                # with a file it cannot handle. One operation, two records.
                "operation": operation,
            },
            selected="move",
        ).resolve(ctx).apply(data_file)
    except (FileRoutingError, FileManifestError, ConfigError, DatabaseError, OSError,
            ValueError, KeyError, DataStructureError) as exc:
        _log.warning("'%s' failed in %s and its original could not be moved to "
                     "kickouts: %s", source.name, operation, exc)
