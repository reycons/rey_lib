"""Registration discovery for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-003, REQ-040 to REQ-043).

Three declaration forms are detected, because a registry is populated in three
different ways:

- a call, ``host.register("pipeline_builder", descriptor)``
- a literal entry, an object declaring ``id: "run_workflow"``
- a backend collection naming frontend objects by string

Registries are matched by method name plus receiver, never by a single dotted
name, because one registry concept is reached through several receiver
spellings — a module-local alias, a global, or an accessor call result.

An id that is not a literal is recorded with ``registered_id_resolved`` false.
A syntax scanner cannot follow a variable, and inventing an id would be worse
than recording that a registration site exists.
"""

from __future__ import annotations

import ast
from fnmatch import fnmatchcase
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
    BackendRegistrationRule,
    DeclaredRegistrationRule,
    FileRecord,
    RegistrationRecord,
    RegistrationRule,
    ScanRules,
)

__all__ = ["extract_registrations"]

logger = get_logger(__name__)

# String literal node types across the supported grammars.
_STRING_NODES = frozenset({"string", "template_string"})


def extract_registrations(
    repo_root: Path,
    files: list[FileRecord],
    rules: ScanRules,
) -> list[RegistrationRecord]:
    """Extract every explicit id-to-object registration in a repository.

    Args:
        repo_root: Repository root the file paths are relative to.
        files: The inventoried files to scan.
        rules: The scanned repository's own scan rules.

    Returns:
        Registrations sorted by path, line and registry.
    """
    js_languages = set(supported_js_languages())
    records: list[RegistrationRecord] = []

    for file_record in files:
        if not rules.extracts_facts_from(file_record):
            continue
        path = repo_root / file_record.path
        if file_record.language in js_languages:
            records.extend(_js_registrations(path, file_record, rules))
        elif file_record.language == "Python":
            records.extend(_python_registrations(path, file_record, rules))

    records.sort(
        key=lambda record: (
            record.source_path,
            record.source_line,
            record.registry,
            record.registered_id,
        )
    )
    return records


def _js_registrations(
    path: Path,
    file_record: FileRecord,
    rules: ScanRules,
) -> list[RegistrationRecord]:
    """Return call-site and literal registrations in one JS/TS/TSX file.

    Args:
        path: Absolute path to the file.
        file_record: The file's inventory record.
        rules: The scanned repository's scan rules.

    Returns:
        The registrations found.
    """
    applicable_calls = rules.registration_rules
    applicable_literals = tuple(
        rule
        for rule in rules.declared_registration_rules
        if _matches_any(file_record.path, rule.path_globs)
    )
    if not applicable_calls and not applicable_literals:
        return []

    root = parse_js_root(path, file_record.language)
    nodes = walk_js_nodes(root)
    records: list[RegistrationRecord] = []

    for node in nodes:
        if node.type == "call_expression":
            records.extend(_call_registrations(node, file_record.path, applicable_calls))
        elif node.type in {"object", "object_pattern"}:
            records.extend(_literal_registrations(node, file_record.path, applicable_literals))
    return records


def _call_registrations(
    node,
    source_path: str,
    call_rules: tuple[RegistrationRule, ...],
) -> list[RegistrationRecord]:
    """Return registrations proved by one call expression.

    Args:
        node: A ``call_expression`` node.
        source_path: Path to record.
        call_rules: Call-site rules to try.

    Returns:
        The registrations matched by the rules.
    """
    callee = node.child_by_field_name("function")
    if callee is None or callee.type not in {"member_expression", "optional_member_expression"}:
        return []
    property_node = callee.child_by_field_name("property")
    object_node = callee.child_by_field_name("object")
    if property_node is None or object_node is None:
        return []
    method = js_text(property_node)
    receiver = js_text(object_node)

    arguments = node.child_by_field_name("arguments")
    argument_nodes = [] if arguments is None else list(arguments.named_children)

    records = []
    for rule in call_rules:
        if rule.method != method or not _matches_any(receiver, rule.receiver_globs):
            continue
        if rule.id_argument >= len(argument_nodes):
            continue
        id_node = argument_nodes[rule.id_argument]
        registered_id, resolved = _registered_id(id_node, rule.id_property)
        implementation = (
            js_text(argument_nodes[-1]).splitlines()[0]
            if argument_nodes
            else js_text(id_node)
        )
        line, column = node.start_point
        records.append(
            RegistrationRecord(
                source_path=source_path,
                source_line=line + 1,
                source_column=column,
                registry=rule.registry,
                registered_id=registered_id,
                implementation=implementation,
                registration_kind=rule.registration_kind,
                registered_id_resolved=resolved,
            )
        )
    return records


def _registered_id(id_node, id_property: str | None) -> tuple[str, bool]:
    """Return the registered id and whether it was resolved from a literal.

    Args:
        id_node: The argument holding the id.
        id_property: Property to read when the argument is an object.

    Returns:
        The id text and a resolved flag.
    """
    if id_node.type in _STRING_NODES:
        return js_text(id_node).strip("\"'`"), True
    if id_property is not None and id_node.type == "object":
        value = _object_property(id_node, id_property)
        if value is not None:
            return value, True
    return js_text(id_node).splitlines()[0], False


def _object_property(node, property_name: str) -> str | None:
    """Return a string property's value from an object literal.

    Args:
        node: An ``object`` node.
        property_name: Property to read.

    Returns:
        The literal value, or None when absent or not a literal.
    """
    for pair in node.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is None or value is None:
            continue
        if js_text(key).strip("\"'`") != property_name:
            continue
        if value.type in _STRING_NODES:
            return js_text(value).strip("\"'`")
    return None


def _literal_registrations(
    node,
    source_path: str,
    literal_rules: tuple[DeclaredRegistrationRule, ...],
) -> list[RegistrationRecord]:
    """Return registrations declared by one object literal.

    Args:
        node: An ``object`` node.
        source_path: Path to record.
        literal_rules: Literal-declaration rules applying to this file.

    Returns:
        The registrations matched by the rules.
    """
    records = []
    for rule in literal_rules:
        value = _object_property(node, rule.id_property)
        if value is None:
            continue
        line, column = node.start_point
        records.append(
            RegistrationRecord(
                source_path=source_path,
                source_line=line + 1,
                source_column=column,
                registry=rule.registry,
                registered_id=value,
                implementation=source_path,
                registration_kind=rule.registration_kind,
            )
        )
    return records


def _python_registrations(
    path: Path,
    file_record: FileRecord,
    rules: ScanRules,
) -> list[RegistrationRecord]:
    """Return backend-collection registrations in one Python file.

    Args:
        path: Absolute path to the file.
        file_record: The file's inventory record.
        rules: The scanned repository's scan rules.

    Returns:
        The registrations found.
    """
    applicable = [
        rule
        for rule in rules.backend_registration_rules
        if _matches_any(file_record.path, rule.path_globs)
    ]
    if not applicable:
        return []

    try:
        tree = ast.parse(read_text_file(path), filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"Could not parse Python file {path}: {exc}") from exc

    records = []
    for rule in applicable:
        for entry in _collection_entries(tree, rule.symbol):
            records.extend(_backend_records(entry, file_record.path, rule))
    return records


def _collection_entries(tree: ast.Module, symbol: str) -> list[ast.Dict]:
    """Return the dict literals inside a module-level list assignment.

    Args:
        tree: Parsed module.
        symbol: Module-level name holding the collection.

    Returns:
        The dict entries, empty when the symbol is absent or not a list.
    """
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if not any(isinstance(target, ast.Name) and target.id == symbol for target in targets):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            return [element for element in value.elts if isinstance(element, ast.Dict)]
    return []


def _backend_records(
    entry: ast.Dict,
    source_path: str,
    rule: BackendRegistrationRule,
) -> list[RegistrationRecord]:
    """Return one registration per implementation named by a backend entry.

    Each implementation string is its own fact: a backend entry that names a
    server object and a client file declares two things reachable, and the map
    must be able to say which line proved each.

    Args:
        entry: One dict literal from the collection.
        source_path: Path to record.
        rule: The backend rule being applied.

    Returns:
        The registrations, empty when the entry declares no id.
    """
    values = _dict_string_values(entry)
    registered_id = values.get(rule.id_key)
    if registered_id is None:
        return []
    implementations = [
        (key, values[key]) for key in rule.implementation_keys if values.get(key)
    ]
    if not implementations:
        implementations = [(rule.id_key, registered_id)]
    return [
        RegistrationRecord(
            source_path=source_path,
            source_line=entry.lineno,
            source_column=entry.col_offset,
            registry=f"{rule.registry}.{key}",
            registered_id=registered_id,
            implementation=implementation,
            registration_kind=rule.registration_kind,
        )
        for key, implementation in implementations
    ]


def _dict_string_values(entry: ast.Dict) -> dict[str, str]:
    """Return the string-valued keys of a dict literal.

    Args:
        entry: A dict literal node.

    Returns:
        Key to string value for every literal string entry.
    """
    values: dict[str, str] = {}
    for key_node, value_node in zip(entry.keys, entry.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            values[key_node.value] = value_node.value
    return values


def _matches_any(value: str, globs: tuple[str, ...]) -> bool:
    """Return True when a value matches any glob.

    Args:
        value: Text to match.
        globs: Configured glob patterns.

    Returns:
        True when at least one glob matches, or when no globs are configured
        and the rule therefore applies everywhere.
    """
    if not globs:
        return True
    return any(fnmatchcase(value, pattern) for pattern in globs)
