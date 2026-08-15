"""The generated record-emitter registry.

Contract: rey_system_repository_map_correction.sgc.yaml (COR-009).

An emitter owns two things: how its records are acquired, and the key that
orders them deterministically. Adding a record type is that implementation plus
one entry in ``RECORD_EMITTERS``.

Acquisition is shared through ``ScanContext`` because the facts depend on each
other — reachability needs a graph, the graph needs registrations and globals,
violations need almost everything. Each stage is computed once, on demand, so
an emitter asks for what it needs without any emitter, or the writer, owning
the order those stages run in.

The writer keeps the header, the stream ordering contract, the content hash and
the atomic write. It holds no per-record-type sequence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.dispatchers import inventory_dispatchers_and_switches
from rey_lib.repository_map.entry_points import extract_runtime_entry_points
from rey_lib.repository_map.extractors import (
    LANGUAGE_EXTRACTORS,
    extract_executable_references,
    extract_symbols,
)
from rey_lib.repository_map.globals_scan import extract_global_publications_and_consumers
from rey_lib.repository_map.graph import build_dependency_graph, compute_reachability
from rey_lib.repository_map.inventory import inventory_files
from rey_lib.repository_map.records import ScanRules
from rey_lib.repository_map.registrations import extract_registrations

__all__ = ["RECORD_EMITTERS", "RecordEmitter", "ScanContext"]


def _by_record_id(record: dict[str, Any]) -> Any:
    """Return the default ordering key: a record's own identity."""
    return record["record_id"]


@dataclass(frozen=True)
class RecordEmitter:
    """One generated record type, and how to acquire and order it.

    Attributes:
        record_type: The record_type this emitter produces.
        acquire: Returns the records from a scan context.
        order_by: Deterministic ordering key within this emitter's group.
    """

    record_type: str
    acquire: Callable[["ScanContext"], list[dict[str, Any]]]
    order_by: Callable[[dict[str, Any]], Any] = _by_record_id


class ScanContext:
    """One repository's analysis, computed once and shared between emitters.

    Each stage is a cached property, so the dependency order between stages is
    expressed by what they ask for rather than by a sequence someone maintains.

    Attributes:
        repo_root: Repository being scanned.
        rules: That repository's scan rules.
    """

    def __init__(self, repo_root: Path, rules: ScanRules) -> None:
        """Initialize the context.

        Args:
            repo_root: Repository being scanned.
            rules: That repository's scan rules.
        """
        self.repo_root = repo_root
        self.rules = rules

    @cached_property
    def files(self) -> list[Any]:
        """Return the inventoried files."""
        return inventory_files(self.repo_root, self.rules)

    @cached_property
    def _extraction(self) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return references, symbol records and edge records from one pass.

        Symbols and references come from the same parse of the same file, so
        they are acquired together rather than parsing twice.
        """
        references: list[Any] = []
        symbols: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for file_record in self.files:
            if file_record.language not in LANGUAGE_EXTRACTORS:
                continue
            if not self.rules.extracts_facts_from(file_record):
                continue
            path = self.repo_root / file_record.path
            inventory = extract_symbols(path, file_record.language, file_record.path)
            file_references = extract_executable_references(
                path, file_record.language, file_record.path
            )
            references.extend(file_references)
            symbols.extend(inventory.to_records())
            edges.extend(edge.to_dict() for edge in file_references)
        return references, symbols, edges

    @property
    def references(self) -> list[Any]:
        """Return the executable reference edges."""
        return self._extraction[0]

    @cached_property
    def registrations(self) -> list[Any]:
        """Return the explicit id-to-object registrations."""
        return extract_registrations(self.repo_root, self.files, self.rules)

    @cached_property
    def entry_points(self) -> list[Any]:
        """Return the runtime entry points."""
        return extract_runtime_entry_points(self.repo_root, self.files, self.rules)

    @cached_property
    def global_report(self) -> Any:
        """Return the global publications and consumers."""
        return extract_global_publications_and_consumers(self.repo_root, self.files, self.rules)

    @cached_property
    def graph(self) -> Any:
        """Return the dependency graph."""
        return build_dependency_graph(
            self.files,
            self.references,
            self.registrations,
            self.entry_points,
            list(self.global_report.publications),
            list(self.global_report.consumers),
            self.rules,
        )

    @cached_property
    def reachability(self) -> list[Any]:
        """Return the reachability verdicts."""
        return compute_reachability(self.graph, self.files)

    @cached_property
    def dispatchers(self) -> list[Any]:
        """Return the dispatcher facts."""
        return inventory_dispatchers_and_switches(
            self.repo_root, self.files, self.rules, self.references
        )

    @cached_property
    def violations(self) -> list[Any]:
        """Return the architecture violations.

        Guards run during generation through the same authority every consumer
        calls, so the map carries the verdicts its own evidence supports.
        """
        return check_architecture_boundaries(
            self.rules,
            references=self.references,
            publications=list(self.global_report.publications),
            files=self.files,
            entry_points=self.entry_points,
            dispatchers=self.dispatchers,
        )


# The registry. Order here is the stream's record-type order; within a group,
# each emitter's own key applies. Adding a record type is one entry plus its
# acquisition, and no central sequence changes.
RECORD_EMITTERS: tuple[RecordEmitter, ...] = (
    RecordEmitter("file", lambda ctx: [r.to_dict() for r in ctx.files]),
    RecordEmitter("symbol", lambda ctx: list(ctx._extraction[1])),
    RecordEmitter("dependency_edge", lambda ctx: list(ctx._extraction[2])),
    RecordEmitter("registration", lambda ctx: [r.to_dict() for r in ctx.registrations]),
    RecordEmitter("entry_point", lambda ctx: [r.to_dict() for r in ctx.entry_points]),
    RecordEmitter(
        "global_publication",
        lambda ctx: [r.to_dict() for r in ctx.global_report.publications],
    ),
    RecordEmitter(
        "global_consumer",
        lambda ctx: [r.to_dict() for r in ctx.global_report.consumers],
    ),
    RecordEmitter("reachability", lambda ctx: [r.to_dict() for r in ctx.reachability]),
    RecordEmitter("dispatcher", lambda ctx: [r.to_dict() for r in ctx.dispatchers]),
    RecordEmitter("architecture_violation", lambda ctx: [r.to_dict() for r in ctx.violations]),
)
