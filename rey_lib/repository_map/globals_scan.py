"""Global publication and consumer discovery for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-003, REQ-060 to REQ-062).

A classic script that publishes onto the global object and a module that reads
it have no import between them, so this is the evidence that keeps such a file
from looking unreferenced. Publications and consumers are recorded separately,
and the unmatched ones on either side are reported rather than resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rey_lib.repository_map.js_extractor import (
    js_dotted_name,
    js_is_global_rooted,
    js_text,
    parse_js_root,
    supported_js_languages,
    walk_js_nodes,
)
from rey_lib.repository_map.records import (
    ACCESS_KIND_BRACKET_ACCESS,
    ACCESS_KIND_CALL,
    ACCESS_KIND_OPTIONAL_CALL,
    ACCESS_KIND_PROPERTY_ACCESS,
    ACCESS_KIND_TYPEOF,
    FileRecord,
    GlobalConsumerRecord,
    GlobalPublicationRecord,
    ScanRules,
)

__all__ = ["GlobalReport", "extract_global_publications_and_consumers"]


@dataclass(frozen=True)
class GlobalReport:
    """Publications and consumers of the global object, and their mismatches.

    Attributes:
        publications: Every assignment onto the global object.
        consumers: Every executable read of the global object.
        published_without_consumer: Globals published but never consumed.
        consumed_without_publication: Globals consumed but never published in
            this repository. These are not errors: the publisher may be a
            vendored library or another repository.
    """

    publications: tuple[GlobalPublicationRecord, ...]
    consumers: tuple[GlobalConsumerRecord, ...]
    published_without_consumer: tuple[str, ...]
    consumed_without_publication: tuple[str, ...]


def extract_global_publications_and_consumers(
    repo_root: Path,
    files: list[FileRecord],
    rules: ScanRules,
) -> GlobalReport:
    """Extract global publications and consumers across a repository.

    Args:
        repo_root: Repository root the file paths are relative to.
        files: The inventoried files to scan.
        rules: The scanned repository's own scan rules.

    Returns:
        The report, with both sides sorted and mismatches listed.
    """
    js_languages = set(supported_js_languages())
    publications: list[GlobalPublicationRecord] = []
    consumers: list[GlobalConsumerRecord] = []

    for file_record in files:
        if file_record.language not in js_languages:
            continue
        if not rules.extracts_facts_from(file_record):
            continue
        found_publications, found_consumers = _scan_file(
            repo_root / file_record.path, file_record
        )
        publications.extend(found_publications)
        consumers.extend(found_consumers)

    publications.sort(key=lambda record: (record.source_path, record.source_line))
    consumers.sort(
        key=lambda record: (record.source_path, record.source_line, record.source_column)
    )

    published = {record.global_name for record in publications}
    # A consumer of window.A.b depends on the publication of window.A, so a
    # consumed name matches when it or any of its prefixes is published.
    consumed_roots = {_matched_prefix(record.global_name, published) for record in consumers}
    return GlobalReport(
        publications=tuple(publications),
        consumers=tuple(consumers),
        published_without_consumer=tuple(sorted(published - consumed_roots)),
        consumed_without_publication=tuple(
            sorted(
                {
                    record.global_name
                    for record in consumers
                    if _matched_prefix(record.global_name, published) is None
                }
            )
        ),
    )


def _scan_file(
    path: Path,
    file_record: FileRecord,
) -> tuple[list[GlobalPublicationRecord], list[GlobalConsumerRecord]]:
    """Return the publications and consumers in one JS/TS/TSX file.

    Args:
        path: Absolute path to the file.
        file_record: The file's inventory record.

    Returns:
        The publications and consumers found.
    """
    root = parse_js_root(path, file_record.language)
    nodes = walk_js_nodes(root)

    publications: list[GlobalPublicationRecord] = []
    publication_ids: set[int] = set()
    for node in nodes:
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or left.type != "member_expression":
            continue
        name = js_dotted_name(left)
        if name is None or not js_is_global_rooted(name):
            continue
        publication_ids.add(left.id)
        line, column = left.start_point
        publications.append(
            GlobalPublicationRecord(
                source_path=file_record.path,
                source_line=line + 1,
                source_column=column,
                global_name=name,
                implementation=js_text(right).splitlines()[0] if right is not None else "",
            )
        )

    consumers = _consumers(nodes, file_record.path, publication_ids)
    return publications, consumers


def _consumers(
    nodes: list,
    source_path: str,
    publication_ids: set[int],
) -> list[GlobalConsumerRecord]:
    """Return every executable consumer of the global object.

    Args:
        nodes: All named nodes in the file.
        source_path: Path to record.
        publication_ids: Node ids that are publication targets, which are
            declarations rather than uses.

    Returns:
        The consumer records.
    """
    callee_ids = {
        callee.id
        for node in nodes
        if node.type in {"call_expression", "new_expression"}
        and (callee := node.child_by_field_name("function")) is not None
    }
    optional_callee_ids = {
        callee.id
        for node in nodes
        if node.type == "call_expression"
        and node.child_by_field_name("optional_chain") is not None
        and (callee := node.child_by_field_name("function")) is not None
    }

    consumers: list[GlobalConsumerRecord] = []
    for node in nodes:
        if node.type == "subscript_expression":
            name = js_dotted_name(node.child_by_field_name("object"))
            if name is not None and js_is_global_rooted(name):
                consumers.append(
                    _consumer(source_path, node, name, ACCESS_KIND_BRACKET_ACCESS)
                )
            continue
        if node.type == "unary_expression" and js_text(node).startswith("typeof"):
            argument = node.child_by_field_name("argument")
            name = js_dotted_name(argument) if argument is not None else None
            if name is not None and js_is_global_rooted(name):
                consumers.append(_consumer(source_path, node, name, ACCESS_KIND_TYPEOF))
            continue
        if node.type != "member_expression" or node.id in publication_ids:
            continue
        name = js_dotted_name(node)
        if name is None or not js_is_global_rooted(name):
            continue
        # Only the outermost chain is a consumer; window.A.b is one use.
        if node.parent is not None and node.parent.type == "member_expression":
            continue
        if node.id in optional_callee_ids:
            access_kind = ACCESS_KIND_OPTIONAL_CALL
        elif node.id in callee_ids:
            access_kind = ACCESS_KIND_CALL
        else:
            access_kind = ACCESS_KIND_PROPERTY_ACCESS
        consumers.append(_consumer(source_path, node, name, access_kind))
    return consumers


def _consumer(
    source_path: str,
    node,
    global_name: str,
    access_kind: str,
) -> GlobalConsumerRecord:
    """Build one consumer record located at a syntax node.

    Args:
        source_path: Path to record.
        node: The node proving the consumption.
        global_name: The consumed global.
        access_kind: One of the ``ACCESS_KIND_*`` constants.

    Returns:
        The consumer record.
    """
    line, column = node.start_point
    return GlobalConsumerRecord(
        source_path=source_path,
        source_line=line + 1,
        source_column=column,
        global_name=global_name,
        access_kind=access_kind,
    )


def _matched_prefix(name: str, published: set[str]) -> str | None:
    """Return the published global a consumed name depends on.

    Args:
        name: The consumed dotted name.
        published: Every published global name.

    Returns:
        The longest published prefix of the name, or None when none is
        published in this repository.
    """
    parts = name.split(".")
    for end in range(len(parts), 1, -1):
        candidate = ".".join(parts[:end])
        if candidate in published:
            return candidate
    return None
