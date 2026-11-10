"""A validated programmable-object filter selects the canonical export kinds."""
from __future__ import annotations

import pytest

from rey_lib.db.database_objects import (
    DatabaseFunction, DatabaseProcedure, DatabaseTrigger, DatabaseView,
)
from rey_lib.db.object_filter import apply_object_filter, validate_object_filter

OWNERS = (DatabaseProcedure, DatabaseFunction, DatabaseView, DatabaseTrigger)


@pytest.mark.parametrize("owner", OWNERS, ids=lambda owner: owner.KIND)
def test_validated_kind_selects_the_corresponding_export_entry(owner):
    entries = [{"key": f"{kind.KIND}:public.object_{i}", "schema": "public",
                "object_type": kind.KIND, "name": f"object_{i}"}
               for i, kind in enumerate(OWNERS)]
    config = {"include": [{"object_type": owner.KIND, "force": True}]}
    assert validate_object_filter(config) == []
    assert apply_object_filter(entries, config) == [
        {**entry, "force": True} for entry in entries if entry["object_type"] == owner.KIND
    ]


@pytest.mark.parametrize("owner", OWNERS, ids=lambda owner: owner.KIND)
def test_exclusion_uses_the_same_canonical_kind(owner):
    entries = [{"schema": "public", "object_type": kind.KIND, "name": "one"}
               for kind in OWNERS]
    config = {"include": [{"schema": "public"}], "exclude": [{"object_type": owner.KIND}]}
    assert validate_object_filter(config) == []
    assert len(apply_object_filter(entries, config)) == len(OWNERS) - 1
    assert all(entry["object_type"] != owner.KIND for entry in apply_object_filter(entries, config))


@pytest.mark.parametrize("section", ["include", "exclude"])
@pytest.mark.parametrize("kind", ["procedure", "function", "view", "trigger", "unknown", "tables"])
def test_noncanonical_or_unsupported_types_are_refused(section, kind):
    config = {"include": [{"schema": "public"}], section: [{"object_type": kind}]}
    errors = validate_object_filter(config)
    assert len(errors) == 1
    assert f"object_filter.{section}[0]: invalid object_type '{kind}'" in errors[0]
    assert all(owner.KIND in errors[0] for owner in OWNERS)


@pytest.mark.parametrize("wildcard", [None, "", "*"])
def test_schema_scoped_wildcards_still_select_all_kinds(wildcard):
    config = {"include": [{"schema": "public", "object_type": wildcard}]}
    assert validate_object_filter(config) == []
    entries = [{"schema": "public", "object_type": owner.KIND, "name": "one"} for owner in OWNERS]
    assert len(apply_object_filter(entries, config)) == len(OWNERS)
