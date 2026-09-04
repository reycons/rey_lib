"""Immutable File Manifest hierarchy projection.

Phase 1 projects configured source groups, inventoried files, and mutations.
Phase 2 adds bounded, lazy lifecycle queries.  The module still owns no
filesystem, run-log, artifact discovery, or UI behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.logs.file_manifest import FileManifestError

__all__ = [
    "FileHierarchyError",
    "FileHierarchyFeed",
    "FileHierarchyFile",
    "FileHierarchyMutation",
    "FileHierarchyPage",
    "FileHierarchyStage",
    "FileHierarchyStagePage",
    "FileHierarchyFeedSummary",
    "FileHierarchyFeedPage",
    "build_file_hierarchy",
    "build_file_hierarchy_feeds",
    "build_file_hierarchy_feed",
    "build_file_hierarchy_stages",
]

_INVENTORY = "source_file_inventory"
_MUTATION = "source_file_mutation"
#: The lifecycle event that says what a file is. Classification is history
#: rather than manifest state, so a feed is read from these and never from the
#: file's own record.
_CLASSIFICATION = "source_file_classification"
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 250
_MAX_STAGE_LIMIT = 500


class FileHierarchyError(Exception):
    """Raised when a canonical manifest cannot produce a valid hierarchy."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FileHierarchyError(f"{field} must be a positive integer.")
    return value


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FileHierarchyError(f"{field} must be a nonblank string.")
    return value


def _identity(value: Any, field: str) -> int:
    """Return a governed file's identity.

    That identity is ``control.file_manifest.file_manifest_id``, a generated
    integer which application code also calls ``file_id``. It is one value, so
    it is never rendered as a string on the way through -- a second
    representation is how one object ends up with two identities.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FileHierarchyError(f"{field} must be a governed file identity.")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonblank(value, field)


def _file_mapping(record: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = record.get("file")
    if not isinstance(value, Mapping):
        raise FileHierarchyError(f"{field}.file must be a mapping.")
    return value


@dataclass(frozen=True)
class FileHierarchyMutation:
    """One immutable mutation node in committed manifest order."""

    record_id: int
    label: str
    action: str
    status: str | None
    path: str | None
    original_path: str | None
    metadata: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        """Return one JSON-compatible presentation payload."""
        return {
            "record_id": self.record_id,
            "label": self.label,
            "action": self.action,
            "status": self.status,
            "path": self.path,
            "original_path": self.original_path,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True)
class FileHierarchyStage:
    """One immutable lifecycle stage with its exact recorded opening path."""

    stage_identity: str
    stage_type: str
    label: str
    record_id: int
    file_id: int
    path: str | None
    original_path: str | None
    status: str | None
    is_current_primary: bool
    metadata: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage_identity": self.stage_identity,
            "stage_type": self.stage_type,
            "label": self.label,
            "record_id": self.record_id,
            "file_id": self.file_id,
            "path": self.path,
            "original_path": self.original_path,
            "status": self.status,
            "is_current_primary": self.is_current_primary,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True)
class FileHierarchyStagePage:
    """One bounded page of stages for one exact inventory identity."""

    file_identity: str
    file_id: int
    current_path: str | None
    lifecycle_status: str
    stages: tuple[FileHierarchyStage, ...]
    offset: int
    limit: int
    total_stages: int
    next_offset: int | None
    diagnostics: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "file_identity": self.file_identity,
            "file_id": self.file_id,
            "current_path": self.current_path,
            "lifecycle_status": self.lifecycle_status,
            "stages": [stage.to_payload() for stage in self.stages],
            "offset": self.offset,
            "limit": self.limit,
            "total_stages": self.total_stages,
            "next_offset": self.next_offset,
            "diagnostics": _thaw(self.diagnostics),
        }


@dataclass(frozen=True)
class FileHierarchyFeedSummary:
    """A lightweight feed node containing no primary or stage children."""

    feed_identity: str
    display_label: str
    total_files: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "feed_identity": self.feed_identity,
            "display_label": self.display_label,
            "total_files": self.total_files,
            "files": [],
            "files_loaded": False,
        }


@dataclass(frozen=True)
class FileHierarchyFeedPage:
    """All lightweight feed roots from one strict manifest snapshot."""

    feeds: tuple[FileHierarchyFeedSummary, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "feeds": [feed.to_payload() for feed in self.feeds],
            "offset": 0,
            "limit": _MAX_LIMIT,
            "total_files": sum(feed.total_files for feed in self.feeds),
            "next_offset": None,
            "lazy": True,
        }


@dataclass(frozen=True)
class FileHierarchyFile:
    """One inventoried file and its exact-identity mutation children."""

    file_identity: str
    file_id: int
    display_label: str
    inventory_record_id: int
    path: str | None
    metadata: Mapping[str, Any]
    mutations: tuple[FileHierarchyMutation, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return one JSON-compatible presentation payload."""
        return {
            "file_identity": self.file_identity,
            "file_id": self.file_id,
            "display_label": self.display_label,
            "inventory_record_id": self.inventory_record_id,
            "path": self.path,
            "metadata": _thaw(self.metadata),
            "mutations": [mutation.to_payload() for mutation in self.mutations],
        }


@dataclass(frozen=True)
class FileHierarchyFeed:
    """One exact inventory source group."""

    feed_identity: str
    display_label: str
    files: tuple[FileHierarchyFile, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return one JSON-compatible presentation payload."""
        return {
            "feed_identity": self.feed_identity,
            "display_label": self.display_label,
            "files": [file.to_payload() for file in self.files],
        }


@dataclass(frozen=True)
class FileHierarchyPage:
    """One bounded deterministic page of inventory file nodes."""

    feeds: tuple[FileHierarchyFeed, ...]
    offset: int
    limit: int
    total_files: int
    next_offset: int | None

    def to_payload(self) -> dict[str, Any]:
        """Return the complete JSON-compatible card input model."""
        return {
            "feeds": [feed.to_payload() for feed in self.feeds],
            "offset": self.offset,
            "limit": self.limit,
            "total_files": self.total_files,
            "next_offset": self.next_offset,
        }


@dataclass(frozen=True)
class _InventoryNode:
    feed: str
    record_id: int
    file_id: int
    file_name: str
    path: str | None
    metadata: Mapping[str, Any]


def _mutation_node(record: Mapping[str, Any]) -> FileHierarchyMutation:
    record_id = _positive_int(record.get("record_id"), "source_file_mutation.record_id")
    action = _nonblank(record.get("action"), f"source_file_mutation[{record_id}].action")
    file_data = _file_mapping(record, f"source_file_mutation[{record_id}]")
    return FileHierarchyMutation(
        record_id=record_id,
        label=action.replace("_", " ").title(),
        action=action,
        status=_optional_text(record.get("status"), f"source_file_mutation[{record_id}].status"),
        path=_optional_text(file_data.get("path"), f"source_file_mutation[{record_id}].file.path"),
        original_path=_optional_text(
            file_data.get("original_path"),
            f"source_file_mutation[{record_id}].file.original_path",
        ),
        metadata=_freeze(record),
    )


def _classified_feeds(records: list[Mapping[str, Any]]) -> dict[str, str]:
    """Map each governed file to the feed its current classification declares.

    Classification is an event, not manifest state: it is recorded as a
    ``source_file_classification`` mutation, and reclassifying appends another
    rather than overwriting the first. Both are kept, and the later one is what
    ``control.file_vw`` reports as current -- so this reads the classification
    events and takes the last one per file.

    Feed identity is governed evidence and comes only from
    ``classification.values.feed``. The configured inventory ``source_name`` is
    configuration metadata that names a discovery source, never a feed, so a
    file without a classified feed is reported here as belonging to none. A
    file whose current classification names no feed belongs to none either: the
    latest event is the answer, including when the answer is nothing.

    Args:
        records: The canonical record stream, in the order it was read.

    Returns:
        The feed each classified file belongs to, keyed by governed file
        identity. A file with no current feed is absent.
    """
    # The last classification event per file, in the order the stream carries.
    # `read_records_from_control` emits each file's mutations oldest-first by
    # `file_mutation_id`, which is monotonic and which nothing may reorder, so
    # overwriting as they arrive leaves the current one. Keyed by the record's
    # own `file_id` -- the governed identity -- never by a path or a name,
    # because a file is renamed and moved through its own history.
    current: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.get("record_type") != _CLASSIFICATION:
            continue
        record_id = _positive_int(
            record.get("record_id"), f"{_CLASSIFICATION}.record_id")
        file_id = _identity(
            record.get("file_id"), f"{_CLASSIFICATION}[{record_id}].file_id")
        current[file_id] = record

    feeds: dict[str, str] = {}
    for file_id, record in current.items():
        classification = record.get("classification")
        if not isinstance(classification, Mapping):
            continue
        values = classification.get("values")
        if not isinstance(values, Mapping) or values.get("feed") is None:
            continue
        record_id = _positive_int(
            record.get("record_id"), f"{_CLASSIFICATION}.record_id")
        feeds[file_id] = _nonblank(
            values.get("feed"),
            f"{_CLASSIFICATION}[{record_id}].classification.values.feed",
        )
    return feeds


def _inventory_node(record: Mapping[str, Any], feed: str) -> _InventoryNode:
    record_id = _positive_int(record.get("record_id"), "source_file_inventory.record_id")
    file_data = _file_mapping(record, f"source_file_inventory[{record_id}]")
    return _InventoryNode(
        feed=feed,
        record_id=record_id,
        file_id=_identity(record.get("file_id"), f"source_file_inventory[{record_id}].file_id"),
        file_name=_nonblank(
            file_data.get("file_name"),
            f"source_file_inventory[{record_id}].file.file_name",
        ),
        path=_optional_text(file_data.get("path"), f"source_file_inventory[{record_id}].file.path"),
        metadata=_freeze(record),
    )


def _validated_page(offset: int, limit: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise FileHierarchyError("offset must be a nonnegative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise FileHierarchyError("limit must be a positive integer.")
    if limit > _MAX_LIMIT:
        raise FileHierarchyError(f"limit must not exceed {_MAX_LIMIT}.")
    return offset, limit


def _validated_stage_page(offset: int, limit: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise FileHierarchyError("offset must be a nonnegative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise FileHierarchyError("limit must be a positive integer.")
    if limit > _MAX_STAGE_LIMIT:
        raise FileHierarchyError(f"limit must not exceed {_MAX_STAGE_LIMIT}.")
    return offset, limit


def _read_manifest_records(ctx: Any) -> list[Mapping[str, Any]]:
    """Every governed file with its mutations beneath it."""
    from rey_lib.logs.file_manifest import read_records_from_control

    try:
        return list(read_records_from_control(ctx))
    except FileManifestError as exc:
        raise FileHierarchyError(
            f"File hierarchy could not read the governed file manifest: {exc}"
        ) from exc


def _build_page(
    records: list[Mapping[str, Any]],
    *,
    offset: int,
    limit: int,
) -> FileHierarchyPage:
    # Identity is unique within its own kind. A file and a mutation are rows in
    # separate tables with separate generated keys, so the same number names
    # both a file and one of its own mutations; only a collision inside one kind
    # is a duplicate.
    seen_record_ids: set[tuple[str, int]] = set()
    inventories: list[_InventoryNode] = []
    mutations_by_file: dict[int, list[FileHierarchyMutation]] = {}
    feeds_by_file = _classified_feeds(records)

    for record in records:
        record_type = record.get("record_type")
        if record_type not in {_INVENTORY, _MUTATION}:
            continue
        record_id = _positive_int(record.get("record_id"), f"{record_type}.record_id")
        if (record_type, record_id) in seen_record_ids:
            raise FileHierarchyError(
                f"{record_type} record_id {record_id} is duplicated.")
        seen_record_ids.add((record_type, record_id))
        if record_type == _INVENTORY:
            file_id = _identity(record.get("file_id"), f"{_INVENTORY}[{record_id}].file_id")
            feed = feeds_by_file.get(file_id, "")
            # A file with no classified feed belongs to no feed and is not
            # grouped under one; its lifecycle remains readable by record.
            if feed:
                inventories.append(_inventory_node(record, feed))
            continue
        file_id = _identity(record.get("file_id"), f"source_file_mutation[{record_id}].file_id")
        mutations_by_file.setdefault(file_id, []).append(_mutation_node(record))

    inventories.sort(key=lambda item: (item.feed.casefold(), item.feed, item.record_id))
    for mutations in mutations_by_file.values():
        mutations.sort(key=lambda item: item.record_id)

    total_files = len(inventories)
    selected = inventories[offset : offset + limit]
    grouped: dict[str, list[FileHierarchyFile]] = {}
    for inventory in selected:
        grouped.setdefault(inventory.feed, []).append(
            FileHierarchyFile(
                file_identity=f"inventory:{inventory.record_id}",
                file_id=inventory.file_id,
                display_label=inventory.file_name,
                inventory_record_id=inventory.record_id,
                path=inventory.path,
                metadata=inventory.metadata,
                mutations=tuple(mutations_by_file.get(inventory.file_id, ())),
            )
        )

    feeds = tuple(
        FileHierarchyFeed(feed_identity=feed, display_label=feed, files=tuple(files))
        for feed, files in grouped.items()
    )
    end = offset + len(selected)
    return FileHierarchyPage(
        feeds=feeds,
        offset=offset,
        limit=limit,
        total_files=total_files,
        next_offset=end if end < total_files else None,
    )


def build_file_hierarchy(
    ctx: Any,
    *,
    offset: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> FileHierarchyPage:
    """Read the canonical File Manifest and return one immutable hierarchy page."""
    page_offset, page_limit = _validated_page(offset, limit)
    return _build_page(
        _read_manifest_records(ctx),
        offset=page_offset,
        limit=page_limit,
    )


def build_file_hierarchy_feeds(ctx: Any) -> FileHierarchyFeedPage:
    """Return lightweight feed roots; no primary or stage payload is retained."""
    counts: dict[str, int] = {}
    seen: set[int] = set()
    records = _read_manifest_records(ctx)
    feeds_by_file = _classified_feeds(records)
    for record in records:
        if record.get("record_type") != _INVENTORY:
            continue
        record_id = _positive_int(record.get("record_id"), f"{_INVENTORY}.record_id")
        file_id = _identity(record.get("file_id"), f"{_INVENTORY}[{record_id}].file_id")
        feed = feeds_by_file.get(file_id, "")
        if not feed:
            continue
        node = _inventory_node(record, feed)
        if node.record_id in seen:
            raise FileHierarchyError(f"Manifest record_id {node.record_id} is duplicated.")
        seen.add(node.record_id)
        counts[node.feed] = counts.get(node.feed, 0) + 1
    ordered = sorted(counts, key=lambda value: (value.casefold(), value))
    return FileHierarchyFeedPage(tuple(
        FileHierarchyFeedSummary(feed, feed, counts[feed]) for feed in ordered
    ))


def build_file_hierarchy_feed(
    ctx: Any,
    feed_identity: str,
    *,
    offset: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> FileHierarchyPage:
    """Return one feed's bounded primary page without materialized stages."""
    feed = _nonblank(feed_identity, "feed_identity")
    page_offset, page_limit = _validated_page(offset, limit)
    records = _read_manifest_records(ctx)
    feeds_by_file = _classified_feeds(records)
    selected_files = {
        record.get("file_id")
        for record in records
        if record.get("record_type") == _INVENTORY
        and feeds_by_file.get(record.get("file_id")) == feed
    }
    # The selected files, and the classification events that say which feed
    # they belong to. Both travel: the feed is declared by an event in a file's
    # history, so a selection carrying only inventory records would be handed
    # to `_build_page` with nothing left to group by. Mutations are left out --
    # this page materializes no stages.
    page = _build_page(
        [
            record for record in records
            if record.get("file_id") in selected_files
            and record.get("record_type") in {_INVENTORY, _CLASSIFICATION}
        ],
        offset=page_offset,
        limit=page_limit,
    )
    return page


def _classification_stage(record: Mapping[str, Any],
                          file_id: str) -> FileHierarchyStage:
    """Build the classification stage from the classification event.

    Classifying is something that happens to a file, so it is a record in the
    file's history and the stage is that record. Reclassifying appends another
    event and draws another stage: both happened, and the history says so.

    Args:
        record: The ``source_file_classification`` record.
        file_id: Governed file identity the stage hangs under.

    Returns:
        The stage, carrying the classification the event recorded.
    """
    record_id = _positive_int(
        record.get("record_id"), f"{_CLASSIFICATION}.record_id")
    classification = record.get("classification")
    return FileHierarchyStage(
        stage_identity=f"manifest:{record_id}",
        stage_type="classification",
        label="Classification",
        record_id=record_id,
        file_id=file_id,
        # A classification says what the file *is*; the move that follows says
        # where it went. The event records no path, so neither does the stage.
        path=None,
        original_path=None,
        status=_optional_text(
            record.get("status"), f"{_CLASSIFICATION}[{record_id}].status"),
        is_current_primary=False,
        metadata=_freeze(dict(classification))
        if isinstance(classification, Mapping) else _freeze({}),
    )


# What a mutation is called, keyed by its result -- what the operation
# produced -- with one entry owning both outcomes.
#
# result and status are orthogonal: result says what kind of outcome this
# record is about, status says whether it succeeded. So a failure is not a
# second result value; it is the same entry read through the other form.
#
# This is data, not a decision. Adding an outcome is an entry here, never a
# branch -- recorded as a migrate finding in the rey_lib dispatcher review,
# because a seven-armed chain meant every new reason edited the function that
# was supposed to merely present it.
#
# One table, keyed on one field. There were two -- one on conversion.operator
# and one on result.reason -- tried in order, because the fact they presented
# had two homes. producer.operation gave it one, and the presentation reads
# the outcome rather than the operation that caused it.
_MUTATION_PRESENTATION: dict[str, tuple[str, str, str]] = {
    # result             stage type            success        failure
    "inventoried":          ("inventoried",       "Inventoried",         "Inventory failed"),
    "classified":           ("classified",        "Classified",          "Classification failed"),
    "converted_csv":        ("converted",         "Converted CSV",       "Conversion failed"),
    "sanitized_file":       ("sanitized",         "Sanitized CSV",       "Sanitization failed"),
    "prepared_file":        ("prepared",          "Prepared CSV",        "Preparation failed"),
    "kickout_file":         ("kickout",           "Kickout JSONL",       "Kickout failed"),
    "redacted_kickout_file":   ("kickout_redacted",   "Redacted Kickout JSONL",   "Redacted kickout failed"),
    "redacted_sanitized_file": ("sanitized_redacted", "Redacted Sanitized CSV",   "Redacted sanitization failed"),
    "redacted_prepared_file":  ("prepared_redacted",  "Redacted Prepared CSV",    "Redacted preparation failed"),
    # A profile is shared by every file of its identity, so this node hangs
    # under the file whose profiling run created or appended to it.
    "structural_profile":   ("profile",           "Structural Profile",  "Profiling failed"),
    "moved_to_processing":  ("moved",             "Moved to processing", "Move to processing failed"),
    "moved_to_kickouts":    ("moved",             "Moved to kickouts",   "Move to kickouts failed"),
    "moved_to_failed":      ("moved",             "Moved to failed",     "Move to failed failed"),
    "moved_to_archive":     ("moved",             "Moved to archive",    "Move to archive failed"),
}


def _mutation_stage(record: Mapping[str, Any], file_id: int) -> FileHierarchyStage:
    """Return the hierarchy stage for one governed mutation record.

    Args:
        record: The manifest record.
        file_id: Governed file identity the stage hangs under.

    Returns:
        The stage, named for the artifact it created where one is recognised.
    """
    mutation = _mutation_node(record)
    result = str(record.get("result") or "")

    # One lookup on one field. status selects which form of the entry to show;
    # it is never encoded in the result.
    presentation = _MUTATION_PRESENTATION.get(result)
    if presentation is None:
        stage_type, label = "mutation", mutation.label
    else:
        stage_type, succeeded, failed = presentation
        label = failed if str(mutation.status or "") == "failed" else succeeded

    return FileHierarchyStage(
        stage_identity=f"manifest:{mutation.record_id}",
        stage_type=stage_type,
        label=label,
        record_id=mutation.record_id,
        file_id=file_id,
        path=mutation.path,
        original_path=mutation.original_path,
        status=mutation.status,
        is_current_primary=False,
        metadata=mutation.metadata,
    )


def _rollback_stage(record: Mapping[str, Any], file_id: int) -> FileHierarchyStage:
    record_id = _positive_int(record.get("record_id"), "source_file_rollback.record_id")
    status = _optional_text(record.get("status"), f"source_file_rollback[{record_id}].status")
    return FileHierarchyStage(
        stage_identity=f"manifest:{record_id}", stage_type="rollback", label="Rollback",
        record_id=record_id, file_id=file_id, path=None, original_path=None,
        status=status, is_current_primary=False, metadata=_freeze(record),
    )


def _profile_stage(record: Mapping[str, Any], file_id: int) -> FileHierarchyStage:
    # A run-log record, so its identity is the row's. It was addressed as
    # (log file, ordinal) because the ordinal was file-local; run_log_id needs
    # no file to be unique.
    run_log_id = _positive_int(
        record.get("run_log_id"), "ARTIFACT_REFERENCE.run_log_id")
    path = _nonblank(record.get("path"), "ARTIFACT_REFERENCE.path")
    return FileHierarchyStage(
        stage_identity=f"run-log:{run_log_id}",
        stage_type="profile",
        label="Structural Profile", record_id=run_log_id, file_id=file_id, path=path,
        original_path=None, status="created", is_current_primary=False,
        metadata=_freeze(record),
    )


def build_file_hierarchy_stages(
    ctx: Any,
    inventory_record_id: int,
    *,
    offset: int = 0,
    limit: int = 250,
    profile_artifacts: tuple[Mapping[str, Any], ...] = (),
) -> FileHierarchyStagePage:
    """Return one exact primary's lifecycle page from canonical evidence."""
    inventory_id = _positive_int(inventory_record_id, "inventory_record_id")
    page_offset, page_limit = _validated_stage_page(offset, limit)
    records = _read_manifest_records(ctx)
    ordered_records: list[Mapping[str, Any]] = []
    seen_record_ids: set[int] = set()
    for record in records:
        record_id = _positive_int(record.get("record_id"), "manifest.record_id")
        if record_id in seen_record_ids:
            raise FileHierarchyError(f"Manifest record_id {record_id} is duplicated.")
        seen_record_ids.add(record_id)
        ordered_records.append(record)
    ordered_records.sort(key=lambda item: int(item["record_id"]))
    # Stages are keyed by the exact inventory record, so a file that is not yet
    # classified still resolves its full lifecycle here.
    stage_feeds = _classified_feeds(ordered_records)
    inventories = [
        _inventory_node(record, stage_feeds.get(record.get("file_id"), ""))
        for record in ordered_records
        if record.get("record_type") == _INVENTORY and record.get("record_id") == inventory_id
    ]
    if len(inventories) != 1:
        raise FileHierarchyError(
            f"Inventory record {inventory_id} must resolve to exactly one governed file."
        )
    inventory = inventories[0]
    stages: list[FileHierarchyStage] = [FileHierarchyStage(
        stage_identity=f"manifest:{inventory.record_id}", stage_type="inventory",
        label="Inventory", record_id=inventory.record_id, file_id=inventory.file_id,
        path=inventory.path, original_path=None, status="recorded",
        is_current_primary=False, metadata=inventory.metadata,
    )]
    mutations: dict[int, Mapping[str, Any]] = {}
    successful_rollbacks: set[int] = set()
    node_paths: dict[str, str | None] = {"primary": inventory.path}
    mutation_targets: dict[int, str] = {}
    lifecycle_status = "active"
    contradictory = 0
    for record in ordered_records:
        record_type = record.get("record_type")
        # Classification is an event in this file's history, so its stage is
        # drawn where the event falls rather than spliced in ahead of the
        # history. The stream is ordered by record identity, so a reclassified
        # file shows both events in the order they happened.
        if record_type == _CLASSIFICATION and record.get("file_id") == inventory.file_id:
            stages.append(_classification_stage(record, inventory.file_id))
            continue
        if record_type == _MUTATION and record.get("file_id") == inventory.file_id:
            record_id = _positive_int(record.get("record_id"), "source_file_mutation.record_id")
            mutations[record_id] = record
            stages.append(_mutation_stage(record, inventory.file_id))
            # A reversed mutation stays in the history and stays on the page,
            # but it no longer moves the file: its effect was undone.
            if record.get("rollback_complete_in"):
                continue
            if record.get("status") != "success":
                continue
            action = record.get("action")
            file_data = _file_mapping(record, f"source_file_mutation[{record_id}]")
            path = file_data.get("path")
            original = file_data.get("original_path")
            if action in {"move", "delete"}:
                matches = [key for key, current in node_paths.items() if current == original]
                if len(matches) != 1 or (action == "move" and not isinstance(path, str)):
                    contradictory += 1
                    if "primary" in matches:
                        lifecycle_status = "contradictory"
                    continue
                target = matches[0]
                mutation_targets[record_id] = target
                node_paths[target] = path if action == "move" else None
                if target == "primary":
                    lifecycle_status = "active" if action == "move" else "deleted"
            elif action == "create":
                if not isinstance(path, str):
                    contradictory += 1
                    continue
                target = f"derived:{record_id}"
                mutation_targets[record_id] = target
                node_paths[target] = path
            elif action == "replace":
                matches = [key for key, current in node_paths.items() if current == path]
                target = matches[0] if len(matches) == 1 else f"derived:{record_id}"
                mutation_targets[record_id] = target
                node_paths.setdefault(target, path if isinstance(path, str) else None)
        elif record_type == "source_file_rollback":
            rollback = record.get("rollback")
            if isinstance(rollback, Mapping) and rollback.get("original_record_id") in mutations:
                stages.append(_rollback_stage(record, inventory.file_id))
                if record.get("status") != "success" or rollback.get("phase") != "final":
                    continue
                original_id = _positive_int(rollback.get("original_record_id"), "rollback.original_record_id")
                if original_id in successful_rollbacks:
                    contradictory += 1
                    lifecycle_status = "contradictory"
                    continue
                successful_rollbacks.add(original_id)
                original_record = mutations[original_id]
                file_data = _file_mapping(original_record, f"source_file_mutation[{original_id}]")
                action = original_record.get("action")
                target = mutation_targets.get(original_id)
                if target is None:
                    contradictory += 1
                    continue
                expected = file_data.get("path") if action in {"move", "create", "replace"} else None
                if action == "move" and node_paths.get(target) == expected:
                    node_paths[target] = file_data.get("original_path")
                elif action == "create" and node_paths.get(target) == expected:
                    node_paths[target] = None
                elif action == "delete" and node_paths.get(target) is None:
                    node_paths[target] = file_data.get("original_path")
                elif action == "replace" and node_paths.get(target) == expected:
                    pass
                else:
                    contradictory += 1
                    if target == "primary":
                        lifecycle_status = "contradictory"
                    continue
                if target == "primary":
                    lifecycle_status = "deleted" if node_paths["primary"] is None else "active"

    for artifact in profile_artifacts:
        if artifact.get("source_file_id") != inventory.file_id:
            raise FileHierarchyError("Profile artifact source_file_id does not match the governed file.")
        stages.append(_profile_stage(artifact, inventory.file_id))
    stages.sort(key=lambda stage: (
        int(stage.metadata.get("__anchor_manifest_record_id", stage.record_id)),
        1 if stage.stage_type == "profile" else 0,
        stage.record_id,
        stage.stage_identity,
    ))
    total = len(stages)
    selected = tuple(stages[page_offset:page_offset + page_limit])
    end = page_offset + len(selected)
    profile_references: list[dict[str, Any]] = []
    for stage in stages:
        evidence = stage.metadata.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        run_log_id = evidence.get("run_log_id")
        # One key. The log record was addressed as (file, ordinal) while the
        # ordinal was file-local; run_log_id is the row itself.
        if isinstance(run_log_id, int) and run_log_id > 0:
            profile_references.append({
                "manifest_record_id": stage.record_id,
                "run_log_id": run_log_id,
            })
    return FileHierarchyStagePage(
        file_identity=f"inventory:{inventory.record_id}", file_id=inventory.file_id,
        current_path=node_paths["primary"], lifecycle_status=lifecycle_status, stages=selected,
        offset=page_offset, limit=page_limit, total_stages=total,
        next_offset=end if end < total else None,
        diagnostics=_freeze({
            "manifest_records_scanned": len(records), "stages_matched": total,
            "stages_returned": len(selected), "next_offset": end if end < total else None,
            "unavailable_or_contradictory_evidence_count": contradictory,
            "profile_run_log_references": profile_references,
        }),
    )
