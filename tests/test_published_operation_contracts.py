"""Every installed application's published operation contracts (backlog 939).

The contracts are authored in each package registration from the readers that
consume the settings. These assert, over what is actually installed:

- every published operation has exactly one contract, which loads;
- canonical names only: no reader alias, legacy shape or retired key;
- keyed maps stay maps, and a dispatcher's variants stay one operation;
- every live workflow step satisfies its operation's contract, tolerating only
  the unsupported keys existing configuration is known to carry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rey_lib.config.applications import _operations
from rey_lib.config.config_context import _stamp_workflow_ownership
from rey_lib.config.config_loader import _deep_merge as _merge_files
from rey_lib.config.config_loader import _load_yaml, _yaml_files_in_folder
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.operation_contract import contract_review
from rey_lib.config.registration import discover_registrations
from rey_lib.workflow.coordinator import (
    _declared_config,
    _deep_merge,
    _expand_config,
    _get,
    _resolve_tokens,
    _to_mapping,
)

_INSTALLATIONS = Path("/workspaces/installations")

#: Every installation's workflow folders, as each installation declares them.
#: Pipeline folders are included: a pipeline-local workflow (rey_analyzer's
#: generate_* workflows) is a workflow like any other.
_WORKFLOW_FOLDERS = (
    ("local", _INSTALLATIONS / "local" / "work" / "workflows"),
    ("local", _INSTALLATIONS / "local" / "work" / "pipelines"),
    ("wolff_popper", _INSTALLATIONS / "wolff_popper" / "config" / "work" / "workflows"),
    ("wolff_popper", _INSTALLATIONS / "wolff_popper" / "config" / "work" / "pipelines"),
    ("admin", _INSTALLATIONS / "admin" / "work" / "workflows"),
)


def _merged_entries(folder: Path) -> list[dict[str, Any]]:
    """The resolved workflow entries of one folder, merged as the config load does."""
    raw: dict[str, Any] = {}
    for one in _yaml_files_in_folder(folder, folder / "none"):
        raw = _merge_files(raw, _stamp_workflow_ownership(_load_yaml(one)))
    return [entry for entry in raw.get("workflows") or [] if isinstance(entry, dict)]


def _live_entries() -> list[tuple[str, dict[str, Any]]]:
    """Every live workflow entry, with its installation."""
    found = [
        (installation, entry)
        for installation, root in _WORKFLOW_FOLDERS if root.is_dir()
        for child in sorted(root.iterdir()) if child.is_dir()
        for entry in _merged_entries(child)
    ]
    if not found:
        pytest.skip("no installation workflow definitions are present")
    return found

#: The operations each application publishes, as traced (backlog 932).
_PUBLISHED = {
    "file_operator": {"prepare_rule_set_inputs"},
    "rey_analyzer": {"analysis"},
    "rey_db_admin": {"export_database_ddl", "git_commit", "lint_sql", "assist_sql",
                     "recreate_objects", "verify_diff"},
    "rey_loader": {"inventory_source_files", "classify_source_files", "excel_conversion",
                   "sanitize_file", "profile_csv_record_types", "create_prepared_files",
                   "file_operation", "sql_operation", "validate", "etl_operation",
                   "transform_files", "load_files", "validate_load", "sql_apply"},
    "rey_repository_map": {"index", "publish-database", "publish-capability-generation",
                           "publish-architecture-generation", "index-synopsis"},
}

#: Retired or refused keys, never published by any operation.
_RETIRED = {"procedure", "restore_mappings", "config_ref", "selections", "kickouts", "file_kickouts"}

#: Reader aliases, never published by the operation whose reader accepts them.
#: Per operation: ``values`` is an alias only for sql_operation's params; a
#: classification variable's ``values`` is canonical.
_ALIASES = {
    ("rey_loader", "inventory_source_files"): {"file_pattern"},
    ("rey_loader", "sql_operation"): {"values", "routine_name"},
}

#: The unsupported keys live configuration is known to carry, tolerated (user,
#: 2026-10-13): no reader reads them and no contract publishes them.
_KNOWN_UNSUPPORTED = {
    ("local", "inventory_and_prepare_files_loader", "profile_csv_record_types"),
    ("wolff_popper", "inventory_and_prepare_files_loader", "profile_csv_record_types"),
    ("wolff_popper", "inventory_and_prepare_files", "prepare_analysis_inputs"),
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
    profile = operations[("rey_loader", "profile_csv_record_types")].contract_definition
    assert set(profile["properties"]["file_selection"]["properties"]) == {"operation"}
    assert "kickouts" not in operations[("file_operator", "prepare_rule_set_inputs")].contract_definition["properties"]


def test_keyed_maps_remain_maps(operations) -> None:
    sanitize = operations[("rey_loader", "sanitize_file")].contract_definition
    feeds = sanitize["properties"]["sanitization"]["properties"]["feeds"]
    assert feeds["type"] == "object" and feeds["additionalProperties"] == {"$ref": "#/$defs/policy"}
    policy = sanitize["$defs"]["policy"]["properties"]
    for table in ("remove", "preserve", "preserve_if_quoted", "replace"):
        assert policy[table]["type"] == "object"
        assert policy[table]["propertyNames"] == {"pattern": "^U\\+[0-9A-F]{4,6}$"}
    assert policy["line_repair"]["type"] == "object" and "additionalProperties" in policy["line_repair"]
    params = operations[("rey_loader", "sql_operation")].contract_definition["properties"]["params"]
    assert params["type"] == "object" and "additionalProperties" in params


def test_a_dispatchers_variants_stay_one_operation(operations) -> None:
    file_operation = operations[("rey_loader", "file_operation")].contract_definition
    chosen = [variant["properties"]["operation"]["const"] for variant in file_operation["oneOf"]]
    assert chosen == ["discover", "discover_file", "move", "delete"]
    assert file_operation["properties"]["operation"]["enum"] == chosen


def test_every_sample_is_plain_json(operations) -> None:
    for op in operations.values():
        assert json.loads(json.dumps(op.contract_sample)) == op.contract_sample


def test_every_live_workflow_step_satisfies_its_contract(operations) -> None:
    refused: list[str] = []
    tolerated: set[tuple[str, str, str]] = set()
    checked = 0
    for installation, entry in _live_entries():
        if entry.get("enabled", True) is False:
            continue
        name = entry["name"]
        tokens = _resolve_tokens(_get(entry, "tokens"))
        processes = _to_mapping(_get(entry, "processes"))
        for step in entry.get("steps") or []:
            declared = _to_mapping(processes.get(step.get("process")))
            op = operations.get((entry.get("app"), str(declared.get("implementation") or "")))
            if op is None:
                continue
            checked += 1
            merged = _deep_merge(declared, _to_mapping(_get(step, "config")))
            effective = _expand_config(_declared_config(merged), tokens)
            unsupported, violations = contract_review(
                op.contract_definition, to_plain_data(effective))
            refused += [f"{installation}/{name}/{step['id']}: {one}" for one in violations]
            if unsupported:
                tolerated.add((installation, name, step["id"]))
    assert refused == []
    assert tolerated == _KNOWN_UNSUPPORTED
    assert checked == 52
