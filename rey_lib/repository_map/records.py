"""Typed records for the deterministic repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-001).

Records are frozen so a completed scan cannot be mutated afterwards, and every
record serializes through ``to_dict`` into one JSONL record with a fixed key
order, so the generated fact stream stays byte-stable across runs from the same
commit. The generated factual map is JSONL, never a YAML document.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Optional

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
    "RECORD_TYPE_ACCESS",
    "RECORD_TYPE_CLASS_ATTRIBUTE",
    "RECORD_TYPE_ASSIGNMENT",
    "RECORD_TYPE_PARAMETER",
    "RECORD_TYPE_SYMBOL",
    "SYMBOL_KIND_CLASS",
    "SYMBOL_KIND_ENUM",
    "SYMBOL_KIND_EXPORT",
    "SYMBOL_KIND_FUNCTION",
    "SYMBOL_KIND_GLOBAL_PUBLICATION",
    "SYMBOL_KIND_INTERFACE",
    "SYMBOL_KIND_METHOD",
    "SYMBOL_KIND_RE_EXPORT",
    "SYMBOL_KIND_TYPE_ALIAS",
    "SYMBOL_KIND_VARIABLE",
    "attributed_edges",
    "dotted_identity",
    "DECLARATION_FORM_CLASS_ATTRIBUTE",
    "DECLARATION_FORM_FIELD_DEFINITION",
    "matches_any_glob",
    "FileRecord",
    "AccessRecord",
    "ClassAttributeRecord",
    "AssignmentRecord",
    "ParameterRecord",
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
RECORD_TYPE_REPOSITORY_MAP = "repository_map"
RECORD_TYPE_FILE = "file"
RECORD_TYPE_SYMBOL = "symbol"
RECORD_TYPE_DEPENDENCY_EDGE = "dependency_edge"
RECORD_TYPE_PARAMETER = "parameter"
RECORD_TYPE_ASSIGNMENT = "assignment"
RECORD_TYPE_ACCESS = "access"
RECORD_TYPE_CLASS_ATTRIBUTE = "class_attribute"
RECORD_TYPE_REGISTRATION = "registration"
RECORD_TYPE_ENTRY_POINT = "entry_point"
RECORD_TYPE_GLOBAL_PUBLICATION = "global_publication"
RECORD_TYPE_GLOBAL_CONSUMER = "global_consumer"
RECORD_TYPE_REACHABILITY = "reachability"
RECORD_TYPE_ARCHITECTURE_VIOLATION = "architecture_violation"

# The derived architecture projection: authored meaning joined to current
# structure. Its own artifact, never records inside a repository map.
RECORD_TYPE_ARCHITECTURE_MAP = "architecture_map"
RECORD_TYPE_ARCHITECTURE_NODE = "architecture_node"
RECORD_TYPE_DISPATCHER = "dispatcher"

# System index record types. The index binds repository baselines; it is a
# different artifact from a repository map and never carries repository facts.
RECORD_TYPE_SYSTEM_MAP = "system_repository_map"
RECORD_TYPE_REPOSITORY_BASELINE = "repository_baseline"
RECORD_TYPE_CROSS_REPOSITORY_EDGE = "cross_repository_edge"

# The only classification this generator emits. Whether a dispatcher is
# architecturally legitimate is review's decision, not the scanner's.
DISPATCHER_UNREVIEWED = "unreviewed"

# reachability status vocabulary. 'dead' is deliberately absent: a scanner that
# has not checked every runtime mechanism cannot prove absence of use.
REACHABILITY_DEFINITELY = "definitely_reachable"
REACHABILITY_POTENTIALLY = "potentially_reachable"
REACHABILITY_UNREFERENCED = "unreferenced_candidate"

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
SYMBOL_KIND_METHOD = "method"
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


# parameter_kind vocabulary. Position in the signature and nothing else.
# Optionality is not a kind: a TypeScript ``b?: T`` is a positional parameter
# that happens to be omissible, and encoding that as a sixth kind would make a
# property into a position.
PARAMETER_KIND_POSITIONAL_ONLY = "positional_only"
PARAMETER_KIND_POSITIONAL = "positional"
PARAMETER_KIND_KEYWORD_ONLY = "keyword_only"
PARAMETER_KIND_VAR_POSITIONAL = "var_positional"
PARAMETER_KIND_VAR_KEYWORD = "var_keyword"


# What an assignment writes to. Shape of the target and nothing else.
TARGET_KIND_NAME = "name"
TARGET_KIND_ATTRIBUTE = "attribute"
TARGET_KIND_SUBSCRIPT = "subscript"

# The access forms the index records. Syntax, never meaning: a method_call row
# says a call was written, not that the call reads anything.
ACCESS_KIND_SUBSCRIPT = "subscript"
ACCESS_KIND_METHOD_CALL = "method_call"

# Which method names are recorded as an access form. A scope decision about
# what to record, named here so adding one is a list entry -- never a claim
# about what these methods do.
INDEXED_ACCESS_METHODS = frozenset({"get"})

# What a selecting argument is, where one is written. Distinguishing these is
# the point: obj.get(name), obj.get(123) and obj.get() are three proven facts,
# and one nullable literal would collapse them into a single silence.
ARGUMENT_KIND_STRING_LITERAL = "string_literal"
ARGUMENT_KIND_OTHER_LITERAL = "other_literal"
ARGUMENT_KIND_EXPRESSION = "expression"


# Which grammar produced a class attribute row, and nothing else. It does not
# say whether storage exists: TypeScript's ``declare x: number`` and
# ``abstract y: string`` are field definitions that allocate nothing, and what
# the parser proves about them travels in ``modifiers`` for a reader to
# interpret. A form meaning "declares storage" would be an inference wearing a
# syntactic name.
DECLARATION_FORM_CLASS_ATTRIBUTE = "class_attribute"
DECLARATION_FORM_FIELD_DEFINITION = "field_definition"


def matches_any_glob(
    value: str,
    globs: Sequence[str],
    *,
    when_unconfigured: bool = False,
) -> bool:
    """Return whether a value matches any configured glob.

    The one implementation of ScanRules glob semantics, so the behaviour
    documented on ScanRules is the behaviour every consumer gets. Matching is
    case-sensitive fnmatch against a repository-relative POSIX path, which is
    why ``*`` crosses separators and there is no ``**``.

    Args:
        value: Text to match, normally a repository-relative POSIX path.
        globs: Configured patterns.
        when_unconfigured: What an empty pattern list means. False is the
            common case — nothing configured matches nothing. True is for a
            rule whose scope is optional, where declaring no scope means the
            rule applies everywhere rather than nowhere.

    Returns:
        True when at least one glob matches, or ``when_unconfigured`` when no
        globs are configured.
    """
    if not globs:
        return when_unconfigured
    return any(fnmatchcase(value, pattern) for pattern in globs)


def dotted_identity(source_path: str, qualified_name: str) -> str:
    """Return the dotted identity one structural record answers to.

    The file's path becomes its dotted module -- its extension dropped and a
    package ``__init__`` standing for the package itself -- and the symbol's
    qualified name follows it. ``Owner.member`` is already inside
    ``qualified_name``, so nothing here knows that a method has an owner.

    One rule for every language. It names no extension and tries no
    candidates, so a TypeScript identity resolves by the same arithmetic a
    Python one does and neither has a code path of its own.

    It lives here, beside the record it identifies, because it is the value an
    authored architecture reference is matched against -- in the projection,
    and in the code index's own storage. A second implementation of it would be
    a second answer to what a symbol is called.

    Args:
        source_path: The path the declaration is written in.
        qualified_name: The declaration's qualified name.

    Returns:
        The dotted identity, such as ``rey_lib.ai.ai.AI.execute``.
    """
    stem = PurePosixPath(source_path).with_suffix("")
    if stem.name == "__init__":
        stem = stem.parent
    return f"{str(stem).replace('/', '.')}.{qualified_name}"


@dataclass(frozen=True)
class SymbolRecord:
    """One syntax-confirmed declaration: top-level, or a method of a top-level class.

    A declaration nested inside a *function* body is never a declaration this
    record carries, and neither is a class nested inside another class. One
    level of class membership is recorded and no more (REQ-022, AC-003, as
    amended): architecture names a class's behaviour, never a closure's.

    Attributes:
        source_path: Path the declaration is written in.
        source_line: 1-indexed declaration line (REQ-023).
        source_column: 0-indexed declaration column.
        name: Declared name as written in source. A method carries its own
            name, so a consumer matching on ``name`` is unaffected by methods
            arriving.
        symbol_kind: One of the ``SYMBOL_KIND_*`` constants.
        exported: True when the name is publicly reachable. For a top-level
            declaration that is the module publishing it; for a method it is
            a public member of a published owner, so a private helper on an
            exported class is not exported.
        owner: The declaring class, empty at top level.
        end_line: Last line of the declaration, so a symbol is a span rather
            than a point and can be retrieved without re-parsing its file.
            Zero where the extractor cannot prove it, which is absent rather
            than a guess.
        returns_annotation: The declared return type as written, or None
            where none is declared. A *declaration*, never what the symbol
            returns: ``def f() -> int: return "x"`` declares ``int`` and
            returns a string, and only the first is a parser-proven fact.
            None rather than empty, so absence stays distinguishable from a
            declaration of nothing.
        end_column: Column the declaration ends at, **exclusive** -- both
            parsers report one position past the last character. With
            ``end_line`` it completes the span to a half-open
            ``[start, end)`` range, which is what lets one position be tested
            for containment. Line alone cannot separate two declarations
            sharing a line, and ``end_line`` used by itself is an inclusive
            line number: the difference is whether the column is present.
    """

    source_path: str
    source_line: int
    source_column: int
    name: str
    symbol_kind: str
    exported: bool = False
    owner: str = ""
    end_line: int = 0
    end_column: int = 0
    returns_annotation: Optional[str] = None

    @property
    def dotted_identity(self) -> str:
        """Return the identity an architecture reference resolves against."""
        return dotted_identity(self.source_path, self.qualified_name)

    @property
    def qualified_name(self) -> str:
        """Return the identity architecture addresses this declaration by.

        ``AI.execute`` rather than ``execute``, so a statement naming a method
        joins to that method and not to every function sharing its name.
        """
        return f"{self.owner}.{self.name}" if self.owner else self.name

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
            "owner": self.owner,
            "qualified_name": self.qualified_name,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "returns_annotation": self.returns_annotation,
            "dotted_identity": self.dotted_identity,
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
        from_symbol: Qualified name of the narrowest symbol positionally
            containing this reference, or empty. Empty means nothing contains
            it -- a module-level statement -- or that two candidates were
            equally narrow and neither was chosen. It never means the edge
            belongs to the file by some rule about its kind: attribution is
            positional, and an import written inside a function is inside that
            function like any other statement.
        from_symbol_line: That symbol's start line, and
        from_symbol_column: its start column. Carried because a qualified name
            is not unique within a file -- two declarations may share one --
            so the join to a symbol row is the whole tuple rather than the
            name alone.
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
    from_symbol: str = ""
    from_symbol_line: int = 0
    from_symbol_column: int = 0

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
            # Carried, not dropped. record_id has always embedded it, so an
            # edge that reached a consumer without it was identified by a
            # position the consumer could not see -- and every edge sharing a
            # line became indistinguishable from its neighbours.
            "source_column": self.source_column,
            "from": self.from_id,
            "from_symbol": self.from_symbol,
            "from_symbol_line": self.from_symbol_line,
            "from_symbol_column": self.from_symbol_column,
            "to": self.to,
            "edge_kind": self.edge_kind,
            "evidence": self.evidence,
        }


def attributed_edges(
    symbols: Sequence["SymbolRecord"],
    edges: Sequence["ReferenceEdge"],
) -> list["ReferenceEdge"]:
    """Return each edge carrying the narrowest symbol that positionally contains it.

    One rule for every language, applied once where both facts are in hand,
    rather than in each extractor. The extractors already prove the positions;
    nothing new is parsed here.

    **Containment is half-open**: ``start <= position < end``. Both parsers
    report an exclusive end column, so ``const x=1;const y=2;`` parses as
    ``[0,10)`` and ``[10,20)`` -- column 10 ends the first declaration and
    starts the second. An inclusive test would place a position there inside
    both, and choosing between two equally valid matches is a guess.

    **Narrowest** is the candidate that starts latest and ends earliest. Where
    no single candidate satisfies both, or where several share that exact span,
    the edge is left unattributed. Ties are refused rather than broken by
    iteration order: an answer that looks deterministic and depends on
    traversal is worse than no answer, because it is stable enough to be
    trusted and arbitrary enough to be wrong.

    **Nothing is excluded by edge kind.** A module-level import is unattributed
    because no symbol contains it; an import inside a function is attributed to
    that function, like any other statement written there.

    Args:
        symbols: The file's declarations, with proven spans.
        edges: The file's references.

    Returns:
        The edges, in order, each with its attribution filled where one exists.
    """
    spans = [
        (
            (symbol.source_line, symbol.source_column),
            (symbol.end_line, symbol.end_column),
            symbol,
        )
        for symbol in symbols
    ]
    return [_attributed(spans, edge) for edge in edges]


def _attributed(
    spans: Sequence[tuple[tuple[int, int], tuple[int, int], "SymbolRecord"]],
    edge: "ReferenceEdge",
) -> "ReferenceEdge":
    """Return one edge with its containing symbol, or unchanged when it has none.

    Args:
        spans: Every symbol's half-open span, with the symbol.
        edge: The reference to place.

    Returns:
        The edge, attributed where exactly one symbol is innermost.
    """
    position = (edge.source_line, edge.source_column)
    containing = [
        (opens, closes, symbol)
        for opens, closes, symbol in spans
        if opens <= position < closes
    ]
    if not containing:
        return edge

    innermost_opens = max(opens for opens, _closes, _symbol in containing)
    innermost_closes = min(closes for _opens, closes, _symbol in containing)
    winners = [
        symbol for opens, closes, symbol in containing
        if opens == innermost_opens and closes == innermost_closes
    ]
    if len(winners) != 1:
        # Either no candidate is innermost on both ends -- overlapping spans
        # that do not nest -- or several share one span. Neither is a fact.
        return edge

    winner = winners[0]
    return replace(
        edge,
        from_symbol=winner.qualified_name,
        from_symbol_line=winner.source_line,
        from_symbol_column=winner.source_column,
    )


@dataclass(frozen=True)
class ParameterRecord:
    """One parameter a declaration declares.

    Declared, not inferred. What a caller may pass is a language-level
    question; what the signature says is a syntactic one, and only the second
    is recorded here.

    Attributes:
        source_path: Path the owning declaration is written in.
        owner_line: The owning symbol's start line, and
        owner_column: its start column. A qualified name is not unique within
            a file, so the owner is addressed by name **and** position.
        owner_qualified_name: The owning declaration.
        name: The parameter as written.
        ordinal: Its position in the signature, from zero, in declaration
            order. Kept even though names exist, because order is itself a
            parser-proven fact and a caller passing positionally depends on it.
        parameter_kind: One of the ``PARAMETER_KIND_*`` constants.
        has_default: Whether the declaration carries a default expression.
            The expression itself is not stored -- that is source, and storing
            its text would blur the boundary this index keeps.
        is_optional: Whether the declaration explicitly permits omission --
            the syntactic ``?``, written or not. Independent of
            ``has_default``: TypeScript ``b?: T`` has one and not the other,
            and ``b = 2`` has the reverse.
        annotation: The declared type as written, or None where none is
            declared. None rather than empty, because an empty string is a
            value and absence must stay distinguishable from it.
    """

    source_path: str
    owner_qualified_name: str
    owner_line: int
    owner_column: int
    name: str
    ordinal: int
    parameter_kind: str
    has_default: bool = False
    is_optional: bool = False
    annotation: Optional[str] = None

    @property
    def record_id(self) -> str:
        """Return the stable identity of this parameter fact."""
        return (
            f"{RECORD_TYPE_PARAMETER}:{self.source_path}"
            f":{self.owner_qualified_name}:{self.owner_line}:{self.owner_column}"
            f":{self.ordinal}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this parameter as a JSONL 'parameter' record."""
        return {
            "record_type": RECORD_TYPE_PARAMETER,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "owner_qualified_name": self.owner_qualified_name,
            "owner_line": self.owner_line,
            "owner_column": self.owner_column,
            "name": self.name,
            "ordinal": self.ordinal,
            "parameter_kind": self.parameter_kind,
            "has_default": self.has_default,
            "is_optional": self.is_optional,
            "annotation": self.annotation,
        }


@dataclass(frozen=True)
class AssignmentRecord:
    """One syntactic write inside a recorded declaration.

    Writes only. What a call does to its receiver is not here: ``items.append``
    mutates and ``obj.get`` may not, and neither is provable from syntax.

    Attributes:
        source_path: Path the write is written in.
        owner_qualified_name: The declaration containing it, and
        owner_line / owner_column: its start position, because a qualified
            name is not unique within a file.
        source_line / source_column: Where the write is.
        target_kind: One of the ``TARGET_KIND_*`` constants.
        target_chain: The proven dotted chain the target is rooted in, or None
            when the expression is not a pure Name/Attribute chain. **Not the
            source as written** -- ``factory().state`` answers None rather than
            surrendering its text.
        attribute_name: The final attribute of an attribute target, kept
            because it survives an impure chain.
        key: A subscript target's key when it is a string literal, else None.
            None means the key exists and is not provably a literal.
        is_augmented: Whether the write is an augmented assignment.
    """

    source_path: str
    owner_qualified_name: str
    owner_line: int
    owner_column: int
    source_line: int
    source_column: int
    target_kind: str
    target_chain: Optional[str] = None
    attribute_name: Optional[str] = None
    key: Optional[str] = None
    is_augmented: bool = False

    @property
    def record_id(self) -> str:
        """Return the stable identity of this write."""
        return (
            f"{RECORD_TYPE_ASSIGNMENT}:{self.source_path}"
            f":{self.source_line}:{self.source_column}:{self.target_kind}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this write as a JSONL 'assignment' record."""
        return {
            "record_type": RECORD_TYPE_ASSIGNMENT,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "owner_qualified_name": self.owner_qualified_name,
            "owner_line": self.owner_line,
            "owner_column": self.owner_column,
            "source_line": self.source_line,
            "source_column": self.source_column,
            "target_kind": self.target_kind,
            "target_chain": self.target_chain,
            "attribute_name": self.attribute_name,
            "key": self.key,
            "is_augmented": self.is_augmented,
        }


@dataclass(frozen=True)
class AccessRecord:
    """One indexed access-form occurrence.

    Deliberately *not* "a read". A subscript read is one; a ``.get`` call is a
    call to an attribute named ``get``, and whether it reads anything is the
    receiver's business. Naming this a key read would put an interpretation in
    a column name, which is the line this record exists to hold.

    Attributes:
        source_path: Path the access is written in.
        owner_qualified_name: The declaration containing it, and
        owner_line / owner_column: its start position.
        source_line / source_column: Where the access is.
        access_kind: One of the ``ACCESS_KIND_*`` constants.
        object_chain: The receiver's proven dotted chain, or None when impure.
        attribute_name: The receiver's final attribute, kept when the chain is
            impure.
        method_name: The method called, for a method_call; None for a
            subscript.
        argument_present: Whether a selecting argument is written at all. Always
            true for a subscript, where the syntax guarantees one; a call may
            have none.
        argument_kind: One of the ``ARGUMENT_KIND_*`` constants, or None when
            no argument is written.
        literal_argument: The string literal itself, or None. The expression is
            never stored -- only that one was written.
    """

    source_path: str
    owner_qualified_name: str
    owner_line: int
    owner_column: int
    source_line: int
    source_column: int
    access_kind: str
    object_chain: Optional[str] = None
    attribute_name: Optional[str] = None
    method_name: Optional[str] = None
    argument_present: bool = True
    argument_kind: Optional[str] = None
    literal_argument: Optional[str] = None

    @property
    def record_id(self) -> str:
        """Return the stable identity of this access."""
        return (
            f"{RECORD_TYPE_ACCESS}:{self.source_path}"
            f":{self.source_line}:{self.source_column}:{self.access_kind}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this access as a JSONL 'access' record."""
        return {
            "record_type": RECORD_TYPE_ACCESS,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "owner_qualified_name": self.owner_qualified_name,
            "owner_line": self.owner_line,
            "owner_column": self.owner_column,
            "source_line": self.source_line,
            "source_column": self.source_column,
            "access_kind": self.access_kind,
            "object_chain": self.object_chain,
            "attribute_name": self.attribute_name,
            "method_name": self.method_name,
            "argument_present": self.argument_present,
            "argument_kind": self.argument_kind,
            "literal_argument": self.literal_argument,
        }


@dataclass(frozen=True)
class ClassAttributeRecord:
    """One name bound directly in a class body.

    Syntax, never meaning. Python has no field construct -- a dataclass field,
    a ``ClassVar``, a plain class attribute and ``__slots__`` are the same
    statement, and only a decorator elsewhere in the file separates them -- so
    this records what the class body binds and leaves the interpreting to a
    reader. ``__slots__`` needs no exception here; it is a class attribute.

    **The immediate body only.** A name bound under ``if`` / ``try`` / ``with``
    inside a class body, ``if TYPE_CHECKING:`` included, is conditionally
    bound, and "declares" would not be proven. Those are outside this surface,
    which the table and view say in their own comments so a consumer does not
    read absence as proof of absence.

    Attributes:
        source_path: Path the owning class is written in.
        owner_qualified_name: The class the attribute is bound in. The
            innermost one, where classes nest.
        owner_line: The owning class's start line, and
        owner_column: its start column. A qualified name is not unique within
            a file, so the owner is addressed by name **and** position.
        name: The attribute as written. TypeScript's ``#priv`` keeps its
            ``#``, which is part of the identifier token rather than a
            modifier, so ECMAScript-private needs no flag of its own.
        ordinal: Position in the class body, from zero, in declaration order.
        declaration_form: One of the ``DECLARATION_FORM_*`` constants. Grammar
            provenance only.
        is_annotated: Whether a type was written. Kept independent of
            ``annotation`` being present for the reason C1 kept ``has_default``
            independent of ``is_optional``: ``x: int`` and ``x = 1`` are
            different declarations, and one nullable column would blur them.
        has_default: Whether a value expression was written. The expression is
            not stored -- that is source.
        is_optional: The syntactic ``?``, written or not. False throughout
            Python, which has no such syntax.
        modifiers: The modifier keywords as written, in source order --
            ``static``, ``readonly``, ``abstract``, ``declare``, ``accessor``
            and the accessibility keywords. Empty for Python rather than a row
            of falses: Python has no ``readonly``, and ``is_readonly = False``
            would assert a distinction the language does not make, where an
            empty tuple says only that nothing applies.
    """

    source_path: str
    owner_qualified_name: str
    owner_line: int
    owner_column: int
    name: str
    ordinal: int
    declaration_form: str
    is_annotated: bool = False
    has_default: bool = False
    is_optional: bool = False
    annotation: Optional[str] = None
    modifiers: tuple[str, ...] = ()

    @property
    def record_id(self) -> str:
        """Return the stable identity of this class attribute fact."""
        return (
            f"{RECORD_TYPE_CLASS_ATTRIBUTE}:{self.source_path}"
            f":{self.owner_qualified_name}:{self.owner_line}:{self.owner_column}"
            f":{self.ordinal}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this attribute as a JSONL 'class_attribute' record."""
        return {
            "record_type": RECORD_TYPE_CLASS_ATTRIBUTE,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "owner_qualified_name": self.owner_qualified_name,
            "owner_line": self.owner_line,
            "owner_column": self.owner_column,
            "name": self.name,
            "ordinal": self.ordinal,
            "declaration_form": self.declaration_form,
            "is_annotated": self.is_annotated,
            "has_default": self.has_default,
            "is_optional": self.is_optional,
            "annotation": self.annotation,
            "modifiers": list(self.modifiers),
        }


@dataclass(frozen=True)
class FileRecord:
    """One inventoried source file and its deterministic classification.

    Attributes:
        path: Repository-relative POSIX path. The record identity and sort key.
        language: Configured language name, or ``LANGUAGE_UNKNOWN``.
        size_bytes: File size in bytes as reported by the filesystem.
        content_hash: SHA-256 of the file's bytes. What was indexed, exactly:
            a revision plus a working-tree status says whether a checkout is
            clean, and this says which bytes each file actually held.
        is_generated: True when the path matches a configured generated glob.
        is_vendor: True when the path matches a configured vendor glob.
        is_test: True when the path matches a configured test glob.
        entry_point_load_state: One of the ``ENTRY_POINT_LOAD_*`` constants.
    """

    path: str
    language: str
    size_bytes: int
    content_hash: str
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
            "content_hash": self.content_hash,
            "classification": {
                "generated": self.is_generated,
                "vendor": self.is_vendor,
                "test": self.is_test,
            },
            "entry_point_load_state": self.entry_point_load_state,
        }


@dataclass(frozen=True)
class DispatcherRecord:
    """One decision point that branches over a named behaviour vocabulary.

    Recorded as a fact, never as a verdict. A dispatcher is legitimate when it
    interprets a protocol or chooses between genuinely different mechanisms,
    and questionable when adding a new named thing means adding a branch here.
    Deciding which is architecture review's job, so ``classification`` is
    always ``unreviewed`` and this module never sets anything else.

    Attributes:
        source_path: Path the dispatcher is written in.
        source_line: 1-indexed line of the decision point.
        source_column: 0-indexed column of the decision point.
        symbol: Enclosing declaration, or '<module>' at module level.
        vocabulary: The expression being branched on, as written.
        branch_count: How many named branches the decision point has.
        branch_values: The literal values compared, sorted.
        callers: Paths whose executable references reach the enclosing symbol.
        classification: Always ``unreviewed``.
    """

    source_path: str
    source_line: int
    source_column: int
    symbol: str
    vocabulary: str
    branch_count: int
    branch_values: tuple[str, ...] = ()
    callers: tuple[str, ...] = ()
    classification: str = DISPATCHER_UNREVIEWED

    @property
    def record_id(self) -> str:
        """Return the stable identity of this dispatcher fact.

        Position is part of the identity because one function may hold more
        than one decision point.
        """
        return (
            f"{RECORD_TYPE_DISPATCHER}:{self.source_path}:{self.symbol}"
            f":{self.source_line}:{self.source_column}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this dispatcher as a JSONL 'dispatcher' record."""
        return {
            "record_type": RECORD_TYPE_DISPATCHER,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "symbol": self.symbol,
            "vocabulary": self.vocabulary,
            "branch_count": self.branch_count,
            "branch_values": list(self.branch_values),
            "callers": list(self.callers),
            "classification": self.classification,
        }


@dataclass(frozen=True)
class BoundaryRule:
    """One deterministic architecture guard, expressed as data.

    A rule says: within this scope, a reference to something forbidden is a
    violation unless the referring file is an allowed owner. Ownership is the
    exception, not the subject — which is why the sanctioned API of an owner
    can be called freely while the mechanism beneath it cannot.

    Attributes:
        rule_id: Stable identifier recorded on every violation.
        forbidden_target_globs: Globs matched against a reference target.
        allowed_path_globs: Files permitted to make the reference. Everything
            else in scope is a violation.
        scope_path_globs: Files the rule applies to. Both surviving frontend
            roots must be listed for a guard to cover them (REQ-093).
        edge_kinds: Reference kinds the rule applies to, empty for all.
    """

    rule_id: str
    forbidden_target_globs: tuple[str, ...]
    allowed_path_globs: tuple[str, ...] = ()
    scope_path_globs: tuple[str, ...] = ()
    edge_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicationRule:
    """A guard on what may be published onto the global object.

    A mechanism that is meant to be reached through one entry point must not
    also be reachable from a global surface, or the entry point is advisory.

    Attributes:
        rule_id: Stable identifier recorded on every violation.
        forbidden_global_globs: Globs matched against a published global name.
        allowed_path_globs: Files permitted to publish it.
        scope_path_globs: Files the rule applies to.
    """

    rule_id: str
    forbidden_global_globs: tuple[str, ...]
    allowed_path_globs: tuple[str, ...] = ()
    scope_path_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnershipRule:
    """A rule about how many places may own one registered thing.

    The other families ask who may reach what. This one asks how many owners a
    thing has, which the reference families cannot express: two registrations
    of one id are individually legal edges and only wrong together.

    An id that could not be read as a literal is never counted. A syntax
    scanner cannot follow a variable, so two unresolved ids might be the same
    id or different ones, and reporting a duplicate from that would be a guess.

    Attributes:
        rule_id: Stable identity of this rule.
        registry_globs: Registries this rule governs.
        registered_id_globs: Ids within those registries. Empty governs all.
        maximum_owners: How many registrations of one id are permitted.
        allowed_path_globs: Paths permitted to register at all. Empty permits
            any path, so cardinality alone is checked.
        scope_path_globs: Registration sites this rule looks at.
    """

    rule_id: str
    registry_globs: tuple[str, ...]
    registered_id_globs: tuple[str, ...] = ()
    maximum_owners: int = 1
    allowed_path_globs: tuple[str, ...] = ()
    scope_path_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatcherRule:
    """A guard on where a decision point over a vocabulary may live.

    The generator records that a dispatcher exists and what it branches on;
    this decides whether that is permitted. Which vocabularies are objectionable
    is policy data, never a judgement the scanner makes — a vocabulary is only
    forbidden because a repository says so here.

    Attributes:
        rule_id: Stable identifier recorded on every violation.
        forbidden_vocabulary_globs: Globs matched against the branched-on
            expression. Use '*' to forbid any dispatcher within the scope.
        allowed_path_globs: Locations where such a dispatcher is legitimate,
            typically the one central place that owns the decision.
        scope_path_globs: Files the rule applies to.
        minimum_branch_count: Branch count at or above which the rule bites,
            so a rule can target wide dispatch without flagging a two-way
            choice.
    """

    rule_id: str
    forbidden_vocabulary_globs: tuple[str, ...]
    allowed_path_globs: tuple[str, ...] = ()
    scope_path_globs: tuple[str, ...] = ()
    minimum_branch_count: int = 2


@dataclass(frozen=True)
class PresenceRule:
    """A guard on what must not exist or be loaded at all.

    Some boundaries are enforced by absence: a deleted namespace stays deleted,
    and nothing loads it. Absence is a structural fact, so the inventory and
    the entry points answer it directly.

    Attributes:
        rule_id: Stable identifier recorded on every violation.
        forbidden_path_globs: Paths that must not appear in the inventory.
        forbidden_entry_point_globs: Entry-point targets that must not be
            loaded by any window.
    """

    rule_id: str
    forbidden_path_globs: tuple[str, ...] = ()
    forbidden_entry_point_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViolationRecord:
    """One deterministic architecture-boundary violation.

    Attributes:
        source_path: Path the forbidden reference is written in.
        source_line: 1-indexed line of the reference.
        source_column: 0-indexed column of the reference.
        rule_id: The guard that was broken.
        caller: The file making the reference.
        callee: The forbidden target, as written.
        edge_kind: The kind of reference.
        evidence_record_ids: The facts proving the violation.
    """

    source_path: str
    source_line: int
    source_column: int
    rule_id: str
    caller: str
    callee: str
    edge_kind: str
    evidence_record_ids: tuple[str, ...] = ()

    @property
    def record_id(self) -> str:
        """Return the stable identity of this violation."""
        return (
            f"violation:{self.rule_id}:{self.source_path}"
            f":{self.source_line}:{self.source_column}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this violation as a JSONL 'architecture_violation' record."""
        return {
            "record_type": RECORD_TYPE_ARCHITECTURE_VIOLATION,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "rule_id": self.rule_id,
            "caller": self.caller,
            "callee": self.callee,
            "edge_kind": self.edge_kind,
            "evidence_record_ids": list(self.evidence_record_ids),
        }


@dataclass(frozen=True)
class ReachabilityRecord:
    """Why one target is considered reachable, or why it is a candidate.

    Attributes:
        target: The node this verdict is about, as a record_id.
        status: One of the ``REACHABILITY_*`` constants. Never 'dead'.
        root: The runtime root the target was reached from, empty when none.
        evidence_record_ids: The facts forming the path from root to target,
            so a verdict can always be checked against source.
    """

    target: str
    status: str
    root: str
    evidence_record_ids: tuple[str, ...] = ()

    @property
    def record_id(self) -> str:
        """Return the stable identity of this reachability verdict."""
        return f"{RECORD_TYPE_REACHABILITY}:{self.target}"

    def to_dict(self) -> dict[str, Any]:
        """Return this verdict as a JSONL 'reachability' record."""
        return {
            "record_type": RECORD_TYPE_REACHABILITY,
            "record_id": self.record_id,
            "target": self.target,
            "status": self.status,
            "root": self.root,
            "evidence_record_ids": list(self.evidence_record_ids),
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
    # Rule families keyed by the configuration section they are declared
    # under. One field rather than one per family, so a new family adds no
    # attribute here and no branch anywhere that reads them.
    rule_sets: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    registration_rules: tuple[RegistrationRule, ...] = ()
    declared_registration_rules: tuple[DeclaredRegistrationRule, ...] = ()
    backend_registration_rules: tuple[BackendRegistrationRule, ...] = ()
    template_globs: tuple[str, ...] = ()
    bundle_globs: tuple[str, ...] = ()
    primary_template: str | None = None
    # Process entry points that no template declares — the backend equivalent
    # of a bootstrapping window. Without these the whole server side is
    # unreachable by construction rather than by evidence.
    runtime_entry_paths: tuple[str, ...] = ()
    # Graph resolution (INC-004). A written specifier only resolves to a file
    # that the inventory already contains; nothing is guessed into existence.
    module_extensions: tuple[str, ...] = ()
    module_index_files: tuple[str, ...] = ()
    url_path_prefixes: dict[str, str] = field(default_factory=dict)
    backend_path_prefixes: dict[str, str] = field(default_factory=dict)

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
    def unconfigured(cls) -> "ScanRules":
        """Return the rules of a repository that declares none.

        A repository may legitimately have no rules file. It still yields a
        file inventory; it simply maps no language, so no extractor claims any
        file and no policy is evaluated. Named rather than constructed inline
        so "this repository declares nothing" is a state a reader can see.

        Returns:
            Rules with every section empty.
        """
        return cls(
            ignored_directory_names=frozenset(),
            ignored_path_globs=(),
            language_by_extension={},
            generated_path_globs=(),
            vendor_path_globs=(),
            test_path_globs=(),
        )

    def rules_for(self, config_key: str) -> tuple[Any, ...]:
        """Return the declared rules of one family.

        Args:
            config_key: The section the family is declared under.

        Returns:
            The typed rules, empty when the repository declares none.
        """
        return self.rule_sets.get(config_key, ())

    @property
    def declares_any_policy(self) -> bool:
        """Return whether this repository declares architectural policy at all.

        Asked without naming a family, so the answer stays correct when a
        family is added.
        """
        return any(self.rule_sets.values())

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        rule_families: "Sequence[Any]" = (),
    ) -> "ScanRules":
        """Build rules from a parsed rules mapping.

        Args:
            data: Parsed contents of a repository's rules file.
            rule_families: The rule-family registry. Supplied by the caller so
                this module needs no knowledge of any concrete family.

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
            rule_sets={
                family.config_key: family.build(_rule_entries(data, family.config_key))
                for family in rule_families
            },
            registration_rules=_registration_rules(data),
            declared_registration_rules=_declared_registration_rules(data),
            backend_registration_rules=_backend_registration_rules(data),
            template_globs=tuple(_require_str_list(data, "template_globs")),
            bundle_globs=tuple(_require_str_list(data, "bundle_globs")),
            primary_template=data.get("primary_template"),
            runtime_entry_paths=tuple(_require_str_list(data, "runtime_entry_paths")),
            module_extensions=tuple(_require_str_list(data, "module_extensions")),
            module_index_files=tuple(_require_str_list(data, "module_index_files")),
            url_path_prefixes=_require_str_mapping(data, "url_path_prefixes"),
            backend_path_prefixes=_require_str_mapping(data, "backend_path_prefixes"),
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
