"""The DDL export: which database objects, in what order, written where (backlog 723).

``DatabaseDdlExport`` owns export orchestration and nothing else. What a
database holds arrives as ``DatabaseObjectListing`` -- every entry of the
provider's inventory already claimed by exactly one canonical database object
-- and each object's DDL is its own ``read_ddl``. This decides which objects
are exported, checks that the export is coherent, orders it by dependency, and
writes the object files, the build manifest and build.sql, cleaning up what a
previous export left behind.

It writes no SQL, holds no structural model of its own, and never reaches
DBAdapter or a provider: an adapter it is handed is passed to the database
objects, which use it, and is otherwise unread.
"""

from __future__ import annotations

import re
from typing import Any

from rey_lib.db.database_objects import (
    DatabaseObjectListing,
    DatabaseObjectReference,
    DatabaseTable,
)
from rey_lib.db.object_filter import apply_object_filter
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.files import (
    cleanup_stale_files,
    export_build_manifest_path,
    export_build_sql_path,
    export_db_root,
    export_object_file_path,
    export_relative_posix,
    write_file,
)

__all__ = ["DatabaseDdlExport"]

#: The order kinds are built in where nothing else decides between two objects.
_BUILD_ORDER: list[str] = [
    "schemas",
    "types",
    "sequences",
    "tables",
    "constraints",
    "indexes",
    "views",
    "functions",
    "procedures",
    "triggers",
]


class DatabaseDdlExport:
    """Export one database's DDL into deterministic object files and build artifacts."""

    @classmethod
    def export(
        cls,
        conn: Any,
        *,
        output_root: str,
        database: str | None = None,
        validate_only: bool = False,
        cleanup_stale: bool = True,
        object_filter: dict[str, Any] | None = None,
        adapter: Any = None,
    ) -> dict[str, Any]:
        """Export one database into deterministic object files and build artifacts.

        When ``object_filter`` is supplied (SGC Prerequisites P1) only the
        selected objects are exported; the whole-database build artifacts and
        stale-file cleanup are skipped so unselected objects are left untouched.
        With ``object_filter=None`` the whole database is exported.

        Raises:
            DatabaseError: When an inventory entry has no single owner, the
                export is incoherent (duplicates, missing or invalid
                dependencies, a cycle), or an object answers with no DDL.
        """
        listing = DatabaseObjectListing.read(conn, database, adapter)
        entries = _entries(listing)
        # What a table's creation statement already creates is not emitted again
        # as an object of its own: the table states it, this export decides
        # (backlog 695).
        suppressed = set()
        if DatabaseTable.create_includes_constraints(conn, adapter):
            suppressed.add("constraints")
        if DatabaseTable.create_includes_indexes(conn, adapter):
            suppressed.add("indexes")
        entries = [entry for entry in entries if entry["object_type"] not in suppressed]
        validation = _validate(entries)
        if validation["errors"]:
            raise DatabaseError(
                "DDL export validation failed: " + "; ".join(validation["errors"])
            )

        ordered = _order(entries)

        # P1 object selection: filter the ordered set after validation/ordering
        # so dependency checks run on the full database; only selected objects
        # are written. Whole-database artifacts and cleanup are skipped below so
        # unselected objects are never touched.
        filtered = bool(object_filter)
        if filtered:
            ordered = apply_object_filter(ordered, object_filter)

        db_root = export_db_root(output_root, listing.provider, listing.database)
        desired_files: set[str] = set()
        ordered_ddls: list[tuple[dict[str, Any], str, str]] = []

        for entry in ordered:
            if validate_only:
                continue
            # EVERY OBJECT WRITES ITS OWN DDL (backlog 695, 723): asked of the
            # object itself, which addresses its own read_ddl.
            ddl = entry["object"].read_ddl(conn, adapter)
            if not isinstance(ddl, str) or not ddl.strip():
                raise DatabaseError(
                    f"Provider '{listing.provider}' returned empty DDL for {entry['key']}."
                )
            file_path = export_object_file_path(
                db_root, entry["schema"], entry["object_type"], entry["file_name"],
            )
            write_file(file_path, ddl.rstrip() + "\n", "TEXT")
            desired_files.add(str(file_path.resolve()))
            ordered_ddls.append((entry, ddl.rstrip(), str(file_path)))

        build_manifest = _manifest(listing, ordered, db_root)
        build_manifest_path = export_build_manifest_path(db_root)
        build_sql_path = export_build_sql_path(db_root)

        # Whole-database aggregates and stale cleanup are only written for a full
        # export. A filtered (P1) export touches just the selected object files,
        # leaving the manifest, build.sql, and unselected files unchanged.
        removed: list[Any] = []
        if not validate_only and not filtered:
            write_file(build_manifest_path, build_manifest, "JSON")
            write_file(build_sql_path, _build_sql(db_root, ordered_ddls), "TEXT")
            desired_files.add(str(build_manifest_path.resolve()))
            desired_files.add(str(build_sql_path.resolve()))
            if cleanup_stale:
                removed = cleanup_stale_files(db_root, desired_files)

        return {
            "provider": listing.provider,
            "database": listing.database,
            "validate_only": validate_only,
            "filtered": filtered,
            "object_count": len(ordered),
            "objects": [entry["key"] for entry in ordered],
            "build_manifest": build_manifest,
            "removed_stale_files": removed,
        }

    @classmethod
    def validate(
        cls, conn: Any, *, database: str | None = None, adapter: Any = None,
    ) -> dict[str, Any]:
        """Validate the export and its dependency graph without writing files.

        An inventory entry no canonical object owns -- or that several claim --
        makes the export invalid, and is refused by the listing before any
        finding is collected (backlog 723).
        """
        listing = DatabaseObjectListing.read(conn, database, adapter)
        entries = _entries(listing)
        validation = _validate(entries)
        if not validation["errors"]:
            _order(entries)
        return {"provider": listing.provider, "database": listing.database, **validation}


def _entries(listing: DatabaseObjectListing) -> list[dict[str, Any]]:
    """One export entry per listed object: its key, file and dependency keys."""
    entries = []
    for obj in listing.objects:
        reference = obj.reference
        entries.append({
            "object": obj,
            "key": _key(reference),
            "object_type": reference.kind,
            "schema": reference.schema,
            "name": reference.name,
            "file_name": f"{reference.schema}.{_safe_name(reference.name)}.sql",
            "dependencies": [_key(dependency) for dependency in obj.depends_on],
        })
    return sorted(
        entries,
        key=lambda one: (one["object_type"], one["schema"], one["name"], one["key"]),
    )


def _key(reference: DatabaseObjectReference) -> str:
    """The export's key for a reference; empty where the reference states no object."""
    if not (reference.kind and reference.name):
        return ""
    return f"{reference.kind}:{reference.schema}.{reference.name}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _validate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the export is coherent: each object once, every dependency present."""
    duplicates: list[str] = []
    invalid_references: list[str] = []
    missing_dependencies: list[str] = []
    errors: list[str] = []

    seen: set[str] = set()
    keys = {entry["key"] for entry in entries}

    for entry in entries:
        if entry["key"] in seen:
            duplicates.append(entry["key"])
        seen.add(entry["key"])
        for dep_key in entry["dependencies"]:
            if not dep_key:
                invalid_references.append(f"{entry['key']} -> <invalid dependency>")
                continue
            if dep_key not in keys:
                missing_dependencies.append(f"{entry['key']} -> {dep_key}")

    if duplicates:
        errors.append(f"duplicate object definitions: {', '.join(sorted(set(duplicates)))}")
    if missing_dependencies:
        errors.append(f"missing dependencies: {', '.join(sorted(set(missing_dependencies)))}")
    if invalid_references:
        errors.append(
            f"invalid dependency references: {', '.join(sorted(set(invalid_references)))}"
        )

    return {
        "errors": errors,
        "duplicates": sorted(set(duplicates)),
        "missing_dependencies": sorted(set(missing_dependencies)),
        "invalid_references": sorted(set(invalid_references)),
    }


def _order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dependencies first; otherwise build order, then schema and name.

    Raises:
        DatabaseError: Naming the objects in a dependency cycle.
    """
    nodes = {entry["key"]: entry for entry in entries}
    incoming: dict[str, int] = {key: 0 for key in nodes}
    outgoing: dict[str, list[str]] = {key: [] for key in nodes}

    for entry in entries:
        for dep_key in entry["dependencies"]:
            if dep_key in nodes:
                outgoing[dep_key].append(entry["key"])
                incoming[entry["key"]] += 1

    ordered: list[dict[str, Any]] = []
    ready = [key for key, count in incoming.items() if count == 0]
    ready.sort(key=lambda key: _sort_key(nodes[key]))

    while ready:
        key = ready.pop(0)
        ordered.append(nodes[key])
        for nxt in sorted(outgoing[key], key=lambda k: _sort_key(nodes[k])):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda k: _sort_key(nodes[k]))

    if len(ordered) != len(entries):
        cycle = sorted(key for key, count in incoming.items() if count > 0)
        raise DatabaseError("Circular dependencies detected: " + ", ".join(cycle))

    return ordered


def _sort_key(entry: dict[str, Any]) -> tuple[int, str, str, str]:
    try:
        index = _BUILD_ORDER.index(entry.get("object_type", "tables"))
    except ValueError:
        index = len(_BUILD_ORDER)
    return (
        index, str(entry.get("schema", "")), str(entry.get("name", "")),
        str(entry.get("key", "")),
    )


def _manifest(
    listing: DatabaseObjectListing, ordered: list[dict[str, Any]], db_root: Any,
) -> dict[str, Any]:
    """The build manifest: every exported object, its file and its dependencies."""
    return {
        "version": 1,
        "provider": listing.provider,
        "database": listing.database,
        "build_order": _BUILD_ORDER,
        "objects": [
            {
                "key": entry["key"],
                "object_type": entry["object_type"],
                "schema": entry["schema"],
                "name": entry["name"],
                "file": f"{entry['schema']}/{entry['object_type']}/{entry['file_name']}",
                "dependencies": [key for key in entry["dependencies"] if key],
            }
            for entry in ordered
        ],
        "paths": {
            "build_manifest": str(export_build_manifest_path(db_root).as_posix()),
            "build_database_sql": str(export_build_sql_path(db_root).as_posix()),
        },
    }


def _build_sql(db_root: Any, ordered_ddls: list[tuple[dict[str, Any], str, str]]) -> str:
    """build.sql: every exported object's DDL, in build order, each naming its file."""
    blocks: list[str] = []
    for entry, ddl, file_path in ordered_ddls:
        rel = export_relative_posix(file_path, db_root)
        blocks.append("\n".join([f"-- {entry['key']}", f"-- source: {rel}", ddl]))
    return "\n\n".join(blocks).rstrip() + "\n"
