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
    SYMBOL_KIND_METHOD,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_TYPE_ALIAS,
    SYMBOL_KIND_VARIABLE,
    PARAMETER_KIND_POSITIONAL,
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
    DECLARATION_FORM_FIELD_DEFINITION,
    VALUE_KIND_ATTRIBUTE,
    VALUE_KIND_AWAIT,
    VALUE_KIND_CALL,
    VALUE_KIND_COLLECTION_LITERAL,
    VALUE_KIND_CONDITIONAL,
    VALUE_KIND_LITERAL,
    VALUE_KIND_NAME_CHAIN,
    VALUE_KIND_NONE_LITERAL,
    VALUE_KIND_OPERATION,
    VALUE_KIND_OTHER,
    VALUE_KIND_SUBSCRIPT,
    VALUE_KIND_F_STRING,
    AccessRecord,
    AssignmentRecord,
    ClassAttributeRecord,
    ParameterRecord,
    ReturnSiteRecord,
    ReferenceEdge,
    SymbolInventory,
    SymbolRecord,
)

__all__ = [
    "extract_js_class_attributes",
    "extract_js_return_sites",
    "extract_js_parameters",
    "extract_js_writes_and_accesses",
    "extract_js_references",
    "extract_js_symbols",
    "JS_GLOBAL_ROOT",
    "js_dotted_name",
    "js_is_global_rooted",
    "js_text",
    "parse_js_root",
    "supported_js_languages",
    "walk_js_nodes",
]

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

# The global object whose publications and consumers are tracked. Public
# because every JavaScript fact producer must agree on what "global" means; a
# second definition elsewhere is how two producers come to disagree.
JS_GLOBAL_ROOT = "window"

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

# The declaration forms that own methods.
_CLASS_DECLARATIONS = frozenset({"class_declaration", "abstract_class_declaration"})

# Class-body members that declare a method. An abstract signature declares one
# as surely as a body does; what a subclass must implement is addressable.
_METHOD_MEMBERS = frozenset({"method_definition", "abstract_method_signature"})

# Node types holding one or more variable_declarator children.
_VARIABLE_STATEMENTS = frozenset({"lexical_declaration", "variable_declaration"})

# Class-body members that bind a named attribute. An index_signature is not
# one: [key: string]: unknown declares no name, so there is nothing to record
# it under. Interface and type members are absent on purpose -- an interface
# property is not a class attribute, and this surface is class-scoped.
_FIELD_MEMBERS = frozenset({"public_field_definition"})

# Modifier keywords a field definition may carry, as the grammar spells them.
# All are unnamed children except accessibility_modifier, which is named and
# holds public/private/protected. Recorded as written: `declare` and `abstract`
# say a field allocates nothing, and that is the reader's inference to draw,
# not a column's claim.
_FIELD_MODIFIERS = frozenset({
    "static", "readonly", "abstract", "declare", "accessor",
    "accessibility_modifier",
})


def supported_js_languages() -> list[str]:
    """Return the language names this extractor handles, sorted."""
    return sorted(_GRAMMARS)


def extract_js_symbols(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> SymbolInventory:
    """Extract a JS, TS or TSX file's declarations.

    Top-level declarations, and one level further: the methods a top-level
    class declares. Nothing inside a function body is recorded, and neither is
    a class nested inside a class -- the same boundary the Python extractor
    keeps.

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
        for name, node, declaration, kind in _declarations(statement):
            symbols.append(
                _symbol(
                    recorded_path,
                    node,
                    declaration,
                    name,
                    kind,
                    exported=inline_exported or name in exported_names,
                )
            )

    for statement, inline_exported in _top_level_statements(root):
        owner_node = statement.child_by_field_name("name")
        if statement.type not in _CLASS_DECLARATIONS or owner_node is None:
            continue
        owner = _text(owner_node)
        owner_exported = inline_exported or owner in exported_names
        for name, node, declaration, public in _methods(statement):
            symbols.append(
                _symbol(
                    recorded_path,
                    node,
                    declaration,
                    name,
                    SYMBOL_KIND_METHOD,
                    # Published only when the owner is and the member is part
                    # of its public surface: a private helper on an exported
                    # class is not itself exported.
                    exported=owner_exported and public,
                    owner=owner,
                )
            )

    for name, node, declaration in _re_export_names(root):
        symbols.append(
            _symbol(
                recorded_path,
                node,
                declaration,
                name,
                SYMBOL_KIND_RE_EXPORT,
                exported=True,
            )
        )

    # A window publication is a global publication, not an ES export, so
    # 'exported' stays false unless the same name is also exported.
    for name, node, declaration in _global_publications(root):
        symbols.append(
            _symbol(
                recorded_path,
                node,
                declaration,
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
    # The locating node is what must be excluded here -- the assignment beside
    # it is the declaration's extent, and excluding that would swallow the
    # references written on its right.
    publication_ids = {node.id for _, node, _declaration in _global_publications(root)}

    edges: list[ReferenceEdge] = []
    for node in nodes:
        if node.type in {"import_statement", "export_statement"}:
            edges.extend(_module_edges(recorded_path, from_id, node))
        elif node.type in {"call_expression", "new_expression"}:
            callee = node.child_by_field_name("function")
            target = _dotted_name(callee) if callee is not None else None
            if target is None or _is_internal(target):
                continue
            kind = EDGE_KIND_GLOBAL_REFERENCE if js_is_global_rooted(target) else EDGE_KIND_CALL
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
            kind = (
                EDGE_KIND_GLOBAL_REFERENCE
                if js_is_global_rooted(target)
                else EDGE_KIND_PROPERTY_ACCESS
            )
            edges.append(_edge(recorded_path, node, from_id, target, kind, node.type))

    edges.sort(
        key=lambda edge: (edge.source_line, edge.source_column, edge.edge_kind, edge.to)
    )
    return edges


def parse_js_root(path: Path, language: str) -> Node:
    """Parse a JS, TS or TSX file and return its root node.

    Shared with the registration, entry-point and globals scanners so every
    detector reads the same tree from the same grammars.

    Args:
        path: File to read and parse.
        language: One of the names in ``supported_js_languages()``.

    Returns:
        The parse tree's root node.

    Raises:
        ValueError: If the language has no grammar registered.
    """
    return _parse(path, language)


def walk_js_nodes(root: Node) -> list[Node]:
    """Return every named node in a tree, in source order.

    Args:
        root: Root node to walk.

    Returns:
        The named nodes.
    """
    return _walk(root)


def js_dotted_name(node: Node) -> str | None:
    """Return the dotted name for a pure identifier/member chain.

    Args:
        node: Expression node to describe.

    Returns:
        The dotted name, or None when the expression is not a plain chain.
    """
    return _dotted_name(node)


def js_text(node: Node) -> str:
    """Return a node's source text.

    Args:
        node: Node to read.

    Returns:
        The decoded source text.
    """
    return _text(node)


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


def extract_js_parameters(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ParameterRecord]:
    """Extract every parameter declared by a top-level function or method.

    The same boundary the symbol inventory keeps.

    Two independent facts, which this grammar makes easy to confuse. A default
    is written ``b = 2`` and tree-sitter still reports the node as a
    ``required_parameter``, so a default is detected by the presence of a value
    child, never by the node type. Optionality is written ``b?: T`` and appears
    as an ``optional_parameter``. Neither implies the other.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The parameters, in declaration order within each declaration.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()

    parameters: list[ParameterRecord] = []
    for statement, _exported in _top_level_statements(root):
        name_node = statement.child_by_field_name("name")
        if name_node is None:
            continue
        if statement.type in _CLASS_DECLARATIONS:
            body = statement.child_by_field_name("body")
            for member in (body.named_children if body is not None else ()):
                if member.type not in _METHOD_MEMBERS:
                    continue
                member_name = member.child_by_field_name("name")
                if member_name is None:
                    continue
                parameters.extend(_js_parameters_of(
                    recorded_path, member, member_name,
                    f"{_text(name_node)}.{_text(member_name)}",
                ))
            continue
        parameters.extend(_js_parameters_of(
            recorded_path, statement, name_node, _text(name_node)
        ))
    return parameters


def extract_js_class_attributes(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ClassAttributeRecord]:
    """Extract every named attribute bound in a top-level class body.

    Class-scoped on purpose. An interface's ``property_signature`` is adjacent
    syntax the grammar exposes cheaply, and an interface property is not a
    class attribute; it gets its own fact when it has its own reason to exist.
    An ``index_signature`` declares no name and is not recordable at all.

    Modifiers are kept as written rather than normalized into booleans. A
    ``declare`` or ``abstract`` field allocates nothing, but the parser proves
    only the keyword -- so the keyword is what is stored, and what it implies
    is the reader's to decide.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The attributes, in declaration order within each class body.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()

    attributes: list[ClassAttributeRecord] = []
    for statement, _exported in _top_level_statements(root):
        if statement.type not in _CLASS_DECLARATIONS:
            continue
        name_node = statement.child_by_field_name("name")
        body = statement.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        owner_line, owner_column = _js_owner_position(statement)
        for member in body.named_children:
            if member.type not in _FIELD_MEMBERS:
                continue
            attribute = _js_class_attribute(
                recorded_path, member, _text(name_node),
                owner_line, owner_column, len(attributes),
            )
            if attribute is not None:
                attributes.append(attribute)
    return attributes


def _js_class_attribute(
    recorded_path: str,
    member: Node,
    owner: str,
    owner_line: int,
    owner_column: int,
    ordinal: int,
) -> ClassAttributeRecord | None:
    """Return one field definition as a record, or None when it has no name.

    Args:
        recorded_path: Path to record.
        member: The ``public_field_definition`` node.
        owner: The owning class's name.
        owner_line: The owning class's recorded line, and
        owner_column: its recorded column.
        ordinal: Position among the recorded attributes of this body.

    Returns:
        The record, or None.
    """
    name_node = member.child_by_field_name("name")
    if name_node is None:
        return None

    # A private name arrives as private_property_identifier with the '#' inside
    # the token, so storing the name verbatim is all ECMAScript-private needs.
    # TypeScript's `private` is a different thing and travels as a modifier.
    modifiers = tuple(
        _text(child) for child in member.children
        if child.type in _FIELD_MODIFIERS
    )
    annotation_node = next(
        (child for child in member.children if child.type == "type_annotation"),
        None,
    )
    annotation = (
        _text(annotation_node).lstrip(":").strip()
        if annotation_node is not None else None
    )
    return ClassAttributeRecord(
        source_path=recorded_path,
        owner_qualified_name=owner,
        owner_line=owner_line,
        owner_column=owner_column,
        name=_text(name_node),
        ordinal=ordinal,
        declaration_form=DECLARATION_FORM_FIELD_DEFINITION,
        is_annotated=annotation_node is not None,
        # A default is written `b = 2` and the node stays a field definition,
        # so presence of a value child is the fact -- never the node type.
        has_default=member.child_by_field_name("value") is not None,
        is_optional=any(child.type == "?" for child in member.children),
        annotation=annotation,
        modifiers=modifiers,
    )


def _js_parameters_of(
    recorded_path: str,
    declaration: Node,
    locating: Node,
    qualified_name: str,
) -> list[ParameterRecord]:
    """Return one declaration's parameters, in written order.

    Args:
        recorded_path: Path to record.
        declaration: The function, method or signature node.
        locating: The node the owning symbol is located at, so the owner
            reference matches the symbol record exactly.
        qualified_name: How the owning declaration is addressed.

    Returns:
        The parameter records.
    """
    formal = declaration.child_by_field_name("parameters")
    if formal is None:
        return []
    line, column = locating.start_point

    records: list[ParameterRecord] = []
    for ordinal, node in enumerate(
        child for child in formal.named_children if child.type != "comment"
    ):
        name_node = node.child_by_field_name("pattern") or _js_first_identifier(node)
        if name_node is None:
            continue
        rest = node.type == "rest_pattern" or name_node.type == "rest_pattern"
        annotation = node.child_by_field_name("type")
        records.append(ParameterRecord(
            source_path=recorded_path,
            owner_qualified_name=qualified_name,
            owner_line=line + 1,
            owner_column=column,
            name=_text(_js_first_identifier(name_node) or name_node),
            ordinal=ordinal,
            parameter_kind=(PARAMETER_KIND_VAR_POSITIONAL if rest
                            else PARAMETER_KIND_POSITIONAL),
            # A value child, not the node type: 'b = 2' is a required_parameter
            # that carries one.
            has_default=node.child_by_field_name("value") is not None,
            # The syntactic '?', written or not. Not an inference about what a
            # caller may omit.
            is_optional=node.type == "optional_parameter",
            annotation=(_text(annotation).lstrip(": ").strip()
                        if annotation is not None else None),
        ))
    return records


def _js_return_annotation(declaration: Node) -> str | None:
    """Return a declaration's written return type, or None.

    Args:
        declaration: The node the symbol spans.

    Returns:
        The annotation without its leading colon, or None where none is
        written.
    """
    annotation = declaration.child_by_field_name("return_type")
    if annotation is None:
        return None
    return _text(annotation).lstrip(": ").strip() or None


def _js_first_identifier(node: Node) -> Node | None:
    """Return the first identifier at or beneath a node, or None."""
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        return node
    for child in node.named_children:
        found = _js_first_identifier(child)
        if found is not None:
            return found
    return None


def extract_js_writes_and_accesses(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> tuple[list[AssignmentRecord], list[AccessRecord]]:
    """Extract the writes and indexed access forms inside recorded declarations.

    The same boundary and the same vocabulary the Python extractor uses. A
    ``variable_declarator`` with a value is a write; a declaration without one
    binds nothing and is not.

    A subscript in a write position is reached through its assignment and is
    not also recorded as an access -- the split between the two records is by
    direction, and recording it twice would be two facts for one syntax.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The writes and the accesses, in source order.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()

    writes: list[AssignmentRecord] = []
    accesses: list[AccessRecord] = []
    for owner, qualified_name in _js_recorded_bodies(root):
        own = _own_nodes(owner)
        written_subscripts: set[int] = set()
        for node in own:
            record = _js_write_of(recorded_path, node, owner, qualified_name)
            if record is not None:
                writes.append(record)
                target = (node.child_by_field_name("left")
                          or node.child_by_field_name("name"))
                if target is not None and target.type == "subscript_expression":
                    written_subscripts.add(target.id)
        for node in own:
            if node.id in written_subscripts:
                continue
            access = _js_access_of(recorded_path, node, owner, qualified_name)
            if access is not None:
                accesses.append(access)
    return writes, accesses


#: Declarations that own their own insides. A node inside one of these belongs
#: to it, not to whatever encloses it, so a traversal looking for one
#: declaration's facts stops here. The Python extractor keeps the same rule.
_NESTED_DECLARATIONS = frozenset({
    "function_declaration", "function_expression", "arrow_function",
    "generator_function_declaration", "generator_function", "method_definition",
})


def _own_nodes(owner: Node) -> list[Node]:
    """Return the nodes belonging to one declaration, stopping at nested ones.

    ``_walk`` descends into nested functions and arrows, which makes a
    closure's statements look like the enclosing declaration's::

        function outer() {
            const inner = () => { d["x"] = 1; };   // _walk(outer) yields this
        }

    Owner means *the indexed declaration containing this occurrence*, and that
    is false positional ownership. A closure is not an addressable symbol, so
    its facts are left out rather than filed under a declaration that does not
    contain them.

    The nested declaration node itself is returned; only its insides are
    pruned. Source order, like ``_walk``, so record order is unchanged for
    everything already correctly owned.

    Args:
        owner: The declaration whose own nodes are wanted.

    Returns:
        The owner and every named descendant not inside a nested declaration.
    """
    nodes: list[Node] = []
    stack = [owner]
    while stack:
        node = stack.pop()
        nodes.append(node)
        if node is not owner and node.type in _NESTED_DECLARATIONS:
            continue
        stack.extend(reversed(node.named_children))
    return nodes


def extract_js_return_sites(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ReturnSiteRecord]:
    """Extract every return written inside a recorded declaration.

    The ownership boundary again: a return inside a nested function or arrow
    belongs to that one. A concise-body arrow (``() => x``) is not reached,
    because an arrow bound to a const is not a declaration this inventory
    records -- the same gap that leaves it without parameters.

    Args:
        path: Source file to read and parse.
        language: One of the names in ``supported_js_languages()``.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The return sites, in source order within each declaration.
    """
    root = _parse(path, language)
    recorded_path = source_path if source_path is not None else path.as_posix()

    sites: list[ReturnSiteRecord] = []
    for owner, qualified_name in _js_recorded_bodies(root):
        owner_line, owner_column = _js_owner_position(owner)
        own = [node for node in _own_nodes(owner)
               if node.type == "return_statement"]
        own.sort(key=lambda node: node.start_point)
        for ordinal, node in enumerate(own):
            value = node.named_children[0] if node.named_children else None
            kind, chain = _js_returned_facts(value)
            line, column = node.start_point
            sites.append(
                ReturnSiteRecord(
                    source_path=recorded_path,
                    owner_qualified_name=qualified_name,
                    owner_line=owner_line,
                    owner_column=owner_column,
                    source_line=line + 1,
                    source_column=column,
                    ordinal=ordinal,
                    has_value=value is not None,
                    value_kind=kind,
                    value_chain=chain,
                )
            )
    return sites


#: Returned expression node types whose shape the vocabulary names directly.
_RETURNED_KINDS = {
    "null": VALUE_KIND_NONE_LITERAL,
    "string": VALUE_KIND_LITERAL,
    "number": VALUE_KIND_LITERAL,
    "true": VALUE_KIND_LITERAL,
    "false": VALUE_KIND_LITERAL,
    "regex": VALUE_KIND_LITERAL,
    "template_string": VALUE_KIND_F_STRING,
    "call_expression": VALUE_KIND_CALL,
    "await_expression": VALUE_KIND_AWAIT,
    "subscript_expression": VALUE_KIND_SUBSCRIPT,
    "array": VALUE_KIND_COLLECTION_LITERAL,
    "object": VALUE_KIND_COLLECTION_LITERAL,
    "ternary_expression": VALUE_KIND_CONDITIONAL,
    "binary_expression": VALUE_KIND_OPERATION,
    "unary_expression": VALUE_KIND_OPERATION,
}


def _js_returned_facts(value: Node | None) -> tuple[str | None, str | None]:
    """Return the shape of a returned expression, and its chain where proved.

    ``undefined`` has its own node type here -- it is neither an identifier nor
    ``null`` -- so it falls to ``other`` rather than being folded into
    ``none_literal``, which would erase a distinction TypeScript makes.

    Args:
        value: The returned expression, or None for a bare ``return``.

    Returns:
        The value kind and the proven dotted chain, each None where absent.
    """
    if value is None:
        return None, None
    if value.type == "identifier":
        return VALUE_KIND_NAME_CHAIN, _text(value)
    if value.type == "member_expression":
        chain = js_dotted_name(value)
        if chain is None:
            return VALUE_KIND_ATTRIBUTE, None
        return VALUE_KIND_NAME_CHAIN, chain
    return _RETURNED_KINDS.get(value.type, VALUE_KIND_OTHER), None


def _js_recorded_bodies(root: Node) -> list[tuple[Node, str]]:
    """Return the declarations whose insides are recorded, with their names."""
    bodies: list[tuple[Node, str]] = []
    for statement, _exported in _top_level_statements(root):
        name_node = statement.child_by_field_name("name")
        if name_node is None:
            continue
        if statement.type in _CLASS_DECLARATIONS:
            body = statement.child_by_field_name("body")
            for member in (body.named_children if body is not None else ()):
                member_name = member.child_by_field_name("name")
                if member.type in _METHOD_MEMBERS and member_name is not None:
                    bodies.append(
                        (member, f"{_text(name_node)}.{_text(member_name)}")
                    )
            continue
        bodies.append((statement, _text(name_node)))
    return bodies


def _js_target_facts(target: Node) -> tuple[str, str | None, str | None, str | None] | None:
    """Return what a write target proves, or None when it is not a target shape.

    ``object_chain`` is the proven dotted chain and is None the moment the
    expression stops being one. No source text is stored.
    """
    if target.type == "identifier":
        return TARGET_KIND_NAME, _text(target), None, None
    if target.type == "member_expression":
        property_node = target.child_by_field_name("property")
        return (
            TARGET_KIND_ATTRIBUTE,
            js_dotted_name(target),
            _text(property_node) if property_node is not None else None,
            None,
        )
    if target.type == "subscript_expression":
        index = target.child_by_field_name("index")
        base = target.child_by_field_name("object")
        key = (_js_string_value(index)
               if index is not None and index.type == "string" else None)
        return (
            TARGET_KIND_SUBSCRIPT,
            js_dotted_name(base) if base is not None else None,
            None,
            key,
        )
    return None


def _js_string_value(node: Node) -> str:
    """Return a string literal's value, without its quotes."""
    fragment = next(
        (child for child in node.named_children
         if child.type == "string_fragment"), None
    )
    return _text(fragment) if fragment is not None else ""


def _js_write_of(
    recorded_path: str, node: Node, owner: Node, qualified_name: str
) -> AssignmentRecord | None:
    """Return the write one node performs, or None."""
    if node.type in {"assignment_expression", "augmented_assignment_expression"}:
        target = node.child_by_field_name("left")
        augmented = node.type == "augmented_assignment_expression"
    elif node.type == "variable_declarator":
        # A declaration with no value binds nothing.
        if node.child_by_field_name("value") is None:
            return None
        target, augmented = node.child_by_field_name("name"), False
    else:
        return None
    if target is None:
        return None

    facts = _js_target_facts(target)
    if facts is None:
        return None
    kind, chain, attribute, key = facts
    line, column = target.start_point
    owner_line, owner_column = _js_owner_position(owner)
    return AssignmentRecord(
        source_path=recorded_path,
        owner_qualified_name=qualified_name,
        owner_line=owner_line,
        owner_column=owner_column,
        source_line=line + 1,
        source_column=column,
        target_kind=kind,
        target_chain=chain,
        attribute_name=attribute,
        key=key,
        is_augmented=augmented,
    )


def _js_access_of(
    recorded_path: str, node: Node, owner: Node, qualified_name: str
) -> AccessRecord | None:
    """Return the access form one node is, or None."""
    owner_line, owner_column = _js_owner_position(owner)
    line, column = node.start_point
    common = {
        "source_path": recorded_path,
        "owner_qualified_name": qualified_name,
        "owner_line": owner_line,
        "owner_column": owner_column,
        "source_line": line + 1,
        "source_column": column,
    }
    if node.type == "subscript_expression":
        index = node.child_by_field_name("index")
        base = node.child_by_field_name("object")
        present, kind, literal = _js_argument_facts(index)
        return AccessRecord(
            **common,
            access_kind=ACCESS_KIND_SUBSCRIPT,
            object_chain=js_dotted_name(base) if base is not None else None,
            argument_present=True,
            argument_kind=kind,
            literal_argument=literal,
        )
    if node.type == "call_expression":
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return None
        property_node = callee.child_by_field_name("property")
        if property_node is None or _text(property_node) not in INDEXED_ACCESS_METHODS:
            return None
        arguments = node.child_by_field_name("arguments")
        first = next(
            (child for child in (arguments.named_children if arguments else ())
             if child.type != "comment"), None
        )
        present, kind, literal = _js_argument_facts(first)
        receiver = callee.child_by_field_name("object")
        return AccessRecord(
            **common,
            access_kind=ACCESS_KIND_METHOD_CALL,
            object_chain=js_dotted_name(receiver) if receiver is not None else None,
            method_name=_text(property_node),
            argument_present=present,
            argument_kind=kind,
            literal_argument=literal,
        )
    return None


def _js_argument_facts(argument: Node | None) -> tuple[bool, str | None, str | None]:
    """Return what a selecting argument proves, in the shared vocabulary."""
    if argument is None:
        return False, None, None
    if argument.type == "string":
        return True, ARGUMENT_KIND_STRING_LITERAL, _js_string_value(argument)
    if argument.type in {"number", "true", "false", "null", "undefined"}:
        return True, ARGUMENT_KIND_OTHER_LITERAL, None
    return True, ARGUMENT_KIND_EXPRESSION, None


def _js_owner_position(owner: Node) -> tuple[int, int]:
    """Return the owning declaration's recorded start position.

    The symbol inventory locates a declaration at its *name*, so the owner
    reference has to use the same point or it would not join.
    """
    name_node = owner.child_by_field_name("name")
    line, column = (name_node or owner).start_point
    return line + 1, column


def _symbol(
    recorded_path: str,
    node: Node,
    declaration: Node,
    name: str,
    symbol_kind: str,
    *,
    exported: bool,
    owner: str = "",
) -> SymbolRecord:
    """Build one symbol record located at a syntax node, spanning its declaration.

    Two nodes, because they answer two different questions. ``node`` says where
    the name is written and is what ``record_id`` is built from, so it must
    never move: every review decision keys on that id. ``declaration`` says how
    far the thing itself extends, which is what a reader retrieves.

    They are frequently different. The locating node of a class is its
    identifier, and that identifier ends where the name ends, not where the
    class body does. Passing one node for both would record a span one word
    long and call it a declaration.

    The span is proved from the parse tree and never estimated. This is the one
    construction point for a JavaScript or TypeScript symbol, so the end line is
    computed here and call sites only declare which node is the declaration.

    Args:
        recorded_path: Path to record on the symbol.
        node: Node giving the declaration position.
        declaration: The enclosing declaration, giving the extent.
        name: Declared name.
        symbol_kind: One of the ``SYMBOL_KIND_*`` constants.
        exported: Whether the name is publicly reachable.
        owner: The declaring class, empty at top level.

    Returns:
        The symbol record.
    """
    line, column = node.start_point
    end_line, end_column = declaration.end_point
    return SymbolRecord(
        source_path=recorded_path,
        source_line=line + 1,
        source_column=column,
        name=name,
        symbol_kind=symbol_kind,
        exported=exported,
        owner=owner,
        # The declared return, where the grammar carries one. A class has none
        # and answers None, which is absence rather than a claim.
        returns_annotation=_js_return_annotation(declaration),
        end_line=end_line + 1,
        # Exclusive, as tree-sitter reports it: 'const x=1;const y=2;' parses
        # as [0,10) and [10,20), so column 10 ends the first and starts the
        # second. Keeping it exclusive is what lets a position on that boundary
        # belong to exactly one of them.
        end_column=end_column,
        # A generator form, or a body that yields. Both are needed: `function*`
        # with no yield is still a generator, and a `*` method carries the star
        # rather than a distinct node type.
        is_generator=_js_is_generator(declaration),
    )


#: Declaration node types that are generators by their own grammar.
_GENERATOR_DECLARATIONS = frozenset({
    "generator_function_declaration", "generator_function",
})


def _js_is_generator(declaration: Node) -> bool:
    """Return whether a declaration is a generator.

    Args:
        declaration: The declaring node.

    Returns:
        True for a generator node type, for a ``*``-marked member, or for a
        declaration whose own body yields. The body check uses the ownership
        boundary, so a ``yield`` inside a nested declaration makes *that* one a
        generator and not this.
    """
    if declaration.type in _GENERATOR_DECLARATIONS:
        return True
    if any(child.type == "*" for child in declaration.children):
        return True
    return any(
        node.type == "yield_expression" for node in _own_nodes(declaration)
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


def _declarations(statement: Node) -> list[tuple[str, Node, Node, str]]:
    """Return the names one top-level statement declares.

    Args:
        statement: A top-level statement node.

    Returns:
        Tuples of name, locating node, declaring node and symbol kind. Empty
        when the statement declares nothing.

        The locating node is the identifier and the declaring node is what it
        names -- the statement for a function, class, interface, enum or type
        alias, and the individual declarator for a variable, so that one
        binding of a multi-name ``const`` spans its own initializer rather than
        the whole statement.
    """
    kind = _DECLARATION_KINDS.get(statement.type)
    if kind is not None:
        name_node = statement.child_by_field_name("name")
        return (
            [(_text(name_node), name_node, statement, kind)]
            if name_node is not None
            else []
        )
    if statement.type in _VARIABLE_STATEMENTS:
        declarations = []
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            # A destructuring pattern binds several names; only a plain
            # identifier is recorded as one named declaration.
            if name_node is not None and name_node.type == "identifier":
                declarations.append(
                    (_text(name_node), name_node, declarator, SYMBOL_KIND_VARIABLE)
                )
        return declarations
    return []


def _methods(statement: Node) -> list[tuple[str, Node, Node, bool]]:
    """Return the methods one class declares, with their visibility.

    Args:
        statement: A class or abstract class declaration.

    Returns:
        Tuples of method name, locating node, declaring node, and whether the
        member is public. The declaring node is the member itself, so a
        method's span is its own body rather than the class's.

        TypeScript members are public by default, so a member is non-public
        only when it says so -- an accessibility modifier of private or
        protected, or a #-prefixed name, which is private in JavaScript itself.
    """
    body = statement.child_by_field_name("body")
    if body is None:
        return []
    members: list[tuple[str, Node, Node, bool]] = []
    for member in body.named_children:
        if member.type not in _METHOD_MEMBERS:
            continue
        name_node = member.child_by_field_name("name")
        if name_node is None:
            continue
        modifiers = {
            _text(child)
            for child in member.children
            if child.type == "accessibility_modifier"
        }
        public = (
            not (modifiers & {"private", "protected"})
            and name_node.type != "private_property_identifier"
        )
        members.append((_text(name_node), name_node, member, public))
    return members


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


def _re_export_names(root: Node) -> list[tuple[str, Node, Node]]:
    """Return names this module republishes from another module.

    Args:
        root: The ``program`` node.

    Returns:
        Triples of published name, locating node and declaring node. A star
        re-export is recorded under the name '*'.

        The declaring node is the whole ``export_statement``, never the
        specifier. A specifier is a fragment of a declaration: in a re-export
        written across several lines, the specifier for one name spans that
        name alone, so a span taken from it would describe the identifier
        instead of the statement that republishes it. The specifier stays the
        locating node, because ``record_id`` is built from it.
    """
    results: list[tuple[str, Node, Node]] = []
    for statement, source in _export_clauses(root):
        if source is None:
            continue
        specifiers = _export_specifiers(statement)
        if not specifiers:
            # export * from "mod" republishes everything under one fact.
            results.append(("*", statement, statement))
            continue
        for specifier in specifiers:
            alias = specifier.child_by_field_name("alias")
            name = specifier.child_by_field_name("name")
            if name is not None:
                results.append(
                    (
                        _text(alias if alias is not None else name),
                        specifier,
                        statement,
                    )
                )
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


def _global_publications(root: Node) -> list[tuple[str, Node, Node]]:
    """Return every assignment that publishes onto the global object.

    Args:
        root: The ``program`` node.

    Returns:
        Triples of published name, such as window.ReyX, the assigned member
        expression node, and the whole assignment.

        The assignment is the declaring node because what is published is the
        expression on its right: a span taken from the member expression would
        cover ``window.ReyX`` and stop before the implementation it names.
    """
    results: list[tuple[str, Node, Node]] = []
    for node in _walk(root):
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "member_expression":
            continue
        target = _dotted_name(left)
        if target is not None and js_is_global_rooted(target):
            results.append((target, left, node))
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


def js_is_global_rooted(name: str) -> bool:
    """Return whether a dotted JavaScript name is rooted at the global object.

    The one definition of what "global" means for JavaScript facts. Both the
    reference extractor and the globals scanner consume it, so a publication
    and a consumer can never be judged by two different rules.

    Args:
        name: Dotted reference target or published name.

    Returns:
        True when the name is the global object or one of its members.
    """
    return name.split(".", 1)[0] == JS_GLOBAL_ROOT
