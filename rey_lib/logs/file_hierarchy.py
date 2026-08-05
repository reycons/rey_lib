"""Immutable File Manifest hierarchy projection.

Phase 1 projects configured source groups, inventoried files, and mutations.
Phase 2 adds bounded, lazy lifecycle queries.  The module still owns no
filesystem, run-log, artifact discovery, or UI behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping

from rey_lib.logs.file_manifest import FileManifestError, resolve_file_manifest_path

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
    file_id: str
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
    file_id: str
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
    file_id: str
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
    file_id: str
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
    """Map each governed file to the feed its classification record declares.

    Feed identity is governed evidence and comes only from
    ``classification.values.feed``. The configured inventory ``source_name`` is
    configuration metadata that names a discovery source, never a feed, so a
    file without a classified feed is reported here as belonging to none.
    """
    feeds: dict[str, str] = {}
    for record in records:
        if record.get("record_type") != _CLASSIFICATION:
            continue
        record_id = _positive_int(record.get("record_id"), f"{_CLASSIFICATION}.record_id")
        classification = record.get("classification")
        if not isinstance(classification, Mapping):
            continue
        values = classification.get("values")
        if not isinstance(values, Mapping) or values.get("feed") is None:
            continue
        file_id = _nonblank(record.get("file_id"), f"{_CLASSIFICATION}[{record_id}].file_id")
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
        file_id=_nonblank(record.get("file_id"), f"source_file_inventory[{record_id}].file_id"),
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
    from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file

    try:
        manifest_path = resolve_file_manifest_path(ctx)
        rows = read_jsonl_file(manifest_path)
    except (FileManifestError, JsonlReadError) as exc:
        raise FileHierarchyError(f"File hierarchy could not read the canonical manifest: {exc}") from exc
    return [row.record for row in rows]


def _build_page(
    records: list[Mapping[str, Any]],
    *,
    offset: int,
    limit: int,
) -> FileHierarchyPage:
    seen_record_ids: set[int] = set()
    inventories: list[_InventoryNode] = []
    mutations_by_file: dict[str, list[FileHierarchyMutation]] = {}
    feeds_by_file = _classified_feeds(records)

    for record in records:
        record_type = record.get("record_type")
        if record_type not in {_INVENTORY, _MUTATION}:
            continue
        record_id = _positive_int(record.get("record_id"), f"{record_type}.record_id")
        if record_id in seen_record_ids:
            raise FileHierarchyError(f"Manifest record_id {record_id} is duplicated.")
        seen_record_ids.add(record_id)
        if record_type == _INVENTORY:
            file_id = _nonblank(record.get("file_id"), f"{_INVENTORY}[{record_id}].file_id")
            feed = feeds_by_file.get(file_id, "")
            # A file with no classified feed belongs to no feed and is not
            # grouped under one; its lifecycle remains readable by record.
            if feed:
                inventories.append(_inventory_node(record, feed))
            continue
        file_id = _nonblank(record.get("file_id"), f"source_file_mutation[{record_id}].file_id")
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
        file_id = _nonblank(record.get("file_id"), f"{_INVENTORY}[{record_id}].file_id")
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
    selected_ids = {
        _positive_int(record.get("record_id"), "source_file_inventory.record_id")
        for record in records
        if record.get("record_type") == _INVENTORY
        and feeds_by_file.get(str(record.get("file_id") or "")) == feed
    }
    # Classification records travel with the selection: the page groups by
    # classified feed, so it must still see the evidence that declares it.
    page = _build_page(
        [
            record for record in records
            if record.get("record_type") == _CLASSIFICATION
            or (
                record.get("record_type") == _INVENTORY
                and _positive_int(record.get("record_id"), "source_file_inventory.record_id") in selected_ids
            )
        ],
        offset=page_offset,
        limit=page_limit,
    )
    return page


def _classification_stage(record: Mapping[str, Any], file_id: str) -> FileHierarchyStage:
    record_id = _positive_int(record.get("record_id"), "source_file_classification.record_id")
    file_data = _file_mapping(record, f"source_file_classification[{record_id}]")
    return FileHierarchyStage(
        stage_identity=f"manifest:{record_id}",
        stage_type="classification",
        label="Classification",
        record_id=record_id,
        file_id=file_id,
        path=_optional_text(file_data.get("path"), f"source_file_classification[{record_id}].file.path"),
        original_path=None,
        status=_optional_text(record.get("status"), f"source_file_classification[{record_id}].status"),
        is_current_primary=False,
        metadata=_freeze(record),
    )


def _mutation_stage(record: Mapping[str, Any], file_id: str) -> FileHierarchyStage:
    mutation = _mutation_node(record)
    conversion = record.get("conversion") if isinstance(record.get("conversion"), Mapping) else {}
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    # A created artifact is named for what it is, so a node says exactly what
    # opens when it is clicked. Anything else stays a generic lifecycle event.
    if conversion.get("operator") == "excel_conversion":
        stage_type, label = "converted", "Converted CSV"
    elif result.get("reason") == "file_sanitization":
        stage_type, label = "sanitized", "Sanitized CSV"
    elif result.get("reason") == "prepared_file":
        stage_type, label = "prepared", "Prepared CSV"
    elif result.get("reason") == "kickout_file":
        stage_type, label = "kickout", "Kickout JSONL"
    elif result.get("reason") == "redacted_kickout_file":
        stage_type, label = "kickout_redacted", "Redacted Kickout JSONL"
    elif result.get("reason") == "redacted_sanitized_file":
        stage_type, label = "sanitized_redacted", "Redacted Sanitized CSV"
    elif result.get("reason") == "redacted_prepared_file":
        stage_type, label = "prepared_redacted", "Redacted Prepared CSV"
    elif result.get("reason") == "structural_profile":
        # A profile is shared by every file of its identity, so this node hangs
        # under the file whose profiling run created or appended to it.
        stage_type, label = "profile", "Structural Profile"
    else:
        stage_type, label = "mutation", mutation.label
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


def _rollback_stage(record: Mapping[str, Any], file_id: str) -> FileHierarchyStage:
    record_id = _positive_int(record.get("record_id"), "source_file_rollback.record_id")
    status = _optional_text(record.get("status"), f"source_file_rollback[{record_id}].status")
    return FileHierarchyStage(
        stage_identity=f"manifest:{record_id}", stage_type="rollback", label="Rollback",
        record_id=record_id, file_id=file_id, path=None, original_path=None,
        status=status, is_current_primary=False, metadata=_freeze(record),
    )


def _profile_stage(record: Mapping[str, Any], file_id: str) -> FileHierarchyStage:
    record_id = _positive_int(record.get("record_id"), "ARTIFACT_REFERENCE.record_id")
    run_log = _nonblank(record.get("__run_log_file"), "ARTIFACT_REFERENCE.__run_log_file")
    path = _nonblank(record.get("path"), "ARTIFACT_REFERENCE.path")
    return FileHierarchyStage(
        stage_identity=f"run-log:{sha256(run_log.encode('utf-8')).hexdigest()}:{record_id}",
        stage_type="profile",
        label="Structural Profile", record_id=record_id, file_id=file_id, path=path,
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
        _inventory_node(record, stage_feeds.get(str(record.get("file_id") or ""), ""))
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
        if record_type == "source_file_classification" and record.get("file_id") == inventory.file_id:
            lineage = record.get("lineage")
            if isinstance(lineage, Mapping) and lineage.get("source_record_id") == inventory.record_id:
                stages.append(_classification_stage(record, inventory.file_id))
        elif record_type == _MUTATION and record.get("file_id") == inventory.file_id:
            record_id = _positive_int(record.get("record_id"), "source_file_mutation.record_id")
            mutations[record_id] = record
            stages.append(_mutation_stage(record, inventory.file_id))
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
        run_log_file = evidence.get("run_log_file")
        run_log_record_id = evidence.get("run_log_record_id")
        if isinstance(run_log_file, str) and run_log_file.strip() and isinstance(run_log_record_id, int) and run_log_record_id > 0:
            profile_references.append({
                "manifest_record_id": stage.record_id,
                "run_log_file": run_log_file,
                "run_log_record_id": run_log_record_id,
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
