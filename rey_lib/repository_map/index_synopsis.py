"""A deterministic, bounded, drillable map of the code index.

The index is too large to read whole -- 92,605 edges, 14,263 symbols, 3,096
database members. This compresses it into something a reader can take in at
once, and then drill into where it points.

**It adds no semantic authority.** It counts and groups facts already in the
index; it never concludes what they MEAN. That distinction is the whole reason
this module exists separately from anything that interprets capability: a
deterministic interpretation is still an interpretation, and a second authority
for meaning is exactly what the separation prevents. Nothing here decides that
a count of import edges amounts to "dependency discipline".

Everything comes from ``code.f_index_population()``, which is also what
capability evidence is measured against. One enumeration, two uses:

    match_target   the evidence-match VALUE    -> measurement
    dimensions     known grouping facts        -> this module

**Navigation never parses ``match_target``.** It may be counted, sorted,
returned as an exemplar or matched as evidence -- never split to recover
structure. Paths, qualified names and database identifiers all contain the
delimiters, and some dimensions are not in the string at all. Structure comes
from ``dimensions``, which the database supplies as facts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from rey_lib.errors.error_utils import AppError

__all__ = [
    "DimensionFilter", "IndexFacet", "FACETS", "FacetGroup", "FacetResult",
    "Synopsis", "SynopsisError", "facets_hash", "facet_keys", "synopsis",
]

#: The function every population comes from. Named once.
_POPULATION = "code.f_index_population()"


class SynopsisError(AppError):
    """A facet or filter that does not describe anything askable."""


@dataclass(frozen=True)
class DimensionFilter:
    """One constraint, naming the dimension it constrains.

    The dimension is REQUIRED because a bare value is ambiguous: ``rey_lib`` is
    a repository, a path component, and could later be a schema or a concept
    root. Naming it removes the guess and gives multi-dimensional drill-down
    without changing the model.
    """

    dimension: str
    value: str


@dataclass(frozen=True)
class IndexFacet:
    """One way of grouping one population.

    Attributes:
        key: How this facet is asked for.
        population: Which of the seven it groups.
        group_by: Dimension names, resolved against **that population's**
            declared vocabulary. Never a global dimension dictionary --
            ``symbol.repository`` and ``repository.repository_key`` are
            different names for related things and unifying them would silently
            change what a facet means.
        parent: The facet this drills down from, where it refines another.
        top: How many groups to return. Bounded by default so the whole
            synopsis stays ingestible; what is omitted is always reported.
        exemplars: Concrete identifiers per group, to drill into.
    """

    key: str
    population: str
    group_by: tuple[str, ...]
    parent: str | None = None
    top: int = 20
    exemplars: int = 3


#: Every facet, in the order a reader meets them. All seven populations appear,
#: independent of whether anything would ever cite them: a census describes the
#: whole index, while evidence cites only what a conclusion needed. Conflating
#: those two is what makes "all seven populations" a coherent requirement here
#: and an incoherent one for capability evidence.
FACETS: tuple[IndexFacet, ...] = (
    IndexFacet("repositories", "repository", ("repository_key",), top=50),
    IndexFacet("symbols_by_repository", "symbol", ("repository",), top=50),
    IndexFacet("symbols_by_path", "symbol", ("repository", "relative_path"),
               parent="symbols_by_repository"),
    IndexFacet("symbol_kinds", "symbol", ("symbol_kind",), top=50),
    IndexFacet("edge_kinds", "edge", ("edge_kind",), top=50),
    IndexFacet("edges_by_repository", "edge", ("source_repository", "edge_kind"),
               parent="edge_kinds", top=50),
    IndexFacet("database_schemas", "db_object", ("schema_name", "object_type"),
               top=50),
    IndexFacet("database_objects", "db_member", ("schema_name", "object_name"),
               parent="database_schemas"),
    IndexFacet("concepts_by_root", "concept", ("root_concept_key",), top=50),
    IndexFacet("code_relations", "code_relation", ("relation_kind",), top=50),
)


@dataclass(frozen=True)
class FacetGroup:
    """One group, with concrete identifiers to drill into.

    ``exemplars`` are the lexicographically first members. They are deterministic
    and therefore systematically one corner of a namespace -- they are
    *exemplars*, not a sample, and no statistical claim is made.

    Each carries both string forms because they are not interchangeable:
    ``match_target`` is the value, ``exact_reference`` is an anchored regex
    matching exactly that value. Matching exactly one VALUE is not matching
    exactly one row -- on ``edge`` a single target legitimately covers up to
    2,638 rows.
    """

    values: tuple[str | None, ...]
    member_count: int
    exemplars: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class FacetResult:
    """One facet, bounded, and honest about what it left out.

    ``group_count`` and ``omitted_*`` exist so that "absent from the list" can
    never be read as "does not exist". A bounded answer that hides its own
    truncation is worse than an unbounded one.
    """

    facet: str
    population: str
    group_by: tuple[str, ...]
    filters: tuple[DimensionFilter, ...]
    total_count: int
    group_count: int
    groups: tuple[FacetGroup, ...]

    @property
    def shown_groups(self) -> int:
        """How many groups this result actually carries."""
        return len(self.groups)

    @property
    def omitted_groups(self) -> int:
        """Groups that exist and are not shown."""
        return self.group_count - self.shown_groups

    @property
    def omitted_count(self) -> int:
        """Members living in the groups that are not shown."""
        return self.total_count - sum(g.member_count for g in self.groups)

    def as_dict(self) -> dict[str, Any]:
        """Render for a reader, with every bound stated."""
        return {
            "facet": self.facet,
            "population": self.population,
            "group_by": list(self.group_by),
            "filters": [{"dimension": f.dimension, "value": f.value}
                        for f in self.filters],
            "total_count": self.total_count,
            "group_count": self.group_count,
            "shown_groups": self.shown_groups,
            "omitted_groups": self.omitted_groups,
            "omitted_count": self.omitted_count,
            "groups": [
                {**dict(zip(self.group_by, g.values)),
                 "count": g.member_count,
                 "exemplars": [dict(e) for e in g.exemplars]}
                for g in self.groups
            ],
        }


@dataclass(frozen=True)
class Synopsis:
    """Every requested facet, with what produced it."""

    facets_hash: str
    results: tuple[FacetResult, ...]

    def as_dict(self) -> dict[str, Any]:
        """Render the whole synopsis."""
        return {
            "facets_hash": self.facets_hash,
            "facets": [r.as_dict() for r in self.results],
        }


def facet_keys() -> tuple[str, ...]:
    """Return every facet key, for a caller validating a request."""
    return tuple(f.key for f in FACETS)


def facets_hash() -> str:
    """Return a stable hash of the facet registry.

    What the synopsis reports depends on which facets were asked, so the
    registry is part of a result's provenance. Sorted by key, so reordering the
    tuple does not read as a change.
    """
    parts = sorted(
        f"{f.key}|{f.population}|{','.join(f.group_by)}|{f.parent or ''}"
        f"|{f.top}|{f.exemplars}"
        for f in FACETS
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def synopsis(
    adapter: Any,
    connection: Any,
    facet: str = "",
    filters: tuple[DimensionFilter, ...] = (),
) -> Synopsis:
    """Measure the index and return a bounded map of it.

    Args:
        adapter: The provider-dispatching adapter.
        connection: An open connection. Nothing is written through it.
        facet: One facet key, or empty for every facet. Filters require a
            named facet, because a filter is meaningful only against a
            population.
        filters: Dimension constraints, each naming its dimension.

    Returns:
        The requested facets, each bounded and reporting what it omitted.

    Raises:
        SynopsisError: An unknown facet, or a filter naming a dimension the
            population does not declare.
    """
    vocabulary = _vocabulary(adapter, connection)

    if facet:
        chosen = _facet(facet)
        _check_filters(chosen, filters, vocabulary)
        wanted: tuple[IndexFacet, ...] = (chosen,)
    elif filters:
        raise SynopsisError(
            "a filter constrains one population, so it needs a facet: name "
            f"one of {', '.join(facet_keys())}."
        )
    else:
        wanted = FACETS

    for one in wanted:
        _check_group_by(one, vocabulary)

    return Synopsis(
        facets_hash=facets_hash(),
        results=tuple(
            _measure(adapter, connection, one, filters) for one in wanted
        ),
    )


def _facet(key: str) -> IndexFacet:
    """Return the named facet, or refuse by name."""
    for candidate in FACETS:
        if candidate.key == key:
            return candidate
    raise SynopsisError(
        f"no facet '{key}'. Available: {', '.join(facet_keys())}."
    )


def _vocabulary(adapter: Any, connection: Any) -> dict[str, frozenset[str]]:
    """Return each population's declared dimension names.

    Read from the database rather than restated here, so the vocabulary has one
    author. A copy in Python would be a second declaration free to drift from
    the one the data is actually built against.
    """
    rows = adapter.execute_sql(
        connection,
        "SELECT population_kind, dimension "
        "FROM code.f_index_population_dimensions()",
        {},
        "dataset_result",
    )
    declared: dict[str, set[str]] = {}
    for row in rows:
        declared.setdefault(row["population_kind"], set()).add(row["dimension"])
    return {name: frozenset(values) for name, values in declared.items()}


def _check_group_by(facet: IndexFacet, vocabulary: dict[str, frozenset[str]]) -> None:
    """Refuse a facet grouping on something its population does not declare.

    A dimension the population never emits would group every row into one null
    bucket and look like a finding. Refusing names the mistake instead.
    """
    declared = vocabulary.get(facet.population)
    if declared is None:
        raise SynopsisError(
            f"facet '{facet.key}' names population '{facet.population}', which "
            "the index does not declare."
        )
    for dimension in facet.group_by:
        if dimension not in declared:
            raise SynopsisError(
                f"facet '{facet.key}' groups by '{dimension}', which "
                f"'{facet.population}' does not declare. It declares: "
                f"{', '.join(sorted(declared))}."
            )


def _check_filters(
    facet: IndexFacet,
    filters: tuple[DimensionFilter, ...],
    vocabulary: dict[str, frozenset[str]],
) -> None:
    """Refuse a filter on a dimension this population does not declare."""
    declared = vocabulary.get(facet.population, frozenset())
    for one in filters:
        if one.dimension not in declared:
            raise SynopsisError(
                f"cannot filter '{facet.population}' on '{one.dimension}'. It "
                f"declares: {', '.join(sorted(declared))}."
            )


def _measure(
    adapter: Any,
    connection: Any,
    facet: IndexFacet,
    filters: tuple[DimensionFilter, ...],
) -> FacetResult:
    """Run one facet and assemble its result.

    Filters arrive already checked against this facet's population --
    ``synopsis`` refuses them unless exactly one facet was named -- so they are
    applied as given.
    """
    sql, params = _facet_sql(facet, filters)
    rows = adapter.execute_sql(connection, sql, params, "dataset_result")

    if not rows:
        return FacetResult(
            facet=facet.key, population=facet.population,
            group_by=facet.group_by, filters=filters,
            total_count=0, group_count=0, groups=(),
        )

    groups = tuple(
        FacetGroup(
            values=tuple(row[f"g{position}"]
                         for position in range(len(facet.group_by))),
            member_count=int(row["member_count"]),
            exemplars=tuple(
                {"match_target": target, "exact_reference": reference}
                for target, reference in zip(row["exemplar_targets"] or [],
                                             row["exemplar_references"] or [])
            ),
        )
        for row in rows
    )
    return FacetResult(
        facet=facet.key, population=facet.population,
        group_by=facet.group_by, filters=filters,
        total_count=int(rows[0]["total_count"]),
        group_count=int(rows[0]["group_count"]),
        groups=groups,
    )


def _facet_sql(
    facet: IndexFacet,
    filters: tuple[DimensionFilter, ...],
) -> tuple[str, dict[str, Any]]:
    """Build one facet's query.

    **Dimension names are embedded; values never are.** A name has already been
    checked against the population's declared vocabulary, so it comes from a
    closed set the database itself published. Every value is bound as a
    parameter.

    ``exact_reference`` comes from ``code.f_regex_literal`` rather than being
    built here: SQL owns the regex semantics, so a second escaping algorithm in
    Python would be free to disagree with the one evidence is matched by.
    """
    params: dict[str, Any] = {
        "population": facet.population,
        "top": facet.top,
        "exemplars": facet.exemplars,
    }

    selected = ", ".join(
        f"p.dimensions ->> '{dimension}' AS g{position}"
        for position, dimension in enumerate(facet.group_by)
    )
    keys = ", ".join(f"g{position}" for position in range(len(facet.group_by)))
    ordering = ", ".join(
        f"g{position}" for position in range(len(facet.group_by))
    )

    where = ""
    if filters:
        conditions = []
        for position, one in enumerate(filters):
            params[f"filter{position}"] = one.value
            conditions.append(
                f"p.dimensions ->> '{one.dimension}' = :filter{position}"
            )
        where = " AND " + " AND ".join(conditions)

    matched = " AND ".join(
        f"e.g{position} IS NOT DISTINCT FROM s.g{position}"
        for position in range(len(facet.group_by))
    )

    sql = f"""
        WITH filtered AS (
            SELECT {selected}, p.match_target
              FROM {_POPULATION} p
             WHERE p.population_kind = :population{where}
        ), grouped AS (
            SELECT {keys}, count(*) AS member_count
              FROM filtered GROUP BY {keys}
        ), totals AS (
            SELECT count(*) AS group_count,
                   coalesce(sum(member_count), 0) AS total_count
              FROM grouped
        ), ranked AS (
            SELECT {keys}, member_count,
                   row_number() OVER (ORDER BY member_count DESC, {ordering})
                       AS position
              FROM grouped
        ), shown AS (
            SELECT {keys}, member_count, position
              FROM ranked WHERE position <= :top
        ), numbered AS (
            SELECT {keys}, match_target,
                   row_number() OVER (PARTITION BY {keys}
                                      ORDER BY match_target) AS ordinal
              FROM filtered
        ), exemplar AS (
            SELECT {keys},
                   array_agg(match_target ORDER BY match_target)
                       AS exemplar_targets,
                   array_agg(code.f_regex_literal(match_target)
                             ORDER BY match_target) AS exemplar_references
              FROM numbered WHERE ordinal <= :exemplars GROUP BY {keys}
        )
        SELECT s.{', s.'.join(f'g{n}' for n in range(len(facet.group_by)))},
               s.member_count, t.group_count, t.total_count,
               e.exemplar_targets, e.exemplar_references
          FROM shown s
         CROSS JOIN totals t
          LEFT JOIN exemplar e ON {matched}
         ORDER BY s.position
    """
    return sql, params
