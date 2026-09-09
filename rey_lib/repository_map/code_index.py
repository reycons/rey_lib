"""Writing one snapshot into the relational code index.

The second destination for a ``CodeIndexSnapshot``. The committed JSONL is the
first, and both are written from the same value, so neither can describe a scan
the other did not see.

**The engine does not open a connection.** ``index`` is handed a
:class:`CodeIndexWriter` and knows nothing about providers, credentials or
installations; the application resolves one and passes it down. A library that
found its own database would make a scan impossible without one, and the
committed artifact has to stay reproducible from a bare checkout.

**Indexing replaces, it does not merge.** One repository's whole file and
symbol set is written at once, so a file deleted or renamed in the source
cannot survive in the model. The replacement is one transaction: a half-written
repository would claim a revision it does not hold. *How* a provider performs
that replacement is the provider's business -- the relational model does not
encode it, and neither does this.

Nothing here is authored. Every row is derived from a scan, which is what lets
a rebuild from source be the recovery path and lets the export prove the model.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.records import RECORD_TYPE_FILE, RECORD_TYPE_SYMBOL
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

    def replace_repository(self, indexed: "IndexedRepository") -> None:
        """Replace one repository's indexed set, all or nothing.

        Args:
            indexed: The repository's whole state, as one scan observed it.
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
    """

    __slots__ = ("repository", "header", "files")

    def __init__(
        self,
        repository: str,
        header: dict[str, Any],
        files: list[dict[str, Any]],
    ) -> None:
        self.repository = repository
        self.header = header
        self.files = files


def index(snapshot: CodeIndexSnapshot, writer: CodeIndexWriter) -> int:
    """Write every repository in one snapshot to one destination.

    Repositories are written one at a time, each replaced whole. A destination
    that fails partway leaves the repositories it already replaced in place:
    each is internally consistent, and the next run replaces the rest. Making
    the whole estate one transaction would mean one unreadable checkout stops
    every other repository from being indexed at all.

    Args:
        snapshot: What to write.
        writer: Where to write it.

    Returns:
        How many repositories were replaced.
    """
    for repository, repository_map in snapshot.maps.items():
        writer.replace_repository(_indexed(repository, repository_map))
    logger.info("Indexed %d repositories", len(snapshot.maps))
    return len(snapshot.maps)


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
    )
