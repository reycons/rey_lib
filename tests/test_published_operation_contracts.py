"""Every installed application's published operation contracts (backlog 939).

The contracts are authored in each package registration from the readers that
consume the settings. These assert, over what is actually installed:

- every published operation has exactly one contract, which loads;
- canonical names only: no reader alias, legacy shape or retired key;
- keyed maps stay maps, and a dispatcher's variants stay one operation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rey_lib.config.applications import _operations
from rey_lib.config.registration import discover_registrations

#: The operations each application publishes, as traced (backlog 932).
_PUBLISHED = {
    "data_engine": {"inventory_source_files", "classify_source_files", "excel_conversion",
                    "sanitize_file", "profile_csv_record_types", "create_prepared_files",
                    "file_operation", "sql_operation", "validate", "etl_operation",
                    "transform_files", "load_files", "validate_load", "sql_apply",
                    "prepare_rule_set_inputs"},
    "rey_analyzer": {"analysis"},
    "rey_db_admin": {"export_database_ddl", "git_commit", "lint_sql", "assist_sql",
                     "recreate_objects", "verify_diff"},
    "rey_repository_map": {"index", "publish-database", "publish-capability-generation",
                           "publish-architecture-generation", "index-synopsis"},
}

#: Retired or refused keys, never published by any operation.
_RETIRED = {"procedure", "restore_mappings", "config_ref", "selections", "kickouts", "file_kickouts"}

#: Reader aliases, never published by the operation whose reader accepts them.
#: Per operation: ``values`` is an alias only for sql_operation's params; a
#: classification variable's ``values`` is canonical.
_ALIASES = {
    ("data_engine", "inventory_source_files"): {"file_pattern"},
    ("data_engine", "sql_operation"): {"values", "routine_name"},
}

@pytest.fixture(scope="module")
def operations() -> dict[tuple[str, str], Any]:
    registrations = discover_registrations()
    if not set(_PUBLISHED) <= set(registrations):
        pytest.skip("the five publishing applications are not all installed")
    return {
        (name, op.name): op
        for name, registration in registrations.items()
        for op in _operations(registration, name)
    }


def _keys(node: Any) -> set[str]:
    """Every property name a schema declares, at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            found |= _keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keys(item)
    return found


def test_each_published_operation_has_exactly_one_contract(operations) -> None:
    published: dict[str, set[str]] = {}
    for application, name in operations:
        published.setdefault(application, set()).add(name)
    assert published == _PUBLISHED
    assert len(operations) == 27


def test_canonical_names_only(operations) -> None:
    offenders = {
        f"{app}.{name}": sorted(found)
        for (app, name), op in operations.items()
        if (found := _keys(op.contract_definition) & (_RETIRED | _ALIASES.get((app, name), set())))
    }
    assert offenders == {}


def test_the_workflow_engine_vocabulary_is_never_published(operations) -> None:
    engine = {"implementation", "apply_only", "always_run"}
    offenders = {
        f"{app}.{name}" for (app, name), op in operations.items()
        if engine & set(op.contract_definition.get("properties", {}))
    }
    assert offenders == set()


def test_unsupported_keys_are_not_published_where_nothing_reads_them(operations) -> None:
    profile = operations[("data_engine", "profile_csv_record_types")].contract_definition
    assert set(profile["properties"]["file_selection"]["properties"]) == {"operation"}
    assert "kickouts" not in operations[("data_engine", "prepare_rule_set_inputs")].contract_definition["properties"]


def test_keyed_maps_remain_maps(operations) -> None:
    sanitize = operations[("data_engine", "sanitize_file")].contract_definition
    feeds = sanitize["properties"]["sanitization"]["properties"]["feeds"]
    assert feeds["type"] == "object" and feeds["additionalProperties"] == {"$ref": "#/$defs/policy"}
    policy = sanitize["$defs"]["policy"]["properties"]
    for table in ("remove", "preserve", "preserve_if_quoted", "replace"):
        assert policy[table]["type"] == "object"
        assert policy[table]["propertyNames"] == {"pattern": "^U\\+[0-9A-F]{4,6}$"}
    assert policy["line_repair"]["type"] == "object" and "additionalProperties" in policy["line_repair"]
    params = operations[("data_engine", "sql_operation")].contract_definition["properties"]["params"]
    assert params["type"] == "object" and "additionalProperties" in params


def test_a_dispatchers_variants_stay_one_operation(operations) -> None:
    file_operation = operations[("data_engine", "file_operation")].contract_definition
    chosen = [variant["properties"]["operation"]["const"] for variant in file_operation["oneOf"]]
    assert chosen == ["discover", "discover_file", "move", "delete"]
    assert file_operation["properties"]["operation"]["enum"] == chosen


def test_every_sample_is_plain_json(operations) -> None:
    for op in operations.values():
        assert json.loads(json.dumps(op.contract_sample)) == op.contract_sample
