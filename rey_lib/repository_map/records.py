"""Typed records for the deterministic repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-001).

Records are frozen so a completed scan cannot be mutated afterwards, and every
record serializes through ``to_dict`` into one JSONL record with a fixed key
order, so the generated fact stream stays byte-stable across runs from the same
commit. The generated factual map is JSONL, never a YAML document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "EDGE_KIND_BACKEND_STRING_REFERENCE",
    "EDGE_KIND_CALL",
    "EDGE_KIND_GLOBAL_REFERENCE",
    "EDGE_KIND_IMPORT",
    "EDGE_KIND_PROPERTY_ACCESS",
    "EDGE_KIND_REGISTRATION",
    "EDGE_KIND_RE_EXPORT",
    "EDGE_KIND_TEMPLATE_LOAD",
    "ENTRY_POINT_LOAD_LOADED",
    "ENTRY_POINT_LOAD_NOT_LOADED",
    "ENTRY_POINT_LOAD_UNKNOWN",
    "LANGUAGE_UNKNOWN",
    "RECORD_TYPE_DEPENDENCY_EDGE",
    "RECORD_TYPE_FILE",
    "RECORD_TYPE_SYMBOL",
    "SYMBOL_KIND_CLASS",
    "SYMBOL_KIND_EXPORT",
    "SYMBOL_KIND_FUNCTION",
    "SYMBOL_KIND_GLOBAL_PUBLICATION",
    "SYMBOL_KIND_RE_EXPORT",
    "SYMBOL_KIND_VARIABLE",
    "FileRecord",
    "ReferenceEdge",
    "ScanRules",
    "SymbolInventory",
    "SymbolRecord",
]

# Tri-state for REQ-011's "loaded by a known runtime entry point".
# Entry-point discovery arrives in INC-003. Until then every file records
# UNKNOWN rather than a fabricated answer, per the conservative-evidence rule.
ENTRY_POINT_LOAD_LOADED = "loaded"
ENTRY_POINT_LOAD_NOT_LOADED = "not_loaded"
ENTRY_POINT_LOAD_UNKNOWN = "unknown"

# Language recorded when no configured extension mapping matches the file.
LANGUAGE_UNKNOWN = "unknown"

# JSONL record_type values. The generated factual map is a JSONL fact stream,
# never a YAML document, and every record carries record_type and record_id.
RECORD_TYPE_FILE = "file"
RECORD_TYPE_SYMBOL = "symbol"
RECORD_TYPE_DEPENDENCY_EDGE = "dependency_edge"

# symbol_kind vocabulary. Only syntax-confirmed top-level declarations are
# emitted; a local never becomes a symbol record.
SYMBOL_KIND_FUNCTION = "function"
SYMBOL_KIND_CLASS = "class"
SYMBOL_KIND_VARIABLE = "variable"
SYMBOL_KIND_EXPORT = "export"
SYMBOL_KIND_RE_EXPORT = "re_export"
SYMBOL_KIND_GLOBAL_PUBLICATION = "global_publication"

# edge_kind vocabulary. Extractors emit only the kinds they can prove from
# executable syntax; registration, template_load and backend_string_reference
# come from the INC-003 scanners, not from per-file extraction.
EDGE_KIND_CALL = "call"
EDGE_KIND_IMPORT = "import"
EDGE_KIND_RE_EXPORT = "re_export"
EDGE_KIND_PROPERTY_ACCESS = "property_access"
EDGE_KIND_GLOBAL_REFERENCE = "global_reference"
EDGE_KIND_REGISTRATION = "registration"
EDGE_KIND_TEMPLATE_LOAD = "template_load"
EDGE_KIND_BACKEND_STRING_REFERENCE = "backend_string_reference"


@dataclass(frozen=True)
class SymbolRecord:
    """One syntax-confirmed top-level declaration.

    A declaration nested inside a function or class body is not a top-level
    declaration and never reaches this record (REQ-022, AC-003).

    Attributes:
        source_path: Path the declaration is written in.
        source_line: 1-indexed declaration line (REQ-023).
        source_column: 0-indexed declaration column.
        name: Declared name as written in source.
        symbol_kind: One of the ``SYMBOL_KIND_*`` constants.
        exported: True when the module publishes the name.
    """

    source_path: str
    source_line: int
    source_column: int
    name: str
    symbol_kind: str
    exported: bool = False

    @property
    def record_id(self) -> str:
        """Return the stable identity of this symbol fact.

        ``name`` stays the semantic symbol name; declaration position is part
        of the identity only so that two top-level declarations sharing a name
        in one file remain two facts. This scanner records what the source
        says, so a duplicate declaration must never silently drop a record.
        """
        return (
            f"{RECORD_TYPE_SYMBOL}:{self.source_path}:{self.name}"
            f":{self.source_line}:{self.source_column}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this declaration as a JSONL 'symbol' record."""
        return {
            "record_type": RECORD_TYPE_SYMBOL,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "source_column": self.source_column,
            "name": self.name,
            "symbol_kind": self.symbol_kind,
            "exported": self.exported,
        }


@dataclass(frozen=True)
class SymbolInventory:
    """The top-level declarations of one source file.

    This is a per-file container, not itself a generated record: each contained
    declaration serializes to its own ``symbol`` record.

    Attributes:
        path: Path of the analysed file.
        language: Language the file was parsed as.
        symbols: The file's top-level declarations, deterministically ordered.
    """

    path: str
    language: str
    symbols: tuple[SymbolRecord, ...] = ()

    def to_records(self) -> list[dict[str, Any]]:
        """Return one JSONL 'symbol' record per declaration."""
        return [symbol.to_dict() for symbol in self.symbols]

    def of_kind(self, symbol_kind: str) -> tuple[SymbolRecord, ...]:
        """Return the declarations of one kind, preserving order.

        Args:
            symbol_kind: One of the ``SYMBOL_KIND_*`` constants.

        Returns:
            The matching declarations.
        """
        return tuple(symbol for symbol in self.symbols if symbol.symbol_kind == symbol_kind)


@dataclass(frozen=True)
class ReferenceEdge:
    """One executable reference, carrying the evidence that proves it.

    An edge is only created from executable syntax. Text inside a comment,
    docstring or string literal never produces one (REQ-032 to REQ-034).

    Attributes:
        source_path: Path the reference is written in.
        source_line: 1-indexed line of the referencing expression.
        source_column: 0-indexed column, used to keep record_id unique when one
            line carries several references.
        from_id: record_id of the fact making the reference.
        to: Referenced name as written, dotted for member expressions. It stays
            unresolved here; resolving it to a record_id is graph work.
        edge_kind: One of the ``EDGE_KIND_*`` constants.
        evidence: The syntax node kind that proves the reference.
    """

    source_path: str
    source_line: int
    source_column: int
    from_id: str
    to: str
    edge_kind: str
    evidence: str

    @property
    def record_id(self) -> str:
        """Return the stable identity of this edge fact.

        Line and column locate the reference precisely, so two references on
        one line stay distinct without depending on emission order.
        """
        return (
            f"edge:{self.source_path}:{self.source_line}:{self.source_column}"
            f":{self.edge_kind}:{self.to}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this reference as a JSONL 'dependency_edge' record."""
        return {
            "record_type": RECORD_TYPE_DEPENDENCY_EDGE,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "from": self.from_id,
            "to": self.to,
            "edge_kind": self.edge_kind,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class FileRecord:
    """One inventoried source file and its deterministic classification.

    Attributes:
        path: Repository-relative POSIX path. The record identity and sort key.
        language: Configured language name, or ``LANGUAGE_UNKNOWN``.
        size_bytes: File size in bytes as reported by the filesystem.
        is_generated: True when the path matches a configured generated glob.
        is_vendor: True when the path matches a configured vendor glob.
        is_test: True when the path matches a configured test glob.
        entry_point_load_state: One of the ``ENTRY_POINT_LOAD_*`` constants.
    """

    path: str
    language: str
    size_bytes: int
    is_generated: bool
    is_vendor: bool
    is_test: bool
    entry_point_load_state: str = ENTRY_POINT_LOAD_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Return this file as a JSONL 'file' record.

        Key order is fixed so the serialized stream is byte-stable across runs.

        Returns:
            The record, conforming to jsonl_record_contract.record_types.file.
        """
        return {
            "record_type": RECORD_TYPE_FILE,
            "record_id": f"{RECORD_TYPE_FILE}:{self.path}",
            "path": self.path,
            "language": self.language,
            "size_bytes": self.size_bytes,
            "classification": {
                "generated": self.is_generated,
                "vendor": self.is_vendor,
                "test": self.is_test,
            },
            "entry_point_load_state": self.entry_point_load_state,
        }


@dataclass(frozen=True)
class ScanRules:
    """Per-repository scan configuration supplied as data, never as code.

    The engine stays application-neutral: every repository-specific name lives
    in the scanned repository's own rules file (ARCH-005).

    Glob semantics — these are NOT shell globs:
        Every glob is matched with ``fnmatch.fnmatchcase`` against the
        repository-relative POSIX path. Consequences worth stating outright,
        because shell and pathlib habits predict otherwise:

        - ``*`` crosses directory separators. ``static/*`` matches
          ``static/js/react/bundle.js``, not only ``static/bundle.js``.
        - There is no ``**``. Writing it adds nothing that ``*`` lacks.
        - Matching is case-sensitive on every platform, so a scan cannot vary
          with the case sensitivity of the filesystem underneath it.
        - Patterns are anchored at the repository root. ``tests/*`` matches only
          a top-level tests directory; use ``*/tests/*`` for nested ones.

        Hidden paths never reach glob matching at all. Excluding any path
        segment beginning with '.' is repository policy applied by the scanner
        before stat, not a rule expressible here.

    Attributes:
        ignored_directory_names: Directory names pruned during the walk.
        ignored_path_globs: Globs excluding files by repository-relative path.
        language_by_extension: Lowercase file suffix to language name.
        generated_path_globs: Globs marking build output.
        vendor_path_globs: Globs marking third-party code.
        test_path_globs: Globs marking test code.
    """

    ignored_directory_names: frozenset[str]
    ignored_path_globs: tuple[str, ...]
    language_by_extension: dict[str, str]
    generated_path_globs: tuple[str, ...]
    vendor_path_globs: tuple[str, ...]
    test_path_globs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ScanRules":
        """Build rules from a parsed rules mapping.

        Args:
            data: Parsed contents of a repository's rules file.

        Returns:
            The typed rules.

        Raises:
            ValueError: If the mapping or any recognized section has the wrong
                shape. Missing sections are permitted and yield empty rules.
        """
        if not isinstance(data, dict):
            raise ValueError(f"Scan rules must be a mapping, got {type(data).__name__}.")

        extensions = _require_str_mapping(data, "language_by_extension")
        return cls(
            ignored_directory_names=frozenset(_require_str_list(data, "ignored_directory_names")),
            ignored_path_globs=tuple(_require_str_list(data, "ignored_path_globs")),
            # Suffixes are compared lowercase so casing in the rules file is not
            # a source of nondeterminism between platforms.
            language_by_extension={key.lower(): value for key, value in extensions.items()},
            generated_path_globs=tuple(_require_str_list(data, "generated_path_globs")),
            vendor_path_globs=tuple(_require_str_list(data, "vendor_path_globs")),
            test_path_globs=tuple(_require_str_list(data, "test_path_globs")),
        )


def _require_str_list(data: dict[str, Any], key: str) -> list[str]:
    """Return a list-of-strings section, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The section values.

    Raises:
        ValueError: If the section is present but is not a list of strings.
    """
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Scan rules section '{key}' must be a list of strings.")
    return value


def _require_str_mapping(data: dict[str, Any], key: str) -> dict[str, str]:
    """Return a string-to-string section, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The section mapping.

    Raises:
        ValueError: If the section is present but is not a string mapping.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, str) for pair in value.items() for item in pair
    ):
        raise ValueError(f"Scan rules section '{key}' must map strings to strings.")
    return value
