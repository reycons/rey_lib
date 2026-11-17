"""Columns In -> Transform -> Columns Out (#1425), from definitions alone.

The analyser reads derivations from the same pglast trees as the reference
analysis; the binder settles them against a catalog snapshot. Both are tested on
definition files (``tests/fixtures/column_derivations``) and catalog-shaped
rows: no database is opened. The chain across objects is walked the way
publication stores it -- every derivation edge in data-flow direction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pglast", reason="column derivations need the rey_lib[db_references] extra")

from rey_lib.db.postgres_references import analyse_routine, analyse_view  # noqa: E402
from rey_lib.db.postgres_utils import _DerivationBinder  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "column_derivations"


def _sql(name: str) -> str:
    """One fixture definition."""
    return (FIXTURES / f"{name}.sql").read_text(encoding="utf-8")


def _by_key(analysis: Any) -> dict[str, Any]:
    return {one.key: one for one in analysis.derivations}


def _inputs(derivation: Any) -> set[str]:
    return {one.observed for one in derivation.inputs}


def _sources(one: Any) -> list[tuple[str, str]]:
    """An input's candidate sources, without their query level."""
    return [(kind, ref) for kind, ref, _level in one.candidates]


class TestTheAnalyserReadsAView:
    """A2: the portfolio_exposure shape."""

    def test_each_output_column_has_its_value_transform(self) -> None:
        found = _by_key(analyse_view(_sql("portfolio_exposure")))

        exposure = found["1.q.value.2.exposure"]
        assert exposure.expression == "sum(t.quantity * t.price * l.multiplier)"
        assert _inputs(exposure) == {"t.quantity", "t.price", "l.multiplier"}
        assert (exposure.output_kind, exposure.output_position) == ("self", 2)
        assert "sum" in exposure.calls
        assert _inputs(found["1.q.value.1.account_id"]) == {"t.account_id"}

    def test_the_join_and_the_grouping_shape_the_values(self) -> None:
        found = _by_key(analyse_view(_sql("portfolio_exposure")))

        join, group = found["1.q.join.1.join"], found["1.q.group.1.group"]
        assert join.expression == "t.security_id = l.security_id"
        assert _inputs(join) == {"t.security_id", "l.security_id"}
        assert group.expression == "GROUP BY t.account_id"
        assert found["1.q.value.2.exposure"].conditions == [join.key, group.key]

    def test_an_alias_names_its_relation(self) -> None:
        exposure = _by_key(analyse_view(_sql("portfolio_exposure")))["1.q.value.2.exposure"]

        sources = {one.observed: _sources(one) for one in exposure.inputs}
        assert sources["l.multiplier"] == [("relation", "app.security_lookup")]

    def test_keys_are_the_same_on_every_reading(self) -> None:
        """A5 at the analyser: an unchanged definition yields identical keys."""
        first = list(_by_key(analyse_view(_sql("recursive_cte"))))
        assert first == list(_by_key(analyse_view(_sql("recursive_cte"))))


class TestTheAnalyserReadsQueryStructure:
    """CTEs, recursion, set operations, subqueries, functions in FROM, rows."""

    def test_a_cte_column_is_read_through_the_cte_transform(self) -> None:
        found = _by_key(analyse_view(_sql("cte")))

        assert _sources(found["1.q.value.1.doubled"].inputs[0]) == [
            ("transforms", "1.q.cte:r.value.1.doubled")]
        assert found["1.q.cte:r.value.1.doubled"].output_kind == "intermediate"

    def test_a_recursive_cte_reads_itself_through_its_anchor(self) -> None:
        found = _by_key(analyse_view(_sql("recursive_cte")))

        join = found["1.q.cte:w.r.join.1.join"]
        sources = {one.observed: _sources(one) for one in join.inputs}
        assert sources["w.id"] == [("transforms", "1.q.cte:w.l.value.1.id")]
        assert _sources(found["1.q.value.1.id"].inputs[0]) == [
            ("transforms", "1.q.cte:w.l.value.1.id|1.q.cte:w.r.value.1.id")]

    def test_a_union_feeds_one_column_from_every_branch(self) -> None:
        found = _by_key(analyse_view(_sql("union")))

        assert {key for key, one in found.items() if one.output_position == 1} == {
            "1.q.l.value.1.a", "1.q.r.value.1.b"}

    def test_a_subquery_alias_list_renames_its_columns(self) -> None:
        found = _by_key(analyse_view(_sql("values_alias")))

        assert _sources(found["1.q.value.1.label"].inputs[0]) == [
            ("transforms", "1.q.sub:b.row1.value.1.column1")]

    def test_a_function_in_from_is_named_for_its_output_columns(self) -> None:
        found = _by_key(analyse_view(_sql("function_in_from")))

        assert _sources(found["1.q.value.1.total"].inputs[0]) == [
            ("function", "app.f_totals#account,total")]

    def test_a_whole_row_is_read_as_a_row(self) -> None:
        found = _by_key(analyse_view(_sql("whole_row")))

        assert _sources(found["1.q.value.1.doc"].inputs[0]) == [("row", "app.dim")]

    def test_a_using_column_is_one_value_read_from_both_sides(self) -> None:
        found = _by_key(analyse_view(_sql("using")))

        assert found["1.q.value.1.k"].inputs[0].merged is True
        assert _inputs(found["1.q.join.1.join"]) == {"s.k", "d.k"}

    def test_what_cannot_be_established_is_a_derivation_gap(self) -> None:
        analysis = analyse_view(_sql("star"))

        assert analysis.derivation_gaps == ["derivation: SELECT * at q: columns not established"]


class TestTheAnalyserReadsRoutines:
    """Statements whose results land in columns; reference analysis unchanged."""

    def test_insert_select_and_its_upsert(self) -> None:
        analysis = analyse_routine(_sql("upsert_procedure"), "plpgsql")
        found = _by_key(analysis)

        value = found["1.q.value.1.x"]
        assert (value.output_kind, value.output_relation, value.output_column) == (
            "relation", "app.stage", "x")
        assert value.expression == "s.a * d.m"
        assert _sources(found["1.u.value.1.x"].inputs[0]) == [("transforms", "1.q.value.1.x")]
        # The READ/WRITE analysis is exactly what it was.
        assert (analysis.status, analysis.gaps) == ("complete", [])
        assert analysis.writes == {"app.stage"}
        assert analysis.reads == {"app.src", "app.dim"}

    def test_return_query_lands_in_the_routines_output_columns(self) -> None:
        found = _by_key(analyse_routine(_sql("return_query"), "plpgsql"))

        # An unnamed expression's column is "?column?", as PostgreSQL names it.
        returned = found["1.q.value.1.?column?"]
        assert (returned.output_kind, returned.output_position) == ("return", 1)

    def test_a_select_into_a_variable_is_not_a_column_derivation(self) -> None:
        analysis = analyse_routine(_sql("select_into"), "plpgsql")

        assert analysis.derivations == [] and "v" in analysis.variables


# -- binding --------------------------------------------------------------------

def _relation(schema: str, name: str, object_type: str, *columns: str,
              definition: str = "") -> dict[str, Any]:
    return {"oid": f"{schema}.{name}", "schema": schema, "name": name,
            "object_type": object_type, "definition": definition,
            "definition_hash": f"h:{name}", "columns": [
                {"name": one, "ordinal": at, "data_type": "numeric"}
                for at, one in enumerate(columns, start=1)]}


def _routine(schema: str, name: str, definition: str, *parameters: tuple[str, str],
             object_type: str = "procedure") -> dict[str, Any]:
    return {"oid": f"{schema}.{name}", "schema": schema, "name": name,
            "object_type": object_type, "signature": "", "language": "plpgsql",
            "definition": definition, "definition_hash": f"h:{name}", "parameters": [
                {"member_name": one, "ordinal": at, "data_type": "numeric",
                 "member_mode": mode, "has_default": False}
                for at, (one, mode) in enumerate(parameters, start=1)]}


def _binder(relations: list[dict[str, Any]], routines: list[dict[str, Any]],
            ) -> _DerivationBinder:
    in_scope: dict[tuple[str, str], list[dict[str, str]]] = {}
    for one in (*relations, *routines):
        in_scope.setdefault((one["schema"], one["name"]), []).append({
            "schema": one["schema"], "name": one["name"],
            "object_type": one["object_type"], "signature": one.get("signature", "")})
    return _DerivationBinder(in_scope, relations, routines)


def _view(name: str, fixture: str, *columns: str) -> dict[str, Any]:
    return _relation("app", name, "view", *columns, definition=_sql(fixture))


def _bound_view(view: dict[str, Any], relations: list[dict[str, Any]],
                routines: list[dict[str, Any]] | None = None) -> tuple[list, list, int]:
    return _binder([*relations, view], routines or []).bind(
        view, analyse_view(view["definition"]), "view_definition")


SRC = _relation("app", "src", "table", "a", "k", "run_id")
DIM = _relation("app", "dim", "table", "k", "m")
STAGE = _relation("app", "stage", "table", "x", "k")


class TestTheBinder:
    """Names become members; what does not bind is a gap, never dropped."""

    def test_inputs_and_outputs_bind_to_members(self) -> None:
        members, rows, gaps = _bound_view(_view("v", "sum_of_src", "total"), [SRC])

        assert gaps == 0
        assert (members[0]["member_kind"], members[0]["expression"],
                members[0]["data_type"], members[0]["member_mode"]) == (
            "transform", "sum(s.a)", "numeric", "value")
        assert {(r["reference_kind"], r["to_object_name"], r["to_member_name"])
                for r in rows} == {("derivation_input", "src", "a"),
                                   ("derivation_output", "v", "total")}

    def test_an_unqualified_column_in_two_relations_is_ambiguous(self) -> None:
        _members, rows, gaps = _bound_view(_view("v", "ambiguous", "k"), [SRC, DIM])

        assert gaps == 1
        assert [r["resolution"] for r in rows if r["evidence"] == "analysis_gap"] == [
            "ambiguous"]

    def test_a_using_column_binds_to_both_sides(self) -> None:
        _members, rows, gaps = _bound_view(_view("v", "using", "k"), [SRC, DIM])

        assert gaps == 0
        assert {r["to_object_name"] for r in rows
                if r["from_member_name"] == "1.q.value.1.k"
                and r["reference_kind"] == "derivation_input"} == {"src", "dim"}

    def test_a_relation_outside_the_snapshot_is_unresolved_with_a_gap(self) -> None:
        _members, rows, gaps = _bound_view(_view("v", "outside_snapshot", "a"), [])

        assert gaps == 1
        gap = next(r for r in rows if r["evidence"] == "analysis_gap")
        assert (gap["resolution"], gap["to_object_name"]) == ("unresolved", "")
        assert gap["observed_target"].endswith("not indexed")

    def test_a_variable_is_said_to_be_a_variable(self) -> None:
        routine = _routine("app", "p", _sql("variable_insert"))
        _members, rows, gaps = _binder([STAGE], [routine]).bind(
            routine, analyse_routine(routine["definition"], "plpgsql"),
            "routine_definition", routine["parameters"])

        assert gaps == 1
        assert any(r["observed_target"] == "v: a variable, not traced in v1" for r in rows)

    def test_a_function_in_from_reads_the_functions_output_parameter(self) -> None:
        function = _routine("app", "f_totals", "-- not read", ("p_run", "IN"),
                            ("account", "TABLE"), ("total", "TABLE"), object_type="function")
        _members, rows, gaps = _bound_view(
            _view("v", "function_in_from_unaliased", "total"), [], [function])

        assert gaps == 0
        assert ("f_totals", "parameter", "total") in {
            (r["to_object_name"], r["to_member_kind"], r["to_member_name"]) for r in rows}

    def test_a_catalog_column_no_transform_reads_is_a_cross_check_reason(self) -> None:
        view = _view("v", "src_a", "a")
        binder = _binder([SRC, view], [])
        analysis = analyse_view(view["definition"])
        _members, rows, _gaps = binder.bind(view, analysis, "view_definition")

        reasons = binder.view_cross_check(
            view, analysis, rows, {("app", "src", "a"), ("app", "src", "k")})
        assert reasons == ["catalog input column app.src.k is read by no parsed transform"]


# -- A1: the chain across objects ------------------------------------------------

def _published_edges(rows: list[dict[str, Any]]) -> set[tuple[tuple, tuple]]:
    """Exact derivation edges, oriented as publication stores them (input -> transform)."""
    edges = set()
    for r in rows:
        if r["resolution"] != "exact" or r["reference_kind"] not in (
                "derivation_input", "derivation_output"):
            continue
        source = (r["from_object_name"], r["from_member_name"])
        target = (r["to_object_name"], r["to_member_name"])
        edges.add((target, source) if r["reference_kind"] == "derivation_input"
                  else (source, target))
    return edges


def _walk(edges: set[tuple[tuple, tuple]], start: tuple, *, reverse: bool) -> set[tuple]:
    """Everything reachable from ``start`` -- f_db_reachable over two edge kinds."""
    seen, frontier = set(), [start]
    while frontier:
        node = frontier.pop()
        for source, target in edges:
            near, far = (target, source) if reverse else (source, target)
            if near == node and far not in seen:
                seen.add(far)
                frontier.append(far)
    return seen


def test_a1_a_final_column_derives_from_source_columns_across_three_objects() -> None:
    """src.a, dim.m -> p (multiply) -> stage.x -> v1 (sum) -> v1.total -> v2 (round) -> final."""
    procedure = _routine("app", "p", _sql("a1_procedure"))
    v1 = _view("v1", "a1_v1", "k", "total")
    v2 = _view("v2", "a1_v2", "final")
    binder = _binder([SRC, DIM, STAGE, v1, v2], [procedure])
    rows: list[dict[str, Any]] = []
    for view in (v1, v2):
        _m, found, gaps = binder.bind(view, analyse_view(view["definition"]), "view_definition")
        assert gaps == 0
        rows += found
    _m, found, gaps = binder.bind(procedure, analyse_routine(procedure["definition"], "plpgsql"),
                                  "routine_definition", procedure["parameters"])
    assert gaps == 0
    rows += found
    edges = _published_edges(rows)

    upstream = _walk(edges, ("v2", "final"), reverse=True)
    assert {node for node in upstream if not node[1][:1].isdigit()} == {
        ("v1", "total"), ("stage", "x"), ("src", "a"), ("dim", "m")}
    # Each hop passes through its transform: final <- round <- total <- sum <- x <- a * m.
    assert {("v2", "1.q.value.1.final"), ("v1", "1.q.value.2.total"),
            ("p", "1.q.value.1.x")} <= upstream
    # The join shaping p's rows is one 'reference' from the value transform.
    assert any(r["reference_kind"] == "reference" and r["from_object_name"] == "p"
               and r["to_member_name"] == "1.q.join.1.join" for r in rows)
    # Forward from a source column to the final column.
    assert ("v2", "final") in _walk(edges, ("src", "a"), reverse=False)
