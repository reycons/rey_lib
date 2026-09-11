"""Syntax-aware Python symbol and reference extraction.

Contract: rey_repository_map_generator.sgc.yaml (INC-002A).

Everything here is derived from the ``ast`` parse tree, never from text
matching. That is what makes comments, docstrings and string literals
structurally incapable of producing a symbol or an edge: the parser does not
hand them to us as executable nodes in the first place.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from rey_lib.files.file_utils import read_text_file
from rey_lib.repository_map.records import (
    EDGE_KIND_CALL,
    EDGE_KIND_IMPORT,
    EDGE_KIND_PROPERTY_ACCESS,
    EDGE_KIND_RE_EXPORT,
    RECORD_TYPE_FILE,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_EXPORT,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_METHOD,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_VARIABLE,
    PARAMETER_KIND_KEYWORD_ONLY,
    PARAMETER_KIND_POSITIONAL,
    PARAMETER_KIND_POSITIONAL_ONLY,
    PARAMETER_KIND_VAR_KEYWORD,
    PARAMETER_KIND_VAR_POSITIONAL,
    ACCESS_KIND_METHOD_CALL,
    ACCESS_KIND_SUBSCRIPT,
    ARGUMENT_KIND_EXPRESSION,
    ARGUMENT_KIND_OTHER_LITERAL,
    ARGUMENT_KIND_STRING_LITERAL,
    INDEXED_ACCESS_METHODS,
    TARGET_KIND_ATTRIBUTE,
    TARGET_KIND_NAME,
    TARGET_KIND_SUBSCRIPT,
    DECLARATION_FORM_CLASS_ATTRIBUTE,
    AccessRecord,
    AssignmentRecord,
    ClassAttributeRecord,
    ParameterRecord,
    ReferenceEdge,
    SymbolInventory,
    SymbolRecord,
)

__all__ = [
    "extract_python_class_attributes",
    "extract_python_parameters",
    "extract_python_writes_and_accesses",
    "extract_python_references",
    "extract_python_symbols",
]

# Chains rooted at these names describe an object's own internals and never
# cross a file boundary, so neither self.attr nor self.method() is recorded.
# File reachability, which is what the graph answers, cannot turn on them.
_SELF_ROOTS = frozenset({"self", "cls"})


@dataclass(frozen=True)
class _Import:
    """One binding introduced by an import statement.

    Internal to Python extraction: an import is not itself a symbol record, it
    produces an import edge and, when republished, a re-export.

    Attributes:
        module: Module the binding comes from, with leading dots preserved.
        name: Imported member, or None for a whole-module import.
        alias: Local binding name when it differs from ``name``.
        line: 1-indexed line of the import statement.
        column: 0-indexed column of the import statement.
        end_line: Last line of the import statement. A republished import
            becomes a re_export symbol located here, and that symbol needs a
            proven span like any other.
        end_column: Exclusive end column of that statement, completing the
            span to a half-open range.
    """

    module: str
    name: str | None
    alias: str | None
    line: int
    column: int
    end_line: int = 0
    end_column: int = 0

    @property
    def local_name(self) -> str:
        """Return the name this import binds in the importing module."""
        if self.alias is not None:
            return self.alias
        if self.name is not None:
            return self.name
        # 'import a.b' binds the root package name.
        return self.module.lstrip(".").split(".", 1)[0]

    @property
    def target(self) -> str:
        """Return the dotted thing being imported."""
        return self.module if self.name is None else f"{self.module}.{self.name}"


def extract_python_symbols(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> SymbolInventory:
    """Extract a Python file's declarations.

    ``module.body``, and one level further: the methods a top-level class
    declares. Nothing inside a function body reaches the inventory, and neither
    does a class nested inside a class (REQ-022, as amended) -- architecture
    names a class's behaviour, never a closure's.

    Each declared name yields exactly one symbol record. A name listed in
    ``__all__`` is marked exported rather than duplicated as a second record;
    an exported name bound by an import becomes a re_export; an exported name
    that is neither declared nor imported becomes a bare export, so ``__all__``
    stays fully represented without inventing a declaration site.

    Args:
        path: Python file to read and parse.
        language: Language name recorded on the inventory.
        source_path: Path to record. Defaults to ``path`` in POSIX form.

    Returns:
        The file's top-level symbol inventory.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    tree = _parse(path)
    recorded_path = source_path if source_path is not None else path.as_posix()
    exported_names = _collect_dunder_all(tree)
    imports = _collect_imports(tree)
    imported_names = {record.local_name: record for record in imports}

    symbols: list[SymbolRecord] = []
    declared_names: set[str] = set()

    for node in tree.body:
        for name, line, column, kind, end_position in _declarations(node):
            declared_names.add(name)
            symbols.append(
                SymbolRecord(
                    source_path=recorded_path,
                    source_line=line,
                    source_column=column,
                    name=name,
                    symbol_kind=kind,
                    exported=name in exported_names,
                    returns_annotation=(
                        ast.unparse(node.returns)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.returns is not None else None
                    ),
                    end_line=end_position[0],
                    end_column=end_position[1],
                )
            )
        if isinstance(node, ast.ClassDef):
            # A method is published when its owner is and the method is part of
            # the class's public surface. Copying the owner's bit would say a
            # private helper is exported because the class it hides in is.
            owner_exported = node.name in exported_names
            for member, line, column, end_position in _methods(node):
                symbols.append(
                    SymbolRecord(
                        source_path=recorded_path,
                        source_line=line,
                        source_column=column,
                        name=member,
                        symbol_kind=SYMBOL_KIND_METHOD,
                        exported=owner_exported and not member.startswith("_"),
                        owner=node.name,
                        returns_annotation=_member_return(node, member),
                        end_line=end_position[0],
                    end_column=end_position[1],
                    )
                )

    dunder_all_line, dunder_all_column, dunder_all_end = _dunder_all_location(tree)
    for name in sorted(exported_names - declared_names):
        imported = imported_names.get(name)
        # The span is the syntax that establishes the name: the import
        # statement for a republished one, the __all__ assignment for a name
        # published with no declaration site. Both are proved, neither is the
        # declaration of the thing itself -- which is in another module, and is
        # exactly what a re_export says.
        symbols.append(
            SymbolRecord(
                source_path=recorded_path,
                source_line=imported.line if imported else dunder_all_line,
                source_column=imported.column if imported else dunder_all_column,
                name=name,
                symbol_kind=SYMBOL_KIND_RE_EXPORT if imported else SYMBOL_KIND_EXPORT,
                exported=True,
                end_line=(imported.end_line if imported else dunder_all_end[0]),
                end_column=(imported.end_column if imported else dunder_all_end[1]),
            )
        )

    symbols.sort(key=lambda symbol: (symbol.source_line, symbol.source_column, symbol.name))
    return SymbolInventory(path=recorded_path, language=language, symbols=tuple(symbols))


def extract_python_references(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ReferenceEdge]:
    """Extract executable references from a Python file.

    Calls come from ``ast.Call`` nodes alone (REQ-034), so a function name
    written in prose, a docstring or a string literal produces nothing
    (REQ-032, REQ-033).

    Args:
        path: Python file to read and parse.
        language: Language name. Accepted for registry symmetry; unused.
        source_path: Path to record on each edge. Defaults to POSIX ``path``.

    Returns:
        Edges sorted by line, column, kind and target.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    del language  # Recorded on the inventory, not on individual edges.
    tree = _parse(path)
    recorded_path = source_path if source_path is not None else path.as_posix()
    # Edges are attributed to the file that contains them. Resolving the
    # enclosing symbol, and the target, is graph work in INC-004.
    from_id = f"{RECORD_TYPE_FILE}:{recorded_path}"
    exported_names = _collect_dunder_all(tree)

    # A member call such as mod.fn() is one call edge, not a call plus a
    # property access, so the callee expression is excluded from attribute
    # reporting below.
    callee_nodes = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    edges: list[ReferenceEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _dotted_name(node.func) or ast.unparse(node.func)
            if not _is_internal(target):
                edges.append(
                    _edge(recorded_path, node, from_id, target, EDGE_KIND_CALL, "ast.Call")
                )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for record in _import_records(node):
                edges.append(
                    ReferenceEdge(
                        source_path=recorded_path,
                        source_line=record.line,
                        source_column=record.column,
                        from_id=from_id,
                        to=record.target,
                        edge_kind=EDGE_KIND_IMPORT,
                        evidence=f"ast.{type(node).__name__}",
                    )
                )
                if record.local_name in exported_names:
                    edges.append(
                        ReferenceEdge(
                            source_path=recorded_path,
                            source_line=record.line,
                            source_column=record.column,
                            from_id=from_id,
                            to=record.target,
                            edge_kind=EDGE_KIND_RE_EXPORT,
                            evidence="__all__",
                        )
                    )
        elif isinstance(node, ast.Attribute) and id(node) not in callee_nodes:
            target = _dotted_name(node)
            if target is not None and not _is_internal(target):
                edges.append(
                    _edge(
                        recorded_path,
                        node,
                        from_id,
                        target,
                        EDGE_KIND_PROPERTY_ACCESS,
                        "ast.Attribute",
                    )
                )

    edges.sort(key=lambda edge: (edge.source_line, edge.source_column, edge.edge_kind, edge.to))
    return edges


def _edge(
    recorded_path: str,
    node: ast.AST,
    from_id: str,
    target: str,
    edge_kind: str,
    evidence: str,
) -> ReferenceEdge:
    """Build one reference edge from a located syntax node.

    Args:
        recorded_path: Path to record on the edge.
        node: The syntax node proving the reference.
        from_id: record_id of the fact making the reference.
        target: Referenced name as written.
        edge_kind: One of the ``EDGE_KIND_*`` constants.
        evidence: The syntax node kind proving the reference.

    Returns:
        The edge.
    """
    return ReferenceEdge(
        source_path=recorded_path,
        source_line=getattr(node, "lineno", 0),
        source_column=getattr(node, "col_offset", 0),
        from_id=from_id,
        to=target,
        edge_kind=edge_kind,
        evidence=evidence,
    )


def _methods(node: ast.ClassDef) -> list[tuple[str, int, int, tuple[int, int]]]:
    """Return the methods one class declares, in source order.

    Its own body only. A class nested inside this one is not walked: its
    methods are two levels of ownership away, and a qualified name carrying one
    owner could not address them without saying something false.

    Args:
        node: A top-level class definition.

    Returns:
        Tuples of method name, line, column and end position.
    """
    return [
        (member.name, member.lineno, member.col_offset, _end_position(member))
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _member_return(owner: ast.ClassDef, member: str) -> str | None:
    """Return one method's declared return annotation, or None.

    Args:
        owner: The declaring class.
        member: The method name.

    Returns:
        The annotation as written, or None where none is declared.
    """
    for node in owner.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == member and node.returns is not None):
            return ast.unparse(node.returns)
    return None


def _end_position(node: ast.AST) -> tuple[int, int]:
    """Return the position a node ends at.

    Both values are optional on the AST, so a node without them answers zeros
    rather than a computed guess: a span that was not proved is absent, and a
    reader can tell the difference.

    The column is **exclusive** -- ``end_col_offset`` is one past the last
    character, so ``(end_line, end_column)`` is the open end of a half-open
    span. That is what lets a position on a boundary belong to exactly one
    declaration when two share a line.

    Args:
        node: Any parsed node.

    Returns:
        Last line and exclusive end column, or zeros when the parse recorded
        neither.
    """
    return (
        int(getattr(node, "end_lineno", 0) or 0),
        int(getattr(node, "end_col_offset", 0) or 0),
    )


def _declarations(node: ast.stmt) -> list[tuple[str, int, int, str, tuple[int, int]]]:
    """Return the top-level declarations one module-body statement makes.

    Args:
        node: A statement from ``module.body``.

    Returns:
        Tuples of name, line, column, symbol kind and end position. Empty for
        statements that declare nothing. Assignment targets carry their own
        position, so two names bound on one line stay distinguishable.

        The span of a binding is the statement's, not the target's: a name
        bound by a multi-line assignment ends where the assignment ends, and
        the target itself occupies only its own name.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [
            (
                node.name, node.lineno, node.col_offset,
                SYMBOL_KIND_FUNCTION, _end_position(node),
            )
        ]
    if isinstance(node, ast.ClassDef):
        return [
            (
                node.name, node.lineno, node.col_offset,
                SYMBOL_KIND_CLASS, _end_position(node),
            )
        ]
    if isinstance(node, ast.Assign):
        return [
            (
                target.id, target.lineno, target.col_offset,
                SYMBOL_KIND_VARIABLE, _end_position(node),
            )
            for target in node.targets
            if isinstance(target, ast.Name) and target.id != "__all__"
        ]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [
            (
                node.target.id,
                node.target.lineno,
                node.target.col_offset,
                SYMBOL_KIND_VARIABLE,
                _end_position(node),
            )
        ]
    return []


def extract_python_parameters(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ParameterRecord]:
    """Extract every parameter declared by a top-level function or method.

    The same boundary the symbol inventory keeps: module-level declarations and
    one level of methods. A closure's parameters are not an addressable
    identity, so they are not recorded.

    Declaration order is preserved across all five forms, which is what
    ``ordinal`` means. Python writes them positional-only, positional,
    ``*args``, keyword-only, ``**kwargs``, and that is the order a caller sees.

    Args:
        path: Python file to read and parse.
        language: Language name. Accepted for registry symmetry; unused.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The parameters, in declaration order within each declaration.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    tree = _parse(path)
    recorded_path = source_path if source_path is not None else path.as_posix()

    parameters: list[ParameterRecord] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters.extend(_parameters_of(recorded_path, node, node.name))
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parameters.extend(
                        _parameters_of(
                            recorded_path, member, f"{node.name}.{member.name}"
                        )
                    )
    return parameters


def extract_python_class_attributes(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ClassAttributeRecord]:
    """Extract every name bound directly in a top-level class body.

    The same boundary the symbol inventory keeps. It records top-level classes
    and one level of methods, so a nested class is not an addressable identity
    and an attribute owned by one would name an owner the index does not hold.
    Nested class bodies are therefore not walked.

    **The immediate body only.** A binding under ``if`` / ``try`` / ``with``,
    ``if TYPE_CHECKING:`` included, is conditional, and "declares" would not be
    proven. Tuple targets are refused for the same reason: ``a, b = 1, 2`` does
    not prove which name receives which value, so ``has_default`` would be a
    claim about unpacking rather than about syntax.

    ``__slots__`` is an ordinary row. It is a class-body assignment, and
    deciding it is not really an attribute would be interpretation.

    Args:
        path: Python file to read and parse.
        language: Language name. Accepted for registry symmetry; unused.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The attributes, in declaration order within each class body.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    tree = _parse(path)
    recorded_path = source_path if source_path is not None else path.as_posix()

    attributes: list[ClassAttributeRecord] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            attributes.extend(_class_attributes_of(recorded_path, node))
    return attributes


def _class_attributes_of(
    recorded_path: str,
    node: ast.ClassDef,
) -> list[ClassAttributeRecord]:
    """Return one class body's bound names, in the order they are written.

    Args:
        recorded_path: Path to record.
        node: The class whose immediate body is read.

    Returns:
        The attribute records. Statements that bind nothing, and targets the
        parser does not resolve to a single name, produce none.
    """
    attributes: list[ClassAttributeRecord] = []
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign):
            if not isinstance(statement.target, ast.Name):
                continue
            attributes.append(
                _class_attribute(
                    recorded_path, node, statement.target.id, len(attributes),
                    is_annotated=True,
                    has_default=statement.value is not None,
                    annotation=ast.unparse(statement.annotation),
                )
            )
        elif isinstance(statement, ast.Assign):
            # Chained bare names each bind, so each is a row: a = b = 1 binds
            # both. A tuple or attribute target binds something this concept
            # does not cover and is passed over.
            for target in statement.targets:
                if not isinstance(target, ast.Name):
                    continue
                attributes.append(
                    _class_attribute(
                        recorded_path, node, target.id, len(attributes),
                        is_annotated=False,
                        has_default=True,
                        annotation=None,
                    )
                )
    return attributes


def _class_attribute(
    recorded_path: str,
    node: ast.ClassDef,
    name: str,
    ordinal: int,
    *,
    is_annotated: bool,
    has_default: bool,
    annotation: str | None,
) -> ClassAttributeRecord:
    """Return one class attribute record, owned by the class that binds it.

    Args:
        recorded_path: Path to record.
        node: The owning class.
        name: The attribute as written.
        ordinal: Its position among the recorded attributes of this body.
        is_annotated: Whether a type was written.
        has_default: Whether a value expression was written.
        annotation: The declared type as written, or None.

    Returns:
        The record.
    """
    return ClassAttributeRecord(
        source_path=recorded_path,
        owner_qualified_name=node.name,
        owner_line=node.lineno,
        owner_column=node.col_offset,
        name=name,
        ordinal=ordinal,
        declaration_form=DECLARATION_FORM_CLASS_ATTRIBUTE,
        is_annotated=is_annotated,
        has_default=has_default,
        # Python has no syntactic optional marker on a class attribute.
        is_optional=False,
        annotation=annotation,
        # Python has no field modifiers. ClassVar and Final are annotation
        # text and stay in `annotation` rather than being decomposed here.
        modifiers=(),
    )


def _parameters_of(
    recorded_path: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
) -> list[ParameterRecord]:
    """Return one declaration's parameters, in the order they are written.

    Args:
        recorded_path: Path to record.
        node: The function or method.
        qualified_name: How the owning declaration is addressed.

    Returns:
        The parameter records.
    """
    arguments = node.args
    # Defaults are right-aligned against positional parameters: the last N
    # positional forms carry them. Keyword-only defaults are positional in
    # their own list, with None where a parameter has none.
    positional = arguments.posonlyargs + arguments.args
    defaulted_from = len(positional) - len(arguments.defaults)
    keyword_defaults = {
        argument.arg: default is not None
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
    }

    written: list[tuple[ast.arg, str, bool]] = []
    for index, argument in enumerate(arguments.posonlyargs):
        written.append((argument, PARAMETER_KIND_POSITIONAL_ONLY,
                        index >= defaulted_from))
    for index, argument in enumerate(arguments.args, start=len(arguments.posonlyargs)):
        written.append((argument, PARAMETER_KIND_POSITIONAL, index >= defaulted_from))
    if arguments.vararg is not None:
        written.append((arguments.vararg, PARAMETER_KIND_VAR_POSITIONAL, False))
    for argument in arguments.kwonlyargs:
        written.append((argument, PARAMETER_KIND_KEYWORD_ONLY,
                        keyword_defaults.get(argument.arg, False)))
    if arguments.kwarg is not None:
        written.append((arguments.kwarg, PARAMETER_KIND_VAR_KEYWORD, False))

    return [
        ParameterRecord(
            source_path=recorded_path,
            owner_qualified_name=qualified_name,
            owner_line=node.lineno,
            owner_column=node.col_offset,
            name=argument.arg,
            ordinal=ordinal,
            parameter_kind=kind,
            has_default=has_default,
            # Python has no syntactic optional marker; a default is the only
            # way a parameter may be omitted, and that is has_default's fact.
            is_optional=False,
            annotation=(ast.unparse(argument.annotation)
                        if argument.annotation is not None else None),
        )
        for ordinal, (argument, kind, has_default) in enumerate(written)
    ]


def extract_python_writes_and_accesses(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> tuple[list[AssignmentRecord], list[AccessRecord]]:
    """Extract the writes and indexed access forms inside recorded declarations.

    The inventory's boundary again: module-level functions and one level of
    methods. A closure's writes are not an addressable identity.

    Both are produced from one walk because both are found in the same bodies,
    and walking twice would parse the same tree for the same facts.

    Args:
        path: Python file to read and parse.
        language: Language name. Accepted for registry symmetry; unused.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The writes and the accesses, in source order.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    tree = _parse(path)
    recorded_path = source_path if source_path is not None else path.as_posix()

    writes: list[AssignmentRecord] = []
    accesses: list[AccessRecord] = []
    for owner, qualified_name in _recorded_bodies(tree):
        for node in ast.walk(owner):
            writes.extend(_writes_of(recorded_path, node, owner, qualified_name))
            access = _access_of(recorded_path, node, owner, qualified_name)
            if access is not None:
                accesses.append(access)
    return writes, accesses


def _recorded_bodies(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Return the declarations whose insides are recorded, with their names."""
    bodies: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies.append((node, node.name))
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bodies.append((member, f"{node.name}.{member.name}"))
    return bodies


def _target_facts(target: ast.expr) -> tuple[str, str | None, str | None, str | None] | None:
    """Return what a write target proves, or None when it is not a target shape.

    Nothing here stores the expression. ``target_chain`` is the proven dotted
    chain and is None the moment the expression stops being one, which is what
    keeps ``factory().state`` out of the index as text.

    Args:
        target: The assignment target node.

    Returns:
        Kind, dotted chain, final attribute and literal key -- each None where
        the parser does not prove it.
    """
    if isinstance(target, ast.Name):
        return TARGET_KIND_NAME, target.id, None, None
    if isinstance(target, ast.Attribute):
        return (TARGET_KIND_ATTRIBUTE, _dotted_name(target), target.attr, None)
    if isinstance(target, ast.Subscript):
        key = (target.slice.value
               if isinstance(target.slice, ast.Constant)
               and isinstance(target.slice.value, str) else None)
        base = target.value
        return (
            TARGET_KIND_SUBSCRIPT,
            _dotted_name(base),
            base.attr if isinstance(base, ast.Attribute) else None,
            key,
        )
    return None


def _writes_of(
    recorded_path: str,
    node: ast.AST,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
) -> list[AssignmentRecord]:
    """Return the writes one statement performs.

    Args:
        recorded_path: Path to record.
        node: Any node inside a recorded declaration.
        owner: The declaration containing it.
        qualified_name: How that declaration is addressed.

    Returns:
        One record per written target.
    """
    if isinstance(node, ast.Assign):
        targets, augmented = node.targets, False
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets, augmented = [node.target], isinstance(node, ast.AugAssign)
    else:
        return []

    records: list[AssignmentRecord] = []
    for target in targets:
        facts = _target_facts(target)
        if facts is None:
            continue
        kind, chain, attribute, key = facts
        records.append(AssignmentRecord(
            source_path=recorded_path,
            owner_qualified_name=qualified_name,
            owner_line=owner.lineno,
            owner_column=owner.col_offset,
            source_line=target.lineno,
            source_column=target.col_offset,
            target_kind=kind,
            target_chain=chain,
            attribute_name=attribute,
            key=key,
            is_augmented=augmented,
        ))
    return records


def _argument_facts(argument: ast.expr | None) -> tuple[bool, str | None, str | None]:
    """Return what a selecting argument proves.

    Three states the parser distinguishes and one nullable literal would not:
    a string literal, some other literal, and an expression. Absence is a
    fourth.

    Args:
        argument: The argument node, or None when none is written.

    Returns:
        Whether one is present, its kind, and the string literal where it is
        one.
    """
    if argument is None:
        return False, None, None
    if isinstance(argument, ast.Constant):
        if isinstance(argument.value, str):
            return True, ARGUMENT_KIND_STRING_LITERAL, argument.value
        return True, ARGUMENT_KIND_OTHER_LITERAL, None
    return True, ARGUMENT_KIND_EXPRESSION, None


def _access_of(
    recorded_path: str,
    node: ast.AST,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
) -> AccessRecord | None:
    """Return the access form one node is, or None.

    A subscript in load position, or a call to one of the indexed method names.
    The call record says a call was written with this argument -- never that it
    read anything.

    Args:
        recorded_path: Path to record.
        node: Any node inside a recorded declaration.
        owner: The declaration containing it.
        qualified_name: How that declaration is addressed.

    Returns:
        The access, or None when the node is neither form.
    """
    common = {
        "source_path": recorded_path,
        "owner_qualified_name": qualified_name,
        "owner_line": owner.lineno,
        "owner_column": owner.col_offset,
        "source_line": getattr(node, "lineno", 0),
        "source_column": getattr(node, "col_offset", 0),
    }
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        present, kind, literal = _argument_facts(node.slice)
        base = node.value
        return AccessRecord(
            **common,
            access_kind=ACCESS_KIND_SUBSCRIPT,
            object_chain=_dotted_name(base),
            attribute_name=base.attr if isinstance(base, ast.Attribute) else None,
            # A subscript always writes a key expression; only its identity
            # can be unknown.
            argument_present=True,
            argument_kind=kind,
            literal_argument=literal,
        )
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in INDEXED_ACCESS_METHODS):
        present, kind, literal = _argument_facts(node.args[0] if node.args else None)
        receiver = node.func.value
        return AccessRecord(
            **common,
            access_kind=ACCESS_KIND_METHOD_CALL,
            object_chain=_dotted_name(receiver),
            attribute_name=(receiver.attr
                            if isinstance(receiver, ast.Attribute) else None),
            method_name=node.func.attr,
            argument_present=present,
            argument_kind=kind,
            literal_argument=literal,
        )
    return None


def _parse(path: Path) -> ast.Module:
    """Parse a Python file into a module tree.

    Args:
        path: File to read and parse.

    Returns:
        The parsed module.

    Raises:
        ValueError: If the file cannot be parsed, naming the offending file.
    """
    text = read_text_file(path)
    try:
        return ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"Could not parse Python file {path}: {exc}") from exc


def _dunder_all_node(tree: ast.Module) -> ast.Assign | None:
    """Return the module-level ``__all__`` assignment, if any.

    Args:
        tree: Parsed module.

    Returns:
        The assignment node, or None.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            return node
    return None


def _dunder_all_location(tree: ast.Module) -> tuple[int, int, tuple[int, int]]:
    """Return the position and extent of the ``__all__`` assignment.

    Args:
        tree: Parsed module.

    Returns:
        Line, column and end position, defaulting to the start of file when
        ``__all__`` is absent. Only names published without a declaration site
        use this, and the extent is the assignment's own: it is the syntax that
        establishes such a name, so it is the syntax the name spans.
    """
    node = _dunder_all_node(tree)
    if node is None:
        return 1, 0, (1, 0)
    return node.lineno, node.col_offset, _end_position(node)


def _collect_dunder_all(tree: ast.Module) -> frozenset[str]:
    """Return the names listed in a module-level ``__all__``.

    Args:
        tree: Parsed module.

    Returns:
        Exported names, empty when the module declares no ``__all__`` or
        declares one that is not a literal list or tuple of strings.
    """
    node = _dunder_all_node(tree)
    if node is None or not isinstance(node.value, (ast.List, ast.Tuple)):
        return frozenset()
    return frozenset(
        element.value
        for element in node.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )


def _collect_imports(tree: ast.Module) -> list[_Import]:
    """Return every binding introduced by module-level imports.

    Args:
        tree: Parsed module.

    Returns:
        The import bindings, in source order.
    """
    records: list[_Import] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            records.extend(_import_records(node))
    return records


def _import_records(node: ast.Import | ast.ImportFrom) -> list[_Import]:
    """Return one record per binding introduced by an import statement.

    Args:
        node: An ``import`` or ``from ... import`` statement.

    Returns:
        The bindings the statement introduces.
    """
    if isinstance(node, ast.Import):
        return [
            _Import(
                module=alias.name,
                name=None,
                alias=alias.asname,
                line=node.lineno,
                column=node.col_offset,
                end_line=_end_position(node)[0],
                end_column=_end_position(node)[1],
            )
            for alias in node.names
        ]
    # A relative import records its dots so 'from . import x' stays distinct
    # from an absolute import of the same trailing name.
    module = "." * node.level + (node.module or "")
    return [
        _Import(
            module=module,
            name=alias.name,
            alias=alias.asname,
            line=node.lineno,
            column=node.col_offset,
            end_line=_end_position(node)[0],
            end_column=_end_position(node)[1],
        )
        for alias in node.names
    ]


def _is_internal(target: str) -> bool:
    """Return True when a reference target is rooted at self or cls.

    Args:
        target: Dotted reference target.

    Returns:
        True when the reference stays inside the owning object.
    """
    return target.split(".", 1)[0] in _SELF_ROOTS


def _dotted_name(node: ast.AST) -> str | None:
    """Return the dotted name for a pure Name/Attribute chain.

    Args:
        node: Expression node to describe.

    Returns:
        The dotted name, or None when the expression is not a plain chain (a
        subscript or call result, for example).
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))
