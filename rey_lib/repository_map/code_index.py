"""Writing one snapshot into the relational code index.

The second destination for a ``CodeIndexSnapshot``. The committed JSONL is the
first, and both are written from the same value, so neither can describe a scan
the other did not see.

**The engine does not open a connection.** ``index`` is handed a
:class:`CodeIndexWriter` and knows nothing about providers, credentials or
installations; the application resolves one and passes it down. A library that
found its own database would make a scan impossible without one, and the
committed artifact has to stay reproducible from a bare checkout.

**Indexing replaces the whole index, it does not merge.** The estate is always
scanned as one snapshot, so a partial replacement would need someone to decide
which repositories are still current -- and that is the scan's answer, not a
caller's. A file deleted or renamed in the source therefore cannot survive.

**Atomicity belongs to the destination, not here.** One call hands over
everything, and what that call does with it -- a transaction, a promotion, a
file rewrite -- is the destination's business. This module never sequences a
wipe and a repopulate, because a caller that has to do that in order can be
interrupted between them.

Nothing here is authored. Every row is derived from a scan, which is what lets
a rebuild from source be the recovery path and lets the export prove the model.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import (
    RECORD_TYPE_DEPENDENCY_EDGE,
    RECORD_TYPE_FILE,
    RECORD_TYPE_SYMBOL,
)
from rey_lib.repository_map.snapshot import CodeIndexSnapshot
from rey_lib.repository_map.writer import RepositoryMap

__all__ = ["CodeIndexWriter", "IndexedRepository", "index"]

logger = get_logger(__name__)


@runtime_checkable
class CodeIndexWriter(Protocol):
    """What indexing needs a destination to be able to do.

    One method, taking one repository's whole indexed state. Everything a
    provider decides -- how the replacement is performed, what a transaction
    is, which driver is underneath -- is behind it.
    """

    def replace_index(self, indexed: list["IndexedRepository"]) -> None:
        """Replace the whole index, all or nothing.

        Args:
            indexed: Every repository the scan observed. Not a delta and not a
                subset: what is absent here is absent from the index.
        """


class IndexedRepository:
    """One repository's indexed state, as rows rather than records.

    The shape the destination receives: the header's own facts, and the files
    and symbols beneath it with symbols already grouped by the file that
    declares them. Doing the grouping here means a destination never re-derives
    the relationship, and two destinations cannot disagree about it.

    Attributes:
        repository: The repository key, which is the declared member name.
        header: The repository's observed state -- revision, branch, working
            tree status, hashes.
        files: One entry per file, each carrying its own symbol rows.
        edges: One entry per proved reference, naming the file it was written
            in. Kept beside the files rather than under them: an edge is
            attributed to a file but is not a property of one, and grouping it
            underneath would suggest the extractor resolved an owner it did
            not.
    """

    __slots__ = ("repository", "header", "files", "edges")

    def __init__(
        self,
        repository: str,
        header: dict[str, Any],
        files: list[dict[str, Any]],
        edges: list[dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.header = header
        self.files = files
        self.edges = edges if edges is not None else []


def index(snapshot: CodeIndexSnapshot, writer: CodeIndexWriter) -> int:
    """Write one snapshot to one destination, as one replacement.

    Args:
        snapshot: What to write.
        writer: Where to write it.

    Returns:
        How many repositories were handed over.
    """
    indexed = [
        _indexed(repository, repository_map)
        for repository, repository_map in snapshot.maps.items()
    ]
    writer.replace_index(indexed)
    logger.info("Indexed %d repositories", len(indexed))
    return len(indexed)


def _indexed(repository: str, repository_map: RepositoryMap) -> IndexedRepository:
    """Return one map as the rows a destination receives.

    A symbol names the file it is declared in by ``source_path``, and the
    inventory already holds that file, so grouping is a lookup rather than a
    join. A symbol naming a path the inventory does not carry is a generator
    defect: it is reported rather than written under an invented file.

    Args:
        repository: The declared member name.
        repository_map: The map this scan produced for it.

    Returns:
        The repository's whole indexed state.
    """
    files: dict[str, dict[str, Any]] = {}
    for record in repository_map.records:
        if record["record_type"] != RECORD_TYPE_FILE:
            continue
        classification = record["classification"]
        files[record["path"]] = {
            "relative_path": record["path"],
            "language": record["language"],
            "content_hash": record["content_hash"],
            "is_generated": classification["generated"],
            "is_vendor": classification["vendor"],
            "is_test": classification["test"],
            "symbols": [],
        }

    for record in repository_map.records:
        if record["record_type"] != RECORD_TYPE_SYMBOL:
            continue
        declared_in = files.get(record["source_path"])
        if declared_in is None:
            logger.warning(
                "%s: symbol %s names file %s, which the inventory does not carry",
                repository,
                record["qualified_name"],
                record["source_path"],
            )
            continue
        declared_in["symbols"].append(
            {
                "symbol_kind": record["symbol_kind"],
                "name": record["name"],
                "qualified_name": record["qualified_name"],
                "owner": record["owner"],
                "is_public": record["exported"],
                "start_line": record["source_line"],
                "start_column": record["source_column"],
                "end_line": record["end_line"],
                "end_column": record["end_column"],
                "dotted_identity": record["dotted_identity"],
            }
        )

    edges: list[dict[str, Any]] = []
    for record in repository_map.records:
        if record["record_type"] != RECORD_TYPE_DEPENDENCY_EDGE:
            continue
        if record["source_path"] not in files:
            logger.warning(
                "%s: edge at %s:%s names a file the inventory does not carry",
                repository,
                record["source_path"],
                record["source_line"],
            )
            continue
        edges.append(
            {
                "relative_path": record["source_path"],
                "source_line": record["source_line"],
                "source_column": record["source_column"],
                "from_id": record["from"],
                "to_reference": record["to"],
                "edge_kind": record["edge_kind"],
                "evidence": record["evidence"],
            }
        )

    header = repository_map.header
    return IndexedRepository(
        repository=repository,
        header={
            "repository_key": header["repository"],
            "revision": header["head_commit"],
            "branch": header["branch"],
            "working_tree_status": header["working_tree_status"],
            "content_hash": header["content_hash"],
            "generator_version": header["generator_version"],
            "rules_hash": header["rules_hash"],
        },
        files=list(files.values()),
        edges=edges,
    )
