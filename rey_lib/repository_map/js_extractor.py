"""Syntax-aware JavaScript, TypeScript and TSX symbol and reference extraction.

Contract: rey_repository_map_generator.sgc.yaml (INC-002B).

Facts are derived from a Tree-sitter parse tree, never from text matching. A
comment is a ``comment`` node and a string is a ``string`` node, so call-like
text inside either can never present itself as a ``call_expression`` — the same
structural guarantee the Python extractor gets from ``ast``.

Only direct children of ``program`` are treated as top-level declarations, so
nested and local declarations are excluded by construction rather than by a
filter that could be forgotten.

Targets stay unresolved here. Turning a written name into the file that
defines it is graph work in INC-004.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from rey_lib.files.file_utils import read_bytes_file
from rey_lib.repository_map.records import (
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    EDGE_KIND_IMPORT,
    EDGE_KIND_PROPERTY_ACCESS,
    EDGE_KIND_RE_EXPORT,
    RECORD_TYPE_FILE,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_ENUM,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_GLOBAL_PUBLICATION,
    SYMBOL_KIND_INTERFACE,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_TYPE_ALIAS,
    SYMBOL_KIND_VARIABLE,
    ReferenceEdge,
    SymbolInventory,
    SymbolRecord,
)

__all__ = ["extract_js_references", "extract_js_symbols", "supported_js_languages"]

# Language name to the grammar it is parsed with. TSX is a distinct grammar
# rather than TypeScript with a flag, so it is registered separately.
_GRAMMARS: dict[str, Callable[[], Any]] = {
    "JavaScript": tree_sitter_javascript.language,
    "TypeScript": tree_sitter_typescript.language_typescript,
    "TSX": tree_sitter_typescript.language_tsx,
}

# Parsers are built once per language: grammar construction is the expensive
# part, and a Parser holds no per-file state between parses.
_PARSERS: dict[str, Parser] = {}

# Chains rooted at these names stay inside the owning object and never cross a
# file boundary, so they are not recorded. The JavaScript counterpart of the
# Python extractor's self/cls rule.
_SELF_ROOTS = frozenset({"this", "super"})

# The global object whose publications and consumers are tracked.
_GLOBAL_ROOT = "window"

# Node types that declare a top-level name, mapped to their symbol kind.
# Adding a declaration form is an entry here, not a new branch.
_DECLARATION_KINDS: dict[str, str] = {
    "function_declaration": SYMBOL_KIND_FUNCTION,
    "generator_function_declaration": SYMBOL_KIND_FUNCTION,
    "function_signature": SYMBOL_KIND_FUNCTION,
    "class_declaration": SYMBOL_KIND_CLASS,
    # An abstract class is still a class.
    "abstract_class_declaration": SYMBOL_KIND_CLASS,
    # TypeScript type-shaped declarations each carry their own kind rather
    # than being squeezed into class or variable.
    "interface_declaration": SYMBOL_KIND_INTERFACE,
    "enum_declaration": SYMBOL_KIND_ENUM,
    "type_alias_declaration": SYMBOL_KIND_TYPE_ALIAS,
}

# Node types holding one or more variable_declarator children.
_VARIABLE_STATEMENTS = frozenset({"lexical_declaration", "variable_declaration"})


def supported_js_languages() -> list[str]:
    """Return the language names this extractor handles, sorted."""
    return sorted(_GRAMMARS)


def extract_js_symbols(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> SymbolInventory:
    """Extract top-level declarations from a JS, TS or TSX file.

    Each declared name yields exactly one symbol record. A name published by an
    export is flagged ``exported`` rather than duplicated as a second record,
    matching the Python extractor.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record. Defaults to ``path`` in POSIX form.

    Returns:
        The file's top-level symbol inventory.

    Raises:
        ValueError: If the language has no grammar registered.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()
    exported_names = _exported_names(root)

    symbols: list[SymbolRecord] = []
    for statement, inline_exported in _top_level_statements(root):
        for name, node, kind in _declarations(statement):
            symbols.append(
                _symbol(
                    recorded_path,
                    node,
                    name,
                    kind,
                    exported=inline_exported or name in exported_names,
                )
            )

    for name, node in _re_export_names(root):
        symbols.append(
            _symbol(recorded_path, node, name, SYMBOL_KIND_RE_EXPORT, exported=True)
        )

    # A window publication is a global publication, not an ES export, so
    # 'exported' stays false unless the same name is also exported.
    for name, node in _global_publications(root):
        symbols.append(
            _symbol(
                recorded_path,
                node,
                name,
                SYMBOL_KIND_GLOBAL_PUBLICATION,
                exported=name in exported_names,
            )
        )

    symbols.sort(key=lambda symbol: (symbol.source_line, symbol.source_column, symbol.name))
    return SymbolInventory(path=recorded_path, language=language, symbols=tuple(symbols))


def extract_js_references(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ReferenceEdge]:
    """Extract executable references from a JS, TS or TSX file.

    A member call is one call edge, not a call plus a property access. A
    reference rooted at the global object is one global_reference, so window
    usage stays identifiable as a single class of fact rather than being split
    across kinds.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record on each edge. Defaults to POSIX ``path``.

    Returns:
        Edges sorted by line, column, kind and target.

    Raises:
        ValueError: If the language has no grammar registered.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()
    from_id = f"{RECORD_TYPE_FILE}:{recorded_path}"

    nodes = list(_walk(root))
    # Node identity is node.id, never id(node): py-tree-sitter hands back a
    # fresh wrapper object on every access, so Python identity is meaningless
    # here and silently matches nothing.
    #
    # The callee expression of a call is reported once, as the call.
    callee_ids = {
        callee.id
        for node in nodes
        if node.type in {"call_expression", "new_expression"}
        and (callee := node.child_by_field_name("function")) is not None
    }
    # The left-hand side of a window publication is a declaration, not a use.
    publication_ids = {node.id for _, node in _global_publications(root)}

    edges: list[ReferenceEdge] = []
    for node in nodes:
        if node.type in {"import_statement", "export_statement"}:
            edges.extend(_module_edges(recorded_path, from_id, node))
        elif node.type in {"call_expression", "new_expression"}:
            callee = node.child_by_field_name("function")
            target = _dotted_name(callee) if callee is not None else None
            if target is None or _is_internal(target):
                continue
            kind = EDGE_KIND_GLOBAL_REFERENCE if _is_global(target) else EDGE_KIND_CALL
            edges.append(_edge(recorded_path, node, from_id, target, kind, node.type))
        elif node.type == "member_expression" and node.id not in callee_ids:
            if node.id in publication_ids:
                continue
            target = _dotted_name(node)
            if target is None or _is_internal(target):
                continue
            # Only the outermost chain is reported; a.b.c is one fact, not
            # also its a.b prefix.
            if node.parent is not None and node.parent.type == "member_expression":
                continue
            kind = EDGE_KIND_GLOBAL_REFERENCE if _is_global(target) else EDGE_KIND_PROPERTY_ACCESS
            edges.append(_edge(recorded_path, node, from_id, target, kind, node.type))

    edges.sort(
        key=lambda edge: (edge.source_line, edge.source_column, edge.edge_kind, edge.to)
    )
    return edges


def _parse(path: Path, language: str) -> Node:
    """Parse a source file and return its root node.

    Args:
        path: File to read and parse.
        language: Language name selecting the grammar.

    Returns:
        The parse tree's root node.

    Raises:
        ValueError: If the language has no grammar registered.
    """
    parser = _PARSERS.get(language)
    if parser is None:
        grammar = _GRAMMARS.get(language)
        if grammar is None:
            raise ValueError(
                f"No Tree-sitter grammar registered for language '{language}'. "
                f"Registered languages: {', '.join(supported_js_languages())}."
            )
        parser = Parser(Language(grammar()))
        _PARSERS[language] = parser
    return parser.parse(read_bytes_file(path)).root_node


def _walk(root: Node) -> list[Node]:
    """Return every named node in the tree, in source order.

    Args:
        root: Root node to walk.

    Returns:
        The named nodes. Anonymous tokens are skipped: they carry punctuation,
        never a reference.
    """
    nodes: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.named_children))
    return nodes


def _text(node: Node) -> str:
    """Return a node's source text.

    Args:
        node: Node to read.

    Returns:
        The decoded source text, empty when the node has none.
    """
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def _symbol(
    recorded_path: str,
    node: Node,
    name: str,
    symbol_kind: str,
    *,
    exported: bool,
) -> SymbolRecord:
    """Build one symbol record located at a syntax node.

    Args:
        recorded_path: Path to record on the symbol.
        node: Node giving the declaration position.
        name: Declared name.
        symbol_kind: One of the ``SYMBOL_KIND_*`` constants.
        exported: Whether the module publishes the name.

    Returns:
        The symbol record.
    """
    line, column = node.start_point
    return SymbolRecord(
        source_path=recorded_path,
        source_line=line + 1,
        source_column=column,
        name=name,
        symbol_kind=symbol_kind,
        exported=exported,
    )


def _edge(
    recorded_path: str,
    node: Node,
    from_id: str,
    target: str,
    edge_kind: str,
    evidence: str,
) -> ReferenceEdge:
    """Build one reference edge located at a syntax node.

    Args:
        recorded_path: Path to record on the edge.
        node: Node proving the reference.
        from_id: record_id of the fact making the reference.
        target: Referenced name as written.
        edge_kind: One of the ``EDGE_KIND_*`` constants.
        evidence: The syntax node type proving the reference.

    Returns:
        The edge.
    """
    line, column = node.start_point
    return ReferenceEdge(
        source_path=recorded_path,
        source_line=line + 1,
        source_column=column,
        from_id=from_id,
        to=target,
        edge_kind=edge_kind,
        evidence=evidence,
    )


def _top_level_statements(root: Node) -> list[tuple[Node, bool]]:
    """Return each top-level statement and whether an export wraps it.

    Args:
        root: The ``program`` node.

    Returns:
        Pairs of statement node and inline-export flag. An export statement is
        unwrapped to the declaration it exports.
    """
    statements: list[tuple[Node, bool]] = []
    for child in root.named_children:
        if child.type != "export_statement":
            statements.append((child, False))
            continue
        declaration = child.child_by_field_name("declaration")
        if declaration is not None:
            statements.append((declaration, True))
    return statements


def _declarations(statement: Node) -> list[tuple[str, Node, str]]:
    """Return the names one top-level statement declares.

    Args:
        statement: A top-level statement node.

    Returns:
        Tuples of name, locating node and symbol kind. Empty when the
        statement declares nothing.
    """
    kind = _DECLARATION_KINDS.get(statement.type)
    if kind is not None:
        name_node = statement.child_by_field_name("name")
        return [(_text(name_node), name_node, kind)] if name_node is not None else []
    if statement.type in _VARIABLE_STATEMENTS:
        declarations = []
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            # A destructuring pattern binds several names; only a plain
            # identifier is recorded as one named declaration.
            if name_node is not None and name_node.type == "identifier":
                declarations.append((_text(name_node), name_node, SYMBOL_KIND_VARIABLE))
        return declarations
    return []


def _export_clauses(root: Node) -> list[tuple[Node, Node | None]]:
    """Return every export clause with the module it re-exports from.

    Args:
        root: The ``program`` node.

    Returns:
        Pairs of export_statement node and its source node, which is None for
        a local export.
    """
    clauses = []
    for child in root.named_children:
        if child.type == "export_statement":
            clauses.append((child, child.child_by_field_name("source")))
    return clauses


def _exported_names(root: Node) -> frozenset[str]:
    """Return names published by a local export clause.

    Args:
        root: The ``program`` node.

    Returns:
        The exported local names. Re-exports are excluded: they publish a name
        this module never declared.
    """
    names: set[str] = set()
    for statement, source in _export_clauses(root):
        if source is not None:
            continue
        for specifier in _export_specifiers(statement):
            alias = specifier.child_by_field_name("alias")
            name = specifier.child_by_field_name("name")
            if name is not None:
                names.add(_text(alias if alias is not None else name))
    return frozenset(names)


def _export_specifiers(statement: Node) -> list[Node]:
    """Return the export specifiers inside an export statement.

    Args:
        statement: An ``export_statement`` node.

    Returns:
        Its ``export_specifier`` nodes.
    """
    specifiers = []
    for child in statement.named_children:
        if child.type == "export_clause":
            specifiers.extend(
                node for node in child.named_children if node.type == "export_specifier"
            )
    return specifiers


def _re_export_names(root: Node) -> list[tuple[str, Node]]:
    """Return names this module republishes from another module.

    Args:
        root: The ``program`` node.

    Returns:
        Pairs of published name and locating node. A star re-export is
        recorded under the name '*'.
    """
    results: list[tuple[str, Node]] = []
    for statement, source in _export_clauses(root):
        if source is None:
            continue
        specifiers = _export_specifiers(statement)
        if not specifiers:
            # export * from "mod" republishes everything under one fact.
            results.append(("*", statement))
            continue
        for specifier in specifiers:
            alias = specifier.child_by_field_name("alias")
            name = specifier.child_by_field_name("name")
            if name is not None:
                results.append((_text(alias if alias is not None else name), specifier))
    return results


def _module_edges(recorded_path: str, from_id: str, statement: Node) -> list[ReferenceEdge]:
    """Return import and re-export edges for one module statement.

    Args:
        recorded_path: Path to record on each edge.
        from_id: record_id of the fact making the reference.
        statement: An ``import_statement`` or ``export_statement`` node.

    Returns:
        The edges the statement proves. An export with no source imports
        nothing and yields none.
    """
    source = statement.child_by_field_name("source")
    if source is None:
        return []
    specifier = _text(source).strip("\"'`")
    is_import = statement.type == "import_statement"
    kind = EDGE_KIND_IMPORT if is_import else EDGE_KIND_RE_EXPORT

    members = _imported_members(statement) if is_import else _reexported_members(statement)
    if not members:
        # A side-effect import, namespace import or star re-export references
        # the module itself.
        return [_edge(recorded_path, statement, from_id, specifier, kind, statement.type)]
    return [
        _edge(recorded_path, statement, from_id, f"{specifier}.{member}", kind, statement.type)
        for member in members
    ]


def _imported_members(statement: Node) -> list[str]:
    """Return the member names a named import binds.

    Args:
        statement: An ``import_statement`` node.

    Returns:
        The imported member names, empty for default, namespace and
        side-effect imports.
    """
    members: list[str] = []
    for clause in statement.named_children:
        if clause.type != "import_clause":
            continue
        for child in clause.named_children:
            if child.type != "named_imports":
                continue
            for specifier in child.named_children:
                if specifier.type != "import_specifier":
                    continue
                name = specifier.child_by_field_name("name")
                if name is not None:
                    members.append(_text(name))
    return members


def _reexported_members(statement: Node) -> list[str]:
    """Return the member names an export-from statement republishes.

    Args:
        statement: An ``export_statement`` node with a source.

    Returns:
        The republished member names, empty for a star re-export.
    """
    members = []
    for specifier in _export_specifiers(statement):
        name = specifier.child_by_field_name("name")
        if name is not None:
            members.append(_text(name))
    return members


def _global_publications(root: Node) -> list[tuple[str, Node]]:
    """Return every assignment that publishes onto the global object.

    Args:
        root: The ``program`` node.

    Returns:
        Pairs of published name, such as window.ReyX, and the assigned
        member expression node.
    """
    results: list[tuple[str, Node]] = []
    for node in _walk(root):
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "member_expression":
            continue
        target = _dotted_name(left)
        if target is not None and _is_global(target):
            results.append((target, left))
    return results


def _dotted_name(node: Node) -> str | None:
    """Return the dotted name for a pure identifier/member chain.

    Args:
        node: Expression node to describe.

    Returns:
        The dotted name, or None when the expression is not a plain chain,
        such as a computed access or a call result.
    """
    if node.type in {"identifier", "shorthand_property_identifier", "property_identifier"}:
        return _text(node)
    if node.type == "this":
        return "this"
    if node.type == "super":
        return "super"
    if node.type != "member_expression":
        return None
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    if obj is None or prop is None or prop.type != "property_identifier":
        return None
    base = _dotted_name(obj)
    return None if base is None else f"{base}.{_text(prop)}"


def _is_internal(target: str) -> bool:
    """Return True when a reference target is rooted at this or super.

    Args:
        target: Dotted reference target.

    Returns:
        True when the reference stays inside the owning object.
    """
    return target.split(".", 1)[0] in _SELF_ROOTS


def _is_global(target: str) -> bool:
    """Return True when a reference target is rooted at the global object.

    Args:
        target: Dotted reference target.

    Returns:
        True when the target names window or one of its members.
    """
    return target.split(".", 1)[0] == _GLOBAL_ROOT
