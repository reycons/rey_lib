"""Dispatcher and switch discovery for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-006, REQ-100 to REQ-103).

A dispatcher is a decision point that branches over a named behaviour
vocabulary: an action id, a node type, a viewer type, an object kind. Finding
them from parse trees rather than by scanning for the word 'switch' means a
chained ``if/else if`` over the same subject is recorded as what it is — the
same decision point wearing different syntax — and a switch inside a comment
or a string is not recorded at all.

Nothing here decides whether a dispatcher is architecturally acceptable. The
generator measures; review classifies. Every record leaves classification as
'unreviewed', and this module has no code path that sets anything else.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rey_lib.files.file_utils import read_text_file
from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.js_extractor import (
    js_text,
    parse_js_root,
    supported_js_languages,
    walk_js_nodes,
)
from rey_lib.repository_map.records import (
    DispatcherRecord,
    FileRecord,
    ReferenceEdge,
    ScanRules,
)

__all__ = ["inventory_dispatchers_and_switches"]

logger = get_logger(__name__)

# A decision point needs at least this many named branches to be a dispatcher.
# One comparison is a condition, not a vocabulary.
_MINIMUM_BRANCHES = 2

# Equality operators that compare a subject against a named value.
_JS_EQUALITY = frozenset({"===", "==", "!==", "!="})

# JS/TS string literal node types.
_JS_STRINGS = frozenset({"string", "template_string"})


def inventory_dispatchers_and_switches(
    repo_root: Path,
    files: list[FileRecord],
    rules: ScanRules,
    references: list[ReferenceEdge] | None = None,
) -> list[DispatcherRecord]:
    """Inventory every decision point that branches over a named vocabulary.

    Args:
        repo_root: Repository root the file paths are relative to.
        files: The inventoried files to scan.
        rules: The scanned repository's own scan rules.
        references: Executable reference edges, used to attribute callers.
            Omitted, callers are left empty rather than guessed.

    Returns:
        Dispatchers sorted by path, line and column.
    """
    js_languages = set(supported_js_languages())
    records: list[DispatcherRecord] = []

    for file_record in files:
        if not rules.extracts_facts_from(file_record):
            continue
        path = repo_root / file_record.path
        if file_record.language == "Python":
            records.extend(_python_dispatchers(path, file_record.path))
        elif file_record.language in js_languages:
            records.extend(_js_dispatchers(path, file_record))

    records = _with_callers(records, references or [])
    records.sort(key=lambda record: (record.source_path, record.source_line, record.source_column))
    return records


def _with_callers(
    records: list[DispatcherRecord],
    references: list[ReferenceEdge],
) -> list[DispatcherRecord]:
    """Attribute callers to each dispatcher from executable references.

    A caller is a file whose reference names the enclosing symbol. Resolution
    to a specific declaration is graph work, so this records which files reach
    the name rather than asserting which declaration they reached.

    Args:
        records: The dispatchers found.
        references: Executable reference edges.

    Returns:
        The dispatchers with callers attributed.
    """
    callers_by_symbol: dict[str, set[str]] = {}
    for reference in references:
        name = reference.to.rpartition(".")[2] or reference.to
        callers_by_symbol.setdefault(name, set()).add(reference.source_path)

    return [
        DispatcherRecord(
            source_path=record.source_path,
            source_line=record.source_line,
            source_column=record.source_column,
            symbol=record.symbol,
            vocabulary=record.vocabulary,
            branch_count=record.branch_count,
            branch_values=record.branch_values,
            callers=tuple(
                sorted(callers_by_symbol.get(record.symbol, set()) - {record.source_path})
            ),
        )
        for record in records
    ]


def _python_dispatchers(path: Path, source_path: str) -> list[DispatcherRecord]:
    """Return dispatchers in one Python file.

    Args:
        path: Absolute path to the file.
        source_path: Repository-relative path to record.

    Returns:
        The dispatchers found.

    Raises:
        ValueError: If the file is not parseable Python.
    """
    try:
        tree = ast.parse(read_text_file(path), filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"Could not parse Python file {path}: {exc}") from exc

    enclosing = _python_enclosing_symbols(tree)
    records: list[DispatcherRecord] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            values = _python_match_values(node)
            if len(values) >= _MINIMUM_BRANCHES:
                records.append(
                    _record(
                        source_path,
                        node.lineno,
                        node.col_offset,
                        enclosing.get(id(node), "<module>"),
                        ast.unparse(node.subject),
                        values,
                    )
                )
        elif isinstance(node, ast.If):
            subject, values = _python_if_chain(node)
            if subject is not None and len(values) >= _MINIMUM_BRANCHES:
                records.append(
                    _record(
                        source_path,
                        node.lineno,
                        node.col_offset,
                        enclosing.get(id(node), "<module>"),
                        subject,
                        values,
                    )
                )
    return records


def _python_enclosing_symbols(tree: ast.Module) -> dict[int, str]:
    """Map each node to the nearest enclosing declaration name.

    Args:
        tree: Parsed module.

    Returns:
        Node id to the enclosing function or class name.
    """
    enclosing: dict[int, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        """Record the enclosing name for every descendant."""
        for child in ast.iter_child_nodes(node):
            child_name = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else name
            )
            enclosing[id(child)] = child_name
            walk(child, child_name)

    walk(tree, "<module>")
    return enclosing


def _python_match_values(node: ast.Match) -> list[str]:
    """Return the literal values a match statement branches on.

    Args:
        node: A match statement.

    Returns:
        The literal case values, excluding the wildcard.
    """
    values = []
    for case in node.cases:
        pattern = case.pattern
        if isinstance(pattern, ast.MatchValue) and isinstance(pattern.value, ast.Constant):
            values.append(str(pattern.value.value))
    return values


def _python_if_chain(node: ast.If) -> tuple[str | None, list[str]]:
    """Return the subject and values of an if/elif chain over one subject.

    A chain only counts when every arm compares the same expression, which is
    what makes it a vocabulary rather than unrelated conditions.

    Args:
        node: The head of a possible chain.

    Returns:
        The subject expression and its compared values, or (None, []).
    """
    subjects: set[str] = set()
    values: list[str] = []
    current: ast.stmt | None = node

    while isinstance(current, ast.If):
        comparison = _python_equality(current.test)
        if comparison is None:
            return None, []
        subject, value = comparison
        subjects.add(subject)
        values.append(value)
        current = current.orelse[0] if len(current.orelse) == 1 else None

    if len(subjects) != 1:
        return None, []
    return subjects.pop(), values


def _python_equality(test: ast.expr) -> tuple[str, str] | None:
    """Return the subject and literal of an equality comparison.

    Args:
        test: The condition expression.

    Returns:
        Subject text and literal value, or None when the test is not a
        comparison of one expression against a constant.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], (ast.Eq, ast.In)):
        return None
    comparator = test.comparators[0]
    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
        return ast.unparse(test.left), comparator.value
    return None


def _js_dispatchers(path: Path, file_record: FileRecord) -> list[DispatcherRecord]:
    """Return dispatchers in one JS, TS or TSX file.

    Args:
        path: Absolute path to the file.
        file_record: The file's inventory record.

    Returns:
        The dispatchers found.
    """
    root = parse_js_root(path, file_record.language)
    records: list[DispatcherRecord] = []

    for node in walk_js_nodes(root):
        if node.type == "switch_statement":
            values = _js_switch_values(node)
            if len(values) < _MINIMUM_BRANCHES:
                continue
            subject = _unwrap(node.child_by_field_name("value"))
            records.append(
                _record(
                    file_record.path,
                    node.start_point[0] + 1,
                    node.start_point[1],
                    _js_enclosing_symbol(node),
                    js_text(subject) if subject is not None else "",
                    values,
                )
            )
        elif node.type == "if_statement" and not _is_chained_alternative(node):
            subject, values = _js_if_chain(node)
            if subject is None or len(values) < _MINIMUM_BRANCHES:
                continue
            records.append(
                _record(
                    file_record.path,
                    node.start_point[0] + 1,
                    node.start_point[1],
                    _js_enclosing_symbol(node),
                    subject,
                    values,
                )
            )
    return records


def _is_chained_alternative(node) -> bool:
    """Return True when this if continues an enclosing chain over one subject.

    Only the head of a chain is recorded, so one decision point does not appear
    once per arm. The test is the enclosing subject, not mere nesting: a chain
    over ``event.key`` that happens to sit in the else-arm of an unrelated
    check is its own decision point and must still be recorded.

    Args:
        node: An ``if_statement`` node.

    Returns:
        True when the node continues a chain over the same subject.
    """
    parent = node.parent
    if parent is not None and parent.type == "else_clause":
        parent = parent.parent
    if parent is None or parent.type != "if_statement":
        return False

    own = _js_equality(node.child_by_field_name("condition"))
    enclosing = _js_equality(parent.child_by_field_name("condition"))
    if own is None or enclosing is None:
        return False
    return own[0] == enclosing[0]


def _unwrap(node):
    """Return an expression with any parentheses removed.

    ``switch (x)`` and ``if (x === "a")`` both hand back a parenthesized
    expression, so the recorded vocabulary would otherwise read '(x)' and two
    spellings of one subject would look like two vocabularies.

    Args:
        node: An expression node, or None.

    Returns:
        The innermost expression, or None.
    """
    while node is not None and node.type == "parenthesized_expression":
        children = node.named_children
        node = children[0] if children else None
    return node


def _js_switch_values(node) -> list[str]:
    """Return the literal values a switch branches on.

    Args:
        node: A ``switch_statement`` node.

    Returns:
        The case literal values, excluding default.
    """
    values = []
    for child in walk_js_nodes(node):
        if child.type != "switch_case":
            continue
        label = child.child_by_field_name("value")
        if label is not None and label.type in _JS_STRINGS:
            values.append(js_text(label).strip("\"'`"))
    return values


def _js_if_chain(node) -> tuple[str | None, list[str]]:
    """Return the subject and values of an if/else-if chain over one subject.

    Args:
        node: The head of a possible chain.

    Returns:
        The subject expression and its compared values, or (None, []).
    """
    subjects: set[str] = set()
    values: list[str] = []
    current = node

    while current is not None and current.type == "if_statement":
        condition = current.child_by_field_name("condition")
        comparison = _js_equality(condition)
        if comparison is None:
            return None, []
        subject, value = comparison
        subjects.add(subject)
        values.append(value)
        current = current.child_by_field_name("alternative")
        # An else clause wraps the following statement.
        if current is not None and current.type == "else_clause":
            current = current.named_children[0] if current.named_children else None

    if len(subjects) != 1:
        return None, []
    return subjects.pop(), values


def _js_equality(condition) -> tuple[str, str] | None:
    """Return the subject and literal of an equality comparison.

    Args:
        condition: The condition node, possibly parenthesized.

    Returns:
        Subject text and literal value, or None.
    """
    condition = _unwrap(condition)
    if condition is None or condition.type != "binary_expression":
        return None
    operator = condition.child_by_field_name("operator")
    if operator is None or js_text(operator) not in _JS_EQUALITY:
        return None
    left = condition.child_by_field_name("left")
    right = condition.child_by_field_name("right")
    if left is None or right is None:
        return None
    if right.type in _JS_STRINGS:
        return js_text(left), js_text(right).strip("\"'`")
    if left.type in _JS_STRINGS:
        return js_text(right), js_text(left).strip("\"'`")
    return None


def _js_enclosing_symbol(node) -> str:
    """Return the nearest enclosing declaration name.

    Args:
        node: The dispatcher node.

    Returns:
        The enclosing function, method or class name, or '<module>'.
    """
    current = node.parent
    while current is not None:
        if current.type in {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "class_declaration",
            "variable_declarator",
        }:
            name = current.child_by_field_name("name")
            if name is not None:
                return js_text(name)
        current = current.parent
    return "<module>"


def _record(
    source_path: str,
    line: int,
    column: int,
    symbol: str,
    vocabulary: str,
    values: list[str],
) -> DispatcherRecord:
    """Build one dispatcher record.

    Args:
        source_path: Path to record.
        line: 1-indexed line.
        column: 0-indexed column.
        symbol: Enclosing declaration name.
        vocabulary: The branched-on expression.
        values: The literal branch values.

    Returns:
        The dispatcher record, always unreviewed.
    """
    return DispatcherRecord(
        source_path=source_path,
        source_line=line,
        source_column=column,
        symbol=symbol,
        vocabulary=vocabulary,
        branch_count=len(values),
        branch_values=tuple(sorted(values)),
    )
