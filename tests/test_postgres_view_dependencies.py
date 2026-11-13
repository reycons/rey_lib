"""#853: view/function dependencies are exact catalog facts, including overloads."""

from __future__ import annotations

import pytest

from rey_lib.db import postgres_utils


@pytest.mark.parametrize("view_kind", ["view", "materialized_view"])
def test_view_references_keep_catalog_identity_and_function_signature(
    monkeypatch, view_kind,
) -> None:
    # Deliberate pg_class/pg_proc OID collisions: neither may mask the other.
    relations = [
        {"oid": 10, "schema": "code", "name": "report", "object_type": view_kind,
         "definition_hash": "view-hash"},
        {"oid": 20, "schema": "code", "name": "facts", "object_type": "table"},
    ]
    routines = [
        {"oid": 10, "schema": "code", "name": "other", "object_type": "function",
         "signature": "text"},
        {"oid": 20, "schema": "code", "name": "resolve", "object_type": "function",
         "signature": "boolean, boolean"},
        {"oid": 21, "schema": "code", "name": "resolve", "object_type": "function",
         "signature": "text"},
    ]

    def rows(_conn, statement, params):
        assert params == ["code", "code"]
        if "JOIN pg_rewrite" in statement:
            assert "d.classid = 'pg_rewrite'::regclass" in statement
            assert "v.relkind IN ('v','m')" in statement
            if "JOIN pg_proc" in statement:
                assert "d.refclassid = 'pg_proc'::regclass" in statement
                return [(10, 20)]
            assert "d.refclassid = 'pg_class'::regclass" in statement
            return [(10, 20)]
        return []

    monkeypatch.setattr(postgres_utils, "_reference_rows", rows)
    edges = postgres_utils._catalog_observations(
        object(), "code", set(), {}, relations, routines,
    )

    assert len(edges) == 2
    assert {
        (e["to_object_type"], e["to_object_name"], e["to_signature"])
        for e in edges
    } == {("table", "facts", ""), ("function", "resolve", "boolean, boolean")}
    for edge in edges:
        assert edge["from_object_name"] == "report"
        assert edge["from_object_type"] == view_kind
        assert edge["reference_kind"] == "reference"
        assert edge["resolution"] == "exact"
        assert edge["fact_source"] == "catalog"
        assert edge["observed_definition_hash"] == "view-hash"


def test_function_outside_snapshot_is_unresolved_even_if_a_relation_has_its_oid(
    monkeypatch,
) -> None:
    relations = [
        {"oid": 10, "schema": "code", "name": "report", "object_type": "view",
         "definition_hash": "view-hash"},
        {"oid": 20, "schema": "code", "name": "unrelated", "object_type": "table"},
    ]

    def rows(_conn, statement, _params):
        return [(10, 20)] if "JOIN pg_rewrite" in statement and "JOIN pg_proc" in statement else []

    monkeypatch.setattr(postgres_utils, "_reference_rows", rows)
    edges = postgres_utils._catalog_observations(
        object(), "code", set(), {}, relations, [],
    )
    assert len(edges) == 1
    assert edges[0]["resolution"] == "unresolved"
    assert edges[0]["to_object_name"] == ""
    assert edges[0]["observed_target"] == "(outside indexed scope)"


def test_trigger_relation_and_function_remain_distinct_when_their_oids_match(
    monkeypatch,
) -> None:
    def rows(_conn, statement, _params):
        return [("changed", "code", 20, 20)] if "FROM pg_trigger" in statement else []

    monkeypatch.setattr(postgres_utils, "_reference_rows", rows)
    edges = postgres_utils._catalog_observations(
        object(), None, set(), {},
        [{"oid": 20, "schema": "code", "name": "facts", "object_type": "table"}],
        [{"oid": 20, "schema": "code", "name": "record_change", "object_type": "function",
          "signature": ""}],
        [{"schema": "code", "name": "changed"}],
    )
    assert {(e["reference_kind"], e["to_object_name"]) for e in edges} == {
        ("trigger", "facts"), ("call", "record_change"),
    }
