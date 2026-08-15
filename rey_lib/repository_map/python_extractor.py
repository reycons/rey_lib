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
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_VARIABLE,
    ReferenceEdge,
    SymbolInventory,
    SymbolRecord,
)

__all__ = ["extract_python_references", "extract_python_symbols"]

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
    """

    module: str
    name: str | None
    alias: str | None
    line: int
    column: int

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
    """Extract top-level declarations from a Python file.

    Only ``module.body`` is inspected, so a function, class or variable nested
    inside another body can never reach the inventory (REQ-022).

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
        for name, line, column, kind in _declarations(node):
            declared_names.add(name)
            symbols.append(
                SymbolRecord(
                    source_path=recorded_path,
                    source_line=line,
                    source_column=column,
                    name=name,
                    symbol_kind=kind,
                    exported=name in exported_names,
                )
            )

    dunder_all_line, dunder_all_column = _dunder_all_location(tree)
    for name in sorted(exported_names - declared_names):
        imported = imported_names.get(name)
        symbols.append(
            SymbolRecord(
                source_path=recorded_path,
                source_line=imported.line if imported else dunder_all_line,
                source_column=imported.column if imported else dunder_all_column,
                name=name,
                symbol_kind=SYMBOL_KIND_RE_EXPORT if imported else SYMBOL_KIND_EXPORT,
                exported=True,
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


def _declarations(node: ast.stmt) -> list[tuple[str, int, int, str]]:
    """Return the top-level declarations one module-body statement makes.

    Args:
        node: A statement from ``module.body``.

    Returns:
        Tuples of name, line, column and symbol kind. Empty for statements that
        declare nothing. Assignment targets carry their own position, so two
        names bound on one line stay distinguishable.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [(node.name, node.lineno, node.col_offset, SYMBOL_KIND_FUNCTION)]
    if isinstance(node, ast.ClassDef):
        return [(node.name, node.lineno, node.col_offset, SYMBOL_KIND_CLASS)]
    if isinstance(node, ast.Assign):
        return [
            (target.id, target.lineno, target.col_offset, SYMBOL_KIND_VARIABLE)
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
            )
        ]
    return []


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


def _dunder_all_location(tree: ast.Module) -> tuple[int, int]:
    """Return the position of the ``__all__`` assignment.

    Args:
        tree: Parsed module.

    Returns:
        Line and column, defaulting to the start of file when ``__all__`` is
        absent. Only names published without a declaration site use this.
    """
    node = _dunder_all_node(tree)
    if node is None:
        return 1, 0
    return node.lineno, node.col_offset


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
