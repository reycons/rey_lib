"""The shared run-to-file-manifest query behind Run History file links.

The run log owns execution evidence; the file manifest owns governed file
evidence. This module is the one boundary that joins them, so no consumer
implements its own manifest scan.

Selection uses exactly one key — ``evidence.run_log_file`` — and nothing else.
Producers record that pointer as the run log's file name, so a caller's
input is normalized once to that same canonical identity and then compared
as an exact string. Run type never participates: no pipeline, workflow,
application, feed, or folder name may alter which records a run owns.

Because the stored identity is a file name, two run logs that share a file
name are one identity here regardless of the directories they sit in. That
follows the producers and is deliberate; widening it would require changing
what producers record, not what this query compares. Paths come from the field the
producer recorded, never from the current filesystem, so a record remains valid
evidence after its file has moved or gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from rey_lib.logs.file_manifest import FileManifestError, file_manifest_session

__all__ = [
    "RunFileRecord",
    "RunFileRecords",
    "RunFileRecordsError",
    "find_run_file_records",
    "register_run_file_record_type",
]

_MUTATION_RECORD_TYPE = "source_file_mutation"
_INVENTORY_RECORD_TYPE = "source_file_inventory"

# Mutation actions whose authoritative display path is the destination the
# producer recorded. A failed mutation shows the path it attempted.
_DESTINATION_ACTIONS = frozenset({"create", "move", "replace"})

_FAILED_STATUS = "failed"
_DELETED_STATUS = "deleted"


class RunFileRecordsError(Exception):
    """Raised when the governed file manifest cannot be consulted."""


@dataclass(frozen=True)
class RunFileRecord:
    """One governed file event owned by the selected run."""

    manifest_record_id: int
    record_type: str
    action: str
    status: str
    path: str
    record: Mapping[str, Any]

    @property
    def deleted(self) -> bool:
        """Whether this event removed the recorded path."""
        return self.status == _DELETED_STATUS

    @property
    def failed(self) -> bool:
        """Whether the recorded path is an attempt rather than a durable result."""
        return self.status == _FAILED_STATUS


@dataclass(frozen=True)
class RunFileRecords:
    """Every governed file event a run owns, in manifest order."""

    run_log_file: str
    manifest_path: str
    records: tuple[RunFileRecord, ...]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[RunFileRecord]:
        """Iterate the run's governed file events in manifest order."""
        return iter(self.records)


def _file_path(record: Mapping[str, Any], field: str) -> str:
    """Return one recorded location from the canonical ``file`` object."""
    file_object = record.get("file")
    if not isinstance(file_object, Mapping):
        return ""
    return _text(file_object.get(field))


def _mutation_path(record: Mapping[str, Any]) -> str:
    """Return the authoritative display path for one mutation record."""
    action = str(record.get("action") or "").strip().lower()
    status = str(record.get("status") or "").strip().lower()
    # The file object records the logical file's current location and the
    # location it came from.
    destination = _file_path(record, "path")
    source = _file_path(record, "original_path")

    if status == _FAILED_STATUS:
        # A failed mutation shows what it attempted, not an invented result.
        return destination or source
    if action == "delete":
        return source
    if action in _DESTINATION_ACTIONS:
        return destination
    return destination or source


def _inventory_path(record: Mapping[str, Any]) -> str:
    return _file_path(record, "path")


# record_type -> the callable that returns its authoritative display path.
# Registering a type is how a future governed file record joins Run History
# without teaching this module about any particular producer.
_PATH_RESOLVERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    _MUTATION_RECORD_TYPE: _mutation_path,
    _INVENTORY_RECORD_TYPE: _inventory_path,
}


def register_run_file_record_type(
    record_type: str,
    path_resolver: Callable[[Mapping[str, Any]], str],
    *,
    replace: bool = False,
) -> None:
    """Register a governed file record type for Run History display."""
    normalized = str(record_type or "").strip()
    if not normalized:
        raise RunFileRecordsError("record_type must be a non-empty string.")
    if not callable(path_resolver):
        raise RunFileRecordsError("path_resolver must be callable.")
    if normalized in _PATH_RESOLVERS and not replace:
        raise RunFileRecordsError(
            f"A path resolver is already registered for '{normalized}'."
        )
    _PATH_RESOLVERS[normalized] = path_resolver


def find_run_file_records(ctx: Any, run_log_file: Path | str) -> RunFileRecords:
    """Return the governed file records the selected run log owns.

    Parameters
    ----------
    ctx : Any
        Resolved context carrying the installation path resolver.
    run_log_file : Path | str
        The already-authorized selected run log. Normalized to the canonical
        stored identity, so a caller may pass a full path or a bare file name
        and both address the same run.

    Returns
    -------
    RunFileRecords
        Matching records in ascending manifest ``record_id``. Distinct
        lifecycle events are preserved even when they share a path.

    Raises
    ------
    RunFileRecordsError
        If the manifest is missing, unreadable, or not valid JSONL. The caller
        reports that file-manifest evidence is unavailable; it never
        reconstructs links from the run log.
    """
    # Imported lazily because rey_lib.files.jsonl imports get_logger from this
    # package; a module-level import would close a cycle.
    from rey_lib.files.jsonl import JsonlReadError

    selected = _canonical_run_log_identity(run_log_file)

    try:
        with file_manifest_session(ctx) as session:
            manifest_path = session.path
            if not manifest_path.is_file():
                raise RunFileRecordsError(
                    f"File manifest does not exist: {manifest_path}"
                )
            records = session.read_records()
    except RunFileRecordsError:
        raise
    except (FileManifestError, JsonlReadError, OSError, ValueError) as exc:
        raise RunFileRecordsError(
            f"Governed file evidence is unavailable: {exc}"
        ) from exc

    selected_records = [
        item
        for item in (_run_file_record(record, selected) for record in records)
        if item is not None
    ]
    selected_records.sort(key=lambda item: item.manifest_record_id)
    return RunFileRecords(
        run_log_file=selected,
        manifest_path=str(manifest_path),
        records=tuple(selected_records),
    )


def _run_file_record(
    record: Mapping[str, Any],
    selected_run_log_file: str,
) -> RunFileRecord | None:
    """Project one manifest record, or None when it is not an eligible link."""
    if not isinstance(record, Mapping):
        return None
    if _evidence_run_log_file(record) != selected_run_log_file:
        return None

    record_type = str(record.get("record_type") or "").strip()
    resolver = _PATH_RESOLVERS.get(record_type)
    if resolver is None:
        return None

    record_id = _positive_int(record.get("record_id"))
    if record_id is None:
        return None

    path = resolver(record)
    if not path:
        # A record without a usable recorded path never produces a file link.
        return None

    return RunFileRecord(
        manifest_record_id=record_id,
        record_type=record_type,
        action=str(record.get("action") or "").strip().lower(),
        status=str(record.get("status") or "").strip().lower(),
        path=path,
        record=dict(record),
    )


def _evidence_run_log_file(record: Mapping[str, Any]) -> str:
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    return _text(evidence.get("run_log_file"))


def _canonical_run_log_identity(value: Path | str) -> str:
    """Normalize a caller's run log to the identity producers actually store.

    Every producer records ``evidence.run_log_file`` as the run log's file name
    (see rey_lib.files.log_run_rollback and the application producers), so the
    caller is reduced to that same form once and compared exactly thereafter.
    """
    text = str(value or "").strip()
    if not text:
        raise RunFileRecordsError(
            "run_log_file must be a non-empty path or file name."
        )
    return Path(text).name


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None
