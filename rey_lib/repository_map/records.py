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
    "SYMBOL_KIND_ENUM",
    "SYMBOL_KIND_EXPORT",
    "SYMBOL_KIND_FUNCTION",
    "SYMBOL_KIND_GLOBAL_PUBLICATION",
    "SYMBOL_KIND_INTERFACE",
    "SYMBOL_KIND_RE_EXPORT",
    "SYMBOL_KIND_TYPE_ALIAS",
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
RECORD_TYPE_REGISTRATION = "registration"
RECORD_TYPE_ENTRY_POINT = "entry_point"
RECORD_TYPE_GLOBAL_PUBLICATION = "global_publication"
RECORD_TYPE_GLOBAL_CONSUMER = "global_consumer"

# registration_kind vocabulary.
REGISTRATION_KIND_ACTION = "action"
REGISTRATION_KIND_EMBEDDED_OBJECT = "embedded_object"
REGISTRATION_KIND_VIEWER = "viewer"
REGISTRATION_KIND_TREE = "tree"
REGISTRATION_KIND_PROVIDER = "provider"
REGISTRATION_KIND_OTHER = "other"

# entry_point_kind vocabulary.
ENTRY_POINT_KIND_BUNDLE = "bundle"
ENTRY_POINT_KIND_MODULE = "module"
ENTRY_POINT_KIND_CLASSIC_SCRIPT = "classic_script"
ENTRY_POINT_KIND_TEMPLATE = "template"
ENTRY_POINT_KIND_INLINE_CALL = "inline_call"
ENTRY_POINT_KIND_ALTERNATE_WINDOW = "alternate_window"

# access_kind vocabulary for a global consumer.
ACCESS_KIND_CALL = "call"
ACCESS_KIND_OPTIONAL_CALL = "optional_call"
ACCESS_KIND_PROPERTY_ACCESS = "property_access"
ACCESS_KIND_TYPEOF = "typeof"
ACCESS_KIND_BRACKET_ACCESS = "bracket_access"

# symbol_kind vocabulary. Only syntax-confirmed top-level declarations are
# emitted; a local never becomes a symbol record. A TypeScript interface, enum
# and type alias each carry their own kind rather than being squeezed into
# class or variable; an abstract class stays class.
SYMBOL_KIND_FUNCTION = "function"
SYMBOL_KIND_CLASS = "class"
SYMBOL_KIND_INTERFACE = "interface"
SYMBOL_KIND_ENUM = "enum"
SYMBOL_KIND_TYPE_ALIAS = "type_alias"
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
class RegistrationRule:
    """How one registry's call-site registrations are recognized.

    Registries are matched by method name plus receiver, because one registry
    concept is reached through several receiver spellings — a module-local
    alias, a global, or the result of an accessor call.

    Attributes:
        registry: Name recorded on matching registrations.
        registration_kind: One of the ``REGISTRATION_KIND_*`` constants.
        method: Method name that performs the registration.
        receiver_globs: Globs matched against the receiver expression text.
        id_argument: Zero-based index of the argument holding the id.
        id_property: Property to read when the id argument is an object rather
            than a string, or None when the argument is the id itself.
    """

    registry: str
    registration_kind: str
    method: str
    receiver_globs: tuple[str, ...]
    id_argument: int = 0
    id_property: str | None = None


@dataclass(frozen=True)
class DeclaredRegistrationRule:
    """How registrations declared as object literals are recognized.

    Some registries are populated from a literal collection rather than by a
    call, so the id is a property of each entry.

    Attributes:
        registry: Name recorded on matching registrations.
        registration_kind: One of the ``REGISTRATION_KIND_*`` constants.
        id_property: Property holding the declared id.
        path_globs: Files this rule applies to.
    """

    registry: str
    registration_kind: str
    id_property: str
    path_globs: tuple[str, ...]


@dataclass(frozen=True)
class BackendRegistrationRule:
    """How a backend collection that names frontend objects is recognized.

    This is the declaration that makes a frontend file reachable from the
    backend by string, with no JavaScript caller anywhere.

    Attributes:
        registry: Name recorded on matching registrations.
        registration_kind: One of the ``REGISTRATION_KIND_*`` constants.
        symbol: Module-level name holding the collection.
        id_key: Mapping key holding the registered id.
        implementation_keys: Mapping keys naming implementations.
        path_globs: Files this rule applies to.
    """

    registry: str
    registration_kind: str
    symbol: str
    id_key: str
    implementation_keys: tuple[str, ...]
    path_globs: tuple[str, ...]


@dataclass(frozen=True)
class RegistrationRecord:
    """One explicit id-to-object registration.

    Attributes:
        source_path: Path the registration is written in.
        source_line: 1-indexed line of the registration call or entry.
        source_column: 0-indexed column of the registration.
        registry: Name of the registry being written to.
        registered_id: The id as written. When the id is not a literal this
            holds the expression text instead.
        registered_id_resolved: False when the id could not be read as a
            literal. A syntax scanner cannot follow a variable, and inventing
            an id would be worse than recording that one exists.
        implementation: What is being registered, as written.
        registration_kind: One of the ``REGISTRATION_KIND_*`` constants.
    """

    source_path: str
    source_line: int
    source_column: int
    registry: str
    registered_id: str
    implementation: str
    registration_kind: str
    registered_id_resolved: bool = True

    @property
    def record_id(self) -> str:
        """Return the stable identity of this registration fact."""
        return (
            f"{RECORD_TYPE_REGISTRATION}:{self.registry}:{self.registered_id}"
            f":{self.source_path}:{self.source_line}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this registration as a JSONL 'registration' record."""
        return {
            "record_type": RECORD_TYPE_REGISTRATION,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "registry": self.registry,
            "registered_id": self.registered_id,
            "registered_id_resolved": self.registered_id_resolved,
            "implementation": self.implementation,
            "registration_kind": self.registration_kind,
        }


@dataclass(frozen=True)
class EntryPointRecord:
    """One runtime entry point: a place execution can begin.

    Attributes:
        source_path: Path declaring the entry point.
        source_line: 1-indexed line of the declaration.
        source_column: 0-indexed column of the declaration.
        entry_point_kind: One of the ``ENTRY_POINT_KIND_*`` constants.
        target: What is loaded or executed, as written.
        window_or_host: The window or host document that bootstraps it.
    """

    source_path: str
    source_line: int
    source_column: int
    entry_point_kind: str
    target: str
    window_or_host: str

    @property
    def record_id(self) -> str:
        """Return the stable identity of this entry-point fact."""
        return (
            f"{RECORD_TYPE_ENTRY_POINT}:{self.source_path}:{self.source_line}"
            f":{self.source_column}:{self.entry_point_kind}:{self.target}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this entry point as a JSONL 'entry_point' record."""
        return {
            "record_type": RECORD_TYPE_ENTRY_POINT,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "entry_point_kind": self.entry_point_kind,
            "target": self.target,
            "window_or_host": self.window_or_host,
        }


@dataclass(frozen=True)
class GlobalPublicationRecord:
    """One assignment publishing onto a global object.

    Attributes:
        source_path: Path the publication is written in.
        source_line: 1-indexed line of the assignment.
        source_column: 0-indexed column of the assignment.
        global_name: The published global, such as window.ReyX.
        implementation: What is assigned to it, as written.
    """

    source_path: str
    source_line: int
    source_column: int
    global_name: str
    implementation: str

    @property
    def record_id(self) -> str:
        """Return the stable identity of this publication fact."""
        return (
            f"{RECORD_TYPE_GLOBAL_PUBLICATION}:{self.global_name}"
            f":{self.source_path}:{self.source_line}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this publication as a JSONL 'global_publication' record."""
        return {
            "record_type": RECORD_TYPE_GLOBAL_PUBLICATION,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "global": self.global_name,
            "implementation": self.implementation,
        }


@dataclass(frozen=True)
class GlobalConsumerRecord:
    """One executable consumer of a global.

    Attributes:
        source_path: Path the consumption is written in.
        source_line: 1-indexed line of the reference.
        source_column: 0-indexed column of the reference.
        global_name: The consumed global, such as window.ReyX.
        access_kind: One of the ``ACCESS_KIND_*`` constants.
    """

    source_path: str
    source_line: int
    source_column: int
    global_name: str
    access_kind: str

    @property
    def record_id(self) -> str:
        """Return the stable identity of this consumer fact."""
        return (
            f"{RECORD_TYPE_GLOBAL_CONSUMER}:{self.global_name}"
            f":{self.source_path}:{self.source_line}:{self.source_column}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this consumer as a JSONL 'global_consumer' record."""
        return {
            "record_type": RECORD_TYPE_GLOBAL_CONSUMER,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "global": self.global_name,
            "access_kind": self.access_kind,
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
        extract_facts_from_generated: Whether generated files yield symbol and
            dependency_edge records. Their file records are emitted either way.
        extract_facts_from_vendor: Whether vendored files yield symbol and
            dependency_edge records. Their file records are emitted either way.
    """

    ignored_directory_names: frozenset[str]
    ignored_path_globs: tuple[str, ...]
    language_by_extension: dict[str, str]
    generated_path_globs: tuple[str, ...]
    vendor_path_globs: tuple[str, ...]
    test_path_globs: tuple[str, ...]
    # Extraction defaults to on, so omitting the section never silently drops
    # facts. Suppression is always an explicit choice in the rules file.
    extract_facts_from_generated: bool = True
    extract_facts_from_vendor: bool = True
    # Root discovery (INC-003). Absent sections mean the repository declares no
    # registries or templates, never that detection is skipped silently.
    registration_rules: tuple[RegistrationRule, ...] = ()
    declared_registration_rules: tuple[DeclaredRegistrationRule, ...] = ()
    backend_registration_rules: tuple[BackendRegistrationRule, ...] = ()
    template_globs: tuple[str, ...] = ()
    bundle_globs: tuple[str, ...] = ()
    primary_template: str | None = None

    def extracts_facts_from(self, file_record: "FileRecord") -> bool:
        """Return whether a file should yield symbol and edge records.

        A suppressed file still appears in the map: only fact extraction is
        skipped, never the file record itself, so the map continues to state
        that the file exists and how it is classified (REQ-013).

        Args:
            file_record: The inventoried file.

        Returns:
            True when facts should be extracted from the file.
        """
        if file_record.is_generated and not self.extract_facts_from_generated:
            return False
        if file_record.is_vendor and not self.extract_facts_from_vendor:
            return False
        return True

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
        extraction = _require_bool_mapping(data, "fact_extraction")
        return cls(
            ignored_directory_names=frozenset(_require_str_list(data, "ignored_directory_names")),
            ignored_path_globs=tuple(_require_str_list(data, "ignored_path_globs")),
            # Suffixes are compared lowercase so casing in the rules file is not
            # a source of nondeterminism between platforms.
            language_by_extension={key.lower(): value for key, value in extensions.items()},
            generated_path_globs=tuple(_require_str_list(data, "generated_path_globs")),
            vendor_path_globs=tuple(_require_str_list(data, "vendor_path_globs")),
            test_path_globs=tuple(_require_str_list(data, "test_path_globs")),
            extract_facts_from_generated=extraction.get("generated", True),
            extract_facts_from_vendor=extraction.get("vendor", True),
            registration_rules=_registration_rules(data),
            declared_registration_rules=_declared_registration_rules(data),
            backend_registration_rules=_backend_registration_rules(data),
            template_globs=tuple(_require_str_list(data, "template_globs")),
            bundle_globs=tuple(_require_str_list(data, "bundle_globs")),
            primary_template=data.get("primary_template"),
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


def _rule_entries(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return a section's rule entries, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The entries.

    Raises:
        ValueError: If the section is not a list of mappings.
    """
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Scan rules section '{key}' must be a list of mappings.")
    return value


def _required(entry: dict[str, Any], key: str, section: str) -> Any:
    """Return a required rule field.

    Args:
        entry: One rule entry.
        key: Field name.
        section: Section name, for the error message.

    Returns:
        The field value.

    Raises:
        ValueError: If the field is missing.
    """
    if key not in entry:
        raise ValueError(f"Scan rules section '{section}' entry is missing '{key}'.")
    return entry[key]


def _registration_rules(data: dict[str, Any]) -> tuple[RegistrationRule, ...]:
    """Build call-site registration rules from the rules mapping.

    Args:
        data: Parsed rules mapping.

    Returns:
        The rules, in declaration order.

    Raises:
        ValueError: If a rule entry is malformed.
    """
    section = "registrations"
    return tuple(
        RegistrationRule(
            registry=_required(entry, "registry", section),
            registration_kind=_required(entry, "registration_kind", section),
            method=_required(entry, "method", section),
            receiver_globs=tuple(entry.get("receiver_globs", ())),
            id_argument=entry.get("id_argument", 0),
            id_property=entry.get("id_property"),
        )
        for entry in _rule_entries(data, section)
    )


def _declared_registration_rules(data: dict[str, Any]) -> tuple[DeclaredRegistrationRule, ...]:
    """Build literal-declaration registration rules from the rules mapping.

    Args:
        data: Parsed rules mapping.

    Returns:
        The rules, in declaration order.

    Raises:
        ValueError: If a rule entry is malformed.
    """
    section = "declared_registrations"
    return tuple(
        DeclaredRegistrationRule(
            registry=_required(entry, "registry", section),
            registration_kind=_required(entry, "registration_kind", section),
            id_property=_required(entry, "id_property", section),
            path_globs=tuple(entry.get("path_globs", ())),
        )
        for entry in _rule_entries(data, section)
    )


def _backend_registration_rules(data: dict[str, Any]) -> tuple[BackendRegistrationRule, ...]:
    """Build backend-collection registration rules from the rules mapping.

    Args:
        data: Parsed rules mapping.

    Returns:
        The rules, in declaration order.

    Raises:
        ValueError: If a rule entry is malformed.
    """
    section = "backend_registrations"
    return tuple(
        BackendRegistrationRule(
            registry=_required(entry, "registry", section),
            registration_kind=_required(entry, "registration_kind", section),
            symbol=_required(entry, "symbol", section),
            id_key=_required(entry, "id_key", section),
            implementation_keys=tuple(entry.get("implementation_keys", ())),
            path_globs=tuple(entry.get("path_globs", ())),
        )
        for entry in _rule_entries(data, section)
    )


def _require_bool_mapping(data: dict[str, Any], key: str) -> dict[str, bool]:
    """Return a string-to-boolean section, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The section mapping.

    Raises:
        ValueError: If the section is present but is not a boolean mapping. A
            near-miss such as the string "false" is rejected rather than
            silently treated as true.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(flag, bool) for name, flag in value.items()
    ):
        raise ValueError(f"Scan rules section '{key}' must map strings to booleans.")
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
