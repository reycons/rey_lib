"""
Installation-scoped governed file manifest.

The file manifest is not an application log. It is a separate append-only JSONL
file recording governed source-file lifecycle facts for one installation
(SGC_Wolff_Popper_Source_File_Inventory_and_File_Manifest). Application
execution records continue to go to the run log; nothing is written here first
and copied there, or the reverse.

Sequencing differs from the run log in one decisive way. A run log has exactly
one writer, so its ``record_id`` can come from unlocked run state. The manifest
is shared by every run in the installation, so assigning a row number requires
real mutual exclusion. The complete critical section — load state, repair it,
assign ``record_id``, append, commit state — is held under an exclusive
``flock`` on a companion lock file:

    file_manifest.jsonl
    file_manifest.jsonl.lock
    file_manifest.jsonl.hstate.json

State carries the committed manifest size in bytes alongside the last record
id. A writer interrupted after the append but before the state commit leaves
the recorded size disagreeing with the file, and the next writer inspects the
manifest and repairs state to its highest retained ID before assigning. Two
concurrent writers can therefore never receive the same ``record_id``.
Governed rewrites may intentionally leave ID gaps; retained IDs are never
renumbered and the next append continues above the highest retained ID.

``flock`` is released by the kernel when a holding process dies, so an
interrupted writer cannot wedge the manifest. This requires a POSIX filesystem.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from rey_lib.logs.logging_setup import get_logger
__all__ = [
    "FileManifestError",
    "FileManifestSession",
    "file_manifest_session",
    "log_file_manifest_record",
    "resolve_file_manifest_path",
]

_LOCK_SUFFIX = ".lock"
_STATE_SUFFIX = ".hstate.json"
_PATH_NAME = "file_manifest"

_LAST_RECORD_ID = "last_record_id"
_MANIFEST_SIZE_BYTES = "manifest_size_bytes"

# The canonical persisted order of manifest root fields, shared by every record
# type. This is the final serialization boundary, so it owns the order; a
# record-type serializer owns which of these fields exist and what they hold,
# never where they sit. Fields a record type does not carry are omitted, and a
# root field that is not listed here is rejected rather than written.
_CANONICAL_ROOT_FIELDS: tuple[str, ...] = (
    "record_id",
    "file_id",
    "recorded_at",
    "record_type",
    "action",
    "status",
    "source_name",
    "evidence",
    "file",
    "lineage",
    "classification",
    "rollback",
    "conversion",
    "result",
    "producer",
)

_logger = get_logger(__name__)


class FileManifestError(Exception):
    """Raised when a governed manifest record cannot be sequenced or appended.

    Deliberately not derived from ``rey_lib.errors.AppError``: the logging layer
    sits below ``rey_lib.errors``, which imports ``get_logger`` from this
    package, so a module-level errors import here would close a cycle. Every
    other rey_lib.logs module reaches ``rey_lib.errors`` through a lazy
    in-function import for the same reason.
    """


def resolve_file_manifest_path(ctx: Any) -> Path:
    """
    Resolve the installation-configured manifest path from the context.

    Parameters
    ----------
    ctx : Any
        Application context carrying the resolved installation ``paths``.

    Returns
    -------
    Path
        The configured ``file_manifest`` path.

    Raises
    ------
    FileManifestError
        If no installation-owned ``file_manifest`` path is configured. There is
        no default and no derived location — a missing path is a configuration
        failure, never something this layer invents.
    """
    resolver = getattr(ctx, "paths", None)
    resolve = getattr(resolver, "resolve", None)
    if not callable(resolve):
        raise FileManifestError(
            "Resolved context carries no path resolver; the installation-owned "
            "'file_manifest' path cannot be resolved."
        )
    # Imported lazily because rey_lib.errors imports get_logger from this
    # package; a module-level import would close a cycle.
    from rey_lib.errors.error_utils import ConfigError

    try:
        return Path(resolve(_PATH_NAME))
    except ConfigError as exc:
        raise FileManifestError(
            f"Installation configuration does not define a '{_PATH_NAME}' path: {exc}"
        ) from exc


def log_file_manifest_record(ctx: Any, record: dict[str, Any]) -> int:
    """
    Record one governed file fact, and return the identity the database gave it.

    Parameters
    ----------
    ctx : Any
        Application context exposing the installation's shared ``Control``.
    record : dict[str, Any]
        One governed manifest record. It must not carry an identity of its own:
        a governed file's identity is what recording it establishes.

    Returns
    -------
    int
        ``file_manifest_id`` for inventory and classification,
        ``file_mutation_id`` for a mutation. For inventory this value *is* the
        governed file's ``file_id``.

    Raises
    ------
    FileManifestError
        If the installation exposes no shared Control, the record is malformed,
        or it carries a type this boundary does not govern.
    """
    _validate_unsequenced_record(record)
    return write_record_to_control(ctx, record)


def _validate_unsequenced_record(record: Any) -> None:
    """Reject malformed records before they reach the governed boundary.

    A record must not carry an identity of its own. Identity is what recording
    a governed file establishes, and the database mints it -- so a caller that
    supplies one is naming a file that does not exist yet.
    """
    if not isinstance(record, dict):
        raise FileManifestError("A manifest record must be a JSON object.")
    if "record_id" in record:
        raise FileManifestError(
            "A manifest record must not supply 'record_id'; a governed file's "
            "identity is established by recording it."
        )
    # A field nobody can name is a field the governed boundary would silently
    # drop. Refusing it keeps a writer from believing it recorded something.
    unknown = sorted(set(record) - set(_CANONICAL_ROOT_FIELDS))
    if unknown:
        raise FileManifestError(
            "Manifest record carries unknown root field(s): " + ", ".join(unknown)
        )


def write_record_to_control(ctx: Any, record: dict[str, Any]) -> int:
    """Route one governed record to ``control.file_manifest``, and return its id.

    Migration scaffolding. It exists so every writer can keep the call it makes
    today while authority moves in one commit; once the database is
    authoritative each writer reaches ``FileManifest`` directly and this
    translation goes with the JSONL manifest.

    The record types are the manifest's own, and each maps to one domain
    operation rather than to a row of the same shape:

        source_file_inventory                    -> inventory()
        source_file_mutation / _rollback         -> append_mutation()
        source_file_classification               -> update()

    Classification is state on the file, not an event beside it, so a
    classified outcome updates the row it names. A rejected one changes no file
    state and writes nothing here: the rejection is already committed to the run
    log before this is reached, and recording it again as a file record would be
    the duplicate the database model exists to remove.

    Returns
    -------
    int
        ``file_manifest_id`` for inventory and classification, and
        ``file_mutation_id`` for a mutation -- in each case the identity the
        database generated for what was actually written.

    Raises
    ------
    FileManifestError
        When the installation exposes no shared Control, or the record carries a
        type this boundary does not govern.
    """
    from rey_lib.files.manifest import FileManifest

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise FileManifestError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to reach it through."
        )
    manifest = FileManifest(control)
    record_type = str(record.get("record_type") or "")
    file_object = dict(record.get("file") or {})

    if record_type == "source_file_inventory":
        return manifest.inventory(
            path=str(file_object.get("path") or ""),
            file_name=str(file_object.get("file_name") or ""),
            base_name=str(file_object.get("base_name") or ""),
            file_extension=str(file_object.get("file_extension") or ""),
            checksum_sha256=str(file_object.get("checksum_sha256") or ""),
            size_bytes=int(file_object.get("size_bytes") or 0),
            source_name=str(record.get("source_name") or ""),
            evidence=record.get("evidence"),
            producer=record.get("producer"),
            classification=record.get("classification"),
        )

    if record_type in ("source_file_mutation", "source_file_rollback"):
        # A governed file has one identity. The application calls it file_id and
        # the database calls it file_manifest_id; they are the same value, so
        # the record's file_id is written straight into the column. lineage
        # points at the previous *mutation*, which is a different thing and
        # stays where it is.
        evidence = dict(record.get("evidence") or {})
        lineage = dict(record.get("lineage") or {})
        return manifest.append_mutation(
            int(record.get("file_id")),
            record_type=record_type,
            action=str(record.get("action") or ""),
            status=str(record.get("status") or ""),
            source_record_id=lineage.get("source_record_id"),
            run_log_id=evidence.get("run_log_id"),
            path=str(file_object.get("path") or ""),
            producer=record.get("producer"),
            conversion=record.get("conversion"),
            result=record.get("result"),
            rollback=record.get("rollback"),
        )

    if record_type == "source_file_classification":
        file_manifest_id = int(record.get("file_id") or 0)
        if not file_manifest_id:
            raise FileManifestError(
                "A classification names the file it classifies by file_id, "
                "which is that file's manifest identity."
            )
        if str(record.get("status") or "") == "classified":
            # The path moves when classification routes the file to processing,
            # so it travels with the classification rather than in a second call.
            manifest.update(
                file_manifest_id,
                classification=record.get("classification"),
                path=file_object.get("path"),
            )
        return file_manifest_id

    raise FileManifestError(
        f"'{record_type}' is not a governed file manifest record type."
    )


def read_records_from_control(ctx: Any, *,
                              file_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Governed files, each followed by its own mutations.

    The stored model is a file and its children, so that is how this reads:
    every file, and beneath each one its history. It is not a stream to be
    grouped -- the grouping is the storage.

    Two things the database model changed, which a consumer sees here:

    - ``record_id`` and ``file_id`` are the same value on a file. A governed
      file has one identity and the database mints it.
    - classification is state on the file, not a record beside it, so there is
      no ``source_file_classification`` entry. A file's classification is read
      from the file.

    Parameters
    ----------
    ctx : Any
        Application context exposing the installation's shared ``Control``.
    file_id : Optional[int]
        One governed file, instead of all of them.

    Returns
    -------
    list[dict[str, Any]]
        The file record, then its mutations, oldest first, per file.
    """
    from rey_lib.files.manifest import FileManifest

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise FileManifestError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to reach it through."
        )
    manifest = FileManifest(control)
    files = ([manifest.get(int(file_id))] if file_id is not None
             else manifest.list_files())

    # One read for every file's mutations, not one read per file. Asking each
    # file for its own history is a round trip per file, and this projection is
    # built several times a run: 229 files became 916 reads of one table.
    #
    # The rows arrive ordered by file_mutation_id, which is monotonic, so
    # appending them in arrival order leaves each file's mutations oldest-first
    # -- the order the running `previous_path` below depends on. Nothing here
    # may reorder them.
    #
    # A single named file still asks for its own history: one call for one file
    # is already the smallest read, and routing it through the whole table
    # would be the same mistake inverted.
    grouped: dict[int, list[dict[str, Any]]] = {}
    if file_id is None:
        for mutation in manifest.all_mutations():
            key = mutation.get("file_manifest_id")
            if key is None:
                continue
            grouped.setdefault(int(key), []).append(mutation)

    records: list[dict[str, Any]] = []
    for row in files:
        if not row:
            continue
        records.append(_inventory_record(row))
        # The baseline mutation records the same fact as the file itself -- that
        # it was inventoried -- so emitting both would put one event in the
        # stream twice, under one identity. History keeps it; this does not.
        #
        # Where a mutation moved the file *from* is not stored: it is the path
        # the file had before, which the history in front of it already says.
        # Carrying that running path is what lets a consumer reduce the
        # lifecycle without re-deriving it or reading the filesystem.
        previous_path = row.get("path")
        identity = int(row["file_manifest_id"])
        # `.get` rather than `[]`: a file with no mutations produced an empty
        # history before and must still produce one record and no children.
        history = (manifest.history(identity) if file_id is not None
                   else grouped.get(identity, []))
        for child in history:
            if child.get("record_type") == "source_file_inventory":
                previous_path = child.get("path") or previous_path
                continue
            record = _mutation_record(child)
            record["file"]["original_path"] = previous_path
            if child.get("path") and not child.get("rollback_complete_in"):
                previous_path = child.get("path")
            records.append(record)
    return records


def _inventory_record(row: dict[str, Any]) -> dict[str, Any]:
    """Project one file_manifest row into its canonical inventory record."""
    identity = row.get("file_manifest_id")
    record: dict[str, Any] = {
        "record_id": identity,
        "file_id": identity,
        "record_type": "source_file_inventory",
        "recorded_at": row.get("created_ts"),
        "source_name": row.get("source_name"),
        "evidence": _as_mapping(row.get("evidence")),
        "producer": _as_mapping(row.get("producer")),
        "file": {
            "path": row.get("path"),
            "file_name": row.get("file_name"),
            "base_name": row.get("base_name"),
            "file_extension": row.get("file_extension"),
            "checksum_sha256": row.get("checksum_sha256"),
            "size_bytes": row.get("size_bytes"),
        },
    }
    classification = _as_mapping(row.get("classification"))
    if classification:
        record["classification"] = classification
    return record


def _mutation_record(row: dict[str, Any]) -> dict[str, Any]:
    """Project one file_mutation row into its canonical mutation record."""
    record: dict[str, Any] = {
        "record_id": row.get("file_mutation_id"),
        "file_id": row.get("file_manifest_id"),
        "record_type": row.get("record_type"),
        "action": row.get("action"),
        "status": row.get("status"),
        "recorded_at": row.get("created_ts"),
        "evidence": {
            "run_log_id": row.get("run_log_id"),
        },
        "lineage": {"source_record_id": row.get("source_record_id")},
        "file": {"path": row.get("path")},
        # A reversed mutation is still history, but it no longer says where the
        # file is. A consumer reducing current state reads this rather than
        # looking for a separate rollback record, because there is not one.
        "rollback_complete_in": row.get("rollback_complete_in") or 0,
    }
    for key in ("producer", "conversion", "result", "rollback"):
        value = _as_mapping(row.get(key))
        if value:
            record[key] = value
    return record


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return a jsonb column as a mapping; the driver may hand back text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return json.loads(value)
    return {}


class FileManifestSession:
    """Public append/read surface held under the shared manifest lock."""

    def __init__(self, manifest_path: Path, ctx: Any = None) -> None:
        self.path = manifest_path
        self._ctx = ctx

    def read_records(self) -> list[dict[str, Any]]:
        """Every current governed record, oldest first."""
        return read_records_from_control(self._ctx)




@contextmanager
def file_manifest_session(ctx: Any) -> Iterator[FileManifestSession]:
    """Yield a manifest session for governed multi-record work.

    No lock is taken. Every operation beneath this session is one atomic
    routine call, so there is no critical section for a caller to hold open.
    """
    yield FileManifestSession(resolve_file_manifest_path(ctx), ctx)

