"""The synopsis maps the index; it never says what the map means.

These run without a database. What needs a live index -- that the seven
populations resolve, that exemplars round-trip -- is asserted against the real
server; what is asserted here is the part a wrong refactor would break
silently: which dimensions may be named, what a bounded answer admits to
omitting, and that structure never comes from the evidence string.
"""

from __future__ import annotations

import pytest

from rey_lib.repository_map.index_synopsis import (
    FACETS,
    DimensionFilter,
    FacetGroup,
    FacetResult,
    IndexFacet,
    SynopsisError,
    facet_keys,
    facets_hash,
    synopsis,
)


#: What the database publishes as each population's grouping vocabulary.
_VOCABULARY = {
    "symbol": ["repository", "relative_path", "language", "symbol_kind",
               "is_public"],
    "edge": ["source_repository", "relative_path", "edge_kind", "to_reference"],
    "concept": ["concept_key", "parent_concept_key", "root_concept_key"],
    "repository": ["repository_key", "branch", "working_tree_status"],
    "db_object": ["schema_name", "object_name", "object_type", "provider"],
    "db_member": ["schema_name", "object_name", "object_type", "member_kind"],
    "code_relation": ["relation_name", "relation_kind"],
}


class _Adapter:
    """Answers the vocabulary query; records every other query verbatim."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.queries: list[tuple[str, dict]] = []

    def execute_sql(self, _conn, sql, params, _mode):
        if "f_index_population_dimensions" in sql:
            return [{"population_kind": population, "dimension": dimension}
                    for population, names in _VOCABULARY.items()
                    for dimension in names]
        self.queries.append((sql, params))
        return self.rows


class TestTheVocabularyIsTheContract:
    """A dimension name means something only for its own population."""

    def test_every_declared_facet_groups_on_something_real(self) -> None:
        """THE GUARD ON THE REGISTRY ITSELF.

        A facet grouping on a dimension its population does not emit would put
        every row into one null bucket and read as a finding.
        """
        adapter = _Adapter()
        synopsis(adapter, object())

        assert len(adapter.queries) == len(FACETS)

    def test_a_filter_on_an_undeclared_dimension_is_REFUSED(self) -> None:
        """Refused, not silently matching nothing.

        `schema_name` is real -- for db_object, not for symbol. That is the
        whole point of population-local names.
        """
        with pytest.raises(SynopsisError) as raised:
            synopsis(_Adapter(), object(), "symbols_by_repository",
                     (DimensionFilter("schema_name", "code"),))

        assert "schema_name" in str(raised.value)
        assert "repository" in str(raised.value), "must say what IS declared"

    def test_a_filter_needs_a_facet(self) -> None:
        """A constraint applies to one population, so it needs one named."""
        with pytest.raises(SynopsisError) as raised:
            synopsis(_Adapter(), object(), "",
                     (DimensionFilter("repository", "rey_lib"),))

        assert "facet" in str(raised.value)

    def test_an_unknown_facet_lists_the_known_ones(self) -> None:
        with pytest.raises(SynopsisError) as raised:
            synopsis(_Adapter(), object(), "symbols_by_moon_phase")

        assert "symbols_by_repository" in str(raised.value)

    def test_the_same_name_may_mean_different_things(self) -> None:
        """`repository` on symbol, `repository_key` on repository.

        Unifying them would silently change what a facet counts, so both names
        stand and each is valid only for its own population.
        """
        assert "repository" in _VOCABULARY["symbol"]
        assert "repository" not in _VOCABULARY["repository"]
        assert "repository_key" in _VOCABULARY["repository"]


class TestBoundedOutputStaysHonest:
    """A bounded answer that hides its truncation is worse than no answer."""

    @staticmethod
    def _result(groups: tuple[FacetGroup, ...], total: int, group_count: int):
        return FacetResult(
            facet="symbols_by_repository", population="symbol",
            group_by=("repository",), filters=(),
            total_count=total, group_count=group_count, groups=groups,
        )

    def test_it_reports_what_it_left_out(self) -> None:
        """So 'absent from the list' can never be read as 'does not exist'."""
        shown = (FacetGroup(("rey_lib",), 5000, ()),
                 FacetGroup(("rey_console",), 4000, ()))
        result = self._result(shown, total=14263, group_count=11)

        assert result.shown_groups == 2
        assert result.omitted_groups == 9
        assert result.omitted_count == 14263 - 9000

    def test_the_arithmetic_closes(self) -> None:
        """shown + omitted must account for everything, or a reader is misled."""
        shown = (FacetGroup(("a",), 7, ()), FacetGroup(("b",), 3, ()))
        result = self._result(shown, total=10, group_count=2)

        assert result.shown_groups + result.omitted_groups == result.group_count
        assert sum(g.member_count for g in result.groups) + result.omitted_count \
            == result.total_count

    def test_nothing_omitted_reads_as_zero_not_absent(self) -> None:
        shown = (FacetGroup(("only",), 4, ()),)
        rendered = self._result(shown, total=4, group_count=1).as_dict()

        assert rendered["omitted_groups"] == 0
        assert rendered["omitted_count"] == 0

    def test_an_empty_population_is_reported_not_refused(self) -> None:
        """Zero is a valid state. A population may legitimately be empty."""
        rendered = self._result((), total=0, group_count=0).as_dict()

        assert rendered["total_count"] == 0
        assert rendered["groups"] == []


class TestStructureNeverComesFromMatchTarget:
    """match_target may be counted, sorted, or matched -- never parsed."""

    def test_no_grouping_key_is_derived_from_the_evidence_string(self) -> None:
        """The invariant, stated so it survives a refactor.

        Note what is NOT banned: match_target is ordered by and returned in the
        exemplar CTEs, and both are permitted uses. What must never happen is a
        grouping KEY derived from it by string surgery.
        """
        import re

        adapter = _Adapter()
        synopsis(adapter, object())

        for sql, _params in adapter.queries:
            for function in ("split_part", "substring", "position(", "left(",
                             "right(", "strpos"):
                assert function not in sql, \
                    f"{function} suggests structure taken from a string"

            definitions = re.findall(r"(\S+[^,]*?) AS g\d+", sql)
            assert definitions, "a facet must define its grouping keys"
            for definition in definitions:
                assert "dimensions ->>" in definition, \
                    f"grouping key {definition!r} does not read a dimension"

    def test_every_grouping_reads_a_named_dimension(self) -> None:
        adapter = _Adapter()
        synopsis(adapter, object(), "symbols_by_path")

        sql, _ = adapter.queries[0]
        assert "dimensions ->> 'repository'" in sql
        assert "dimensions ->> 'relative_path'" in sql


class TestValuesAreBoundNeverInterpolated:
    """Names come from a closed set the database published. Values never do."""

    def test_a_filter_value_is_a_parameter(self) -> None:
        adapter = _Adapter()
        synopsis(adapter, object(), "symbols_by_path",
                 (DimensionFilter("repository", "rey_lib"),))

        sql, params = adapter.queries[0]
        assert "rey_lib" not in sql, "a value must never reach the SQL text"
        assert "rey_lib" in params.values()

    def test_two_filters_bind_separately(self) -> None:
        """Multi-dimensional drill-down, without changing the model."""
        adapter = _Adapter()
        synopsis(adapter, object(), "symbols_by_path",
                 (DimensionFilter("repository", "rey_lib"),
                  DimensionFilter("language", "python")))

        sql, params = adapter.queries[0]
        assert sql.count("dimensions ->>") >= 2
        assert "rey_lib" in params.values() and "python" in params.values()


class TestEscapingHasOneOwner:
    """SQL owns the regex semantics, so Python must not escape anything."""

    def test_exact_reference_comes_from_the_database(self) -> None:
        adapter = _Adapter()
        synopsis(adapter, object(), "repositories")

        sql, _ = adapter.queries[0]
        assert "code.f_regex_literal(match_target)" in sql

    def test_the_module_contains_no_escaping_of_its_own(self) -> None:
        """A second algorithm would be free to disagree with the one evidence
        is matched by, and the disagreement would look like drift."""
        from pathlib import Path

        import rey_lib.repository_map.index_synopsis as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "re.escape" not in source
        assert "regexp_replace" not in source


class TestTheRegistryIsProvenance:
    """What was asked shapes what is reported, so the registry is recorded."""

    def test_the_hash_is_stable(self) -> None:
        assert facets_hash() == facets_hash()

    def test_reordering_does_not_read_as_a_change(self) -> None:
        """Sorted by key, so moving a tuple entry is not a false signal."""
        parts = sorted(f.key for f in FACETS)
        assert parts == sorted(facet_keys())

    def test_changing_a_facet_changes_the_hash(self) -> None:
        import rey_lib.repository_map.index_synopsis as module

        before = facets_hash()
        original = module.FACETS
        try:
            module.FACETS = original + (
                IndexFacet("extra", "repository", ("repository_key",)),
            )
            assert facets_hash() != before
        finally:
            module.FACETS = original

    def test_every_population_is_covered(self) -> None:
        """All seven, independent of whether anything would cite them.

        A census describes the whole index; evidence cites only what a
        conclusion needed. Conflating the two is what made an earlier draft's
        acceptance criterion impossible to satisfy.
        """
        assert {f.population for f in FACETS} == set(_VOCABULARY)
