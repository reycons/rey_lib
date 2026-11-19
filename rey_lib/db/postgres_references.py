"""What a PostgreSQL routine body references, and what could not be established.

The database index stores a graph an agent traverses instead of reopening
stored-procedure bodies. PostgreSQL supplies none of it for a string-bodied
routine -- it records dependencies from externally visible properties and does
not inspect string literal bodies -- so this reads the body and says what it
found.

**The contract is honesty, not coverage.** Every reference either becomes an
observation or becomes a recorded gap:

    reference established        -> a read / write / call, as written
    construct seen, not static   -> a gap, and status partial
    body cannot be analyzed      -> status unparsed, no observations
    no analyzer for the language -> status unsupported

> **Every parser failure for an in-contract fragment must alter the object's
> evidence or status. No parser failure may be observationally invisible.**

That invariant is here because it is easy to break and invisible once broken.
The benchmark harness that chose this parser broke it on its first attempt: a
``RETURN app.f_one() + 1`` is stored as an *expression* rather than a statement,
``parse_sql`` raised, and an ``except: continue`` made the call disappear while
the routine still looked completely analyzed.

**Binding is not this module's business.** It reports names as they were
written. Whether ``customer`` is ``app.customer`` depends on ``search_path``,
which the index does not resolve, and deciding it here would let a name that is
merely unique in one snapshot impersonate PostgreSQL's own resolution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RoutineAnalysis", "analyse_routine", "analyse_view", "ANALYZABLE_LANGUAGES",
    "Derivation", "DerivationInput", "derivation_place", "expression_head",
]

#: Languages this module can read. A routine in any other language is
#: ``unsupported`` -- an honest statement that no analyzer applies, never a
#: silent empty result.
ANALYZABLE_LANGUAGES = frozenset({"sql", "plpgsql"})

#: How libpg_query marks a PL/pgSQL fragment. A fragment handed to the wrong
#: parser fails, and failing quietly is how a reference disappears -- so the
#: mode is read rather than guessed.
#:
#: 0 is an ordinary statement. 2 is an expression: ``RETURN a + 1`` is not
#: parseable as SQL alone and must be wrapped. 3, 4 and 5 are assignments with
#: one, two or three targets, whose text is ``target := expression`` -- the
#: reference lives in the right-hand side.
_STATEMENT_MODE = 0
_EXPRESSION_MODE = 2
_ASSIGNMENT_MODES = frozenset({3, 4, 5})


@dataclass
class RoutineAnalysis:
    """What one routine body was found to reference, and how completely.

    ``status`` answers *did analysis finish*, never *is the graph complete*.
    A routine may be analyzed perfectly and still name something this module
    declines to bind; that is the caller's concern and is recorded per
    reference, not here.
    """

    status: str
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    #: Why completeness could not be asserted. Non-empty exactly when the
    #: status is ``partial``.
    gaps: list[str] = field(default_factory=list)
    #: Columns In -> Transform -> Columns Out, one per transform, as the
    #: statements name them (#1425). Binding them to indexed members is the
    #: caller's, as binding relations is.
    derivations: list["Derivation"] = field(default_factory=list)
    #: Why a column derivation could not be established. Kept apart from
    #: ``gaps``, which -- with ``status`` -- answer for the READ/WRITE/CALL
    #: analysis exactly as before; the caller publishes both.
    derivation_gaps: list[str] = field(default_factory=list)
    #: The PL/pgSQL variables the body declares, parameters included. A name
    #: that binds to no column may be one of these, which says why it does not.
    variables: frozenset[str] = frozenset()
    #: Columns read only to ORDER the result. They shape no value and no row, so
    #: they are not a transform; they are kept because the catalog counts them
    #: among a view's input columns, and the two are compared.
    ordering: list["DerivationInput"] = field(default_factory=list)


def analyse_routine(definition: str, language: str) -> RoutineAnalysis:
    """Read one routine definition.

    Takes the **whole** ``CREATE FUNCTION``/``CREATE PROCEDURE`` statement, as
    ``pg_get_functiondef`` returns it, rather than the body alone. The
    signature is not decoration: PL/pgSQL is parsed against its declared return
    type, so a body lifted out and re-wrapped in a synthetic ``RETURNS void``
    makes an ordinary ``RETURN expr`` unparseable -- a routine that reads
    perfectly well would be reported ``unparsed``.

    Args:
        definition: The complete routine definition from the provider.
        language: The routine's language, lowercased by the caller.

    Returns:
        What it references, and how far analysis got. **This never raises for a
        body it cannot read** -- an unreadable body is a result, not an error,
        because one routine's syntax must not end an indexing run. A failure of
        the surrounding infrastructure is a different thing and does propagate.
    """
    if language not in ANALYZABLE_LANGUAGES:
        return RoutineAnalysis(status="unsupported")

    try:
        from pglast import parse_plpgsql, parse_sql
    except ImportError:
        # The analyzer is not installed. Saying so is not the same as saying
        # the routine has no references.
        return RoutineAnalysis(status="unsupported")

    found = _Found()
    if language == "sql":
        body = _sql_body(definition, parse_sql)
        if body is None:
            return RoutineAnalysis(status="unparsed")
        try:
            _collect(parse_sql(body), found)
        except Exception:
            return RoutineAnalysis(status="unparsed")
        _derive_sql_body(body, found)
        return found.as_analysis()

    tree = None
    for attempt in _definition_readings(definition):
        try:
            tree = parse_plpgsql(attempt)
            break
        except Exception:
            continue
    if tree is None:
        return RoutineAnalysis(status="unparsed")

    if "PLpgSQL_stmt_dynexecute" in json.dumps(tree):
        # A statement built at run time. Its target cannot be established
        # statically, and everything else found in this body still stands.
        found.gaps.append("dynamic EXECUTE: target not statically determinable")

    for fragment, mode in _fragments(tree):
        for attempt in _readings(fragment, mode):
            try:
                _collect(parse_sql(attempt), found)
                break
            except Exception:
                continue
        else:
            # Recorded, never skipped. This is the invariant.
            found.gaps.append(f"unparseable fragment: {fragment[:120]}")

    _derive_plpgsql(tree, found)
    return found.as_analysis()


def expression_head(expression: str) -> str:
    """What a transform's expression applies, as a short name (#1426's map labels).

    The expression is the one this analyser deparsed (``Derivation.expression``):
    a function call answers its function's name, a column reference the column
    it reads as written, a cast what it casts, COALESCE / CASE their keyword. Anything else
    -- or text that does not parse -- answers an empty string: the caller shows
    the transform's role alone, and the full expression stays where it is.
    """
    from pglast import ast as A
    from pglast import parse_sql
    from pglast.parser import ParseError

    try:
        tree = parse_sql(f"SELECT {expression}")
    except ParseError:
        return ""
    node = tree[0].stmt.targetList[0].val
    while isinstance(node, A.TypeCast):
        node = node.arg
    if isinstance(node, A.FuncCall):
        return str(node.funcname[-1].sval)
    if isinstance(node, A.ColumnRef) and all(isinstance(one, A.String) for one in node.fields):
        return ".".join(str(one.sval) for one in node.fields)
    if isinstance(node, A.CoalesceExpr):
        return "COALESCE"
    if isinstance(node, A.CaseExpr):
        return "CASE"
    return ""


def carries_forward(expression: str) -> bool:
    """Whether a value transform only carries a column forward (#1426): its
    expression, as this analyser deparsed it, is a bare column reference -- no
    function, cast, operator or literal. False for anything else, and for text
    that does not parse.
    """
    from pglast import ast as A
    from pglast import parse_sql
    from pglast.parser import ParseError

    if not expression.strip():
        return False
    try:
        tree = parse_sql(f"SELECT {expression}")
    except ParseError:
        return False
    node = tree[0].stmt.targetList[0].val
    return isinstance(node, A.ColumnRef) and all(isinstance(one, A.String)
                                                 for one in node.fields)


def derivation_place(key: str, role: str) -> tuple[str, str]:
    """Where a transform sits and what it produces, as its key states them (#1426).

    The key is ``<statement>.<query path>.<role>.<ordinal>.<name>`` (``Derivation``,
    built by ``_Deriver._transform``). Answers the innermost named query on the
    path -- a CTE's name, a FROM subquery's alias -- or "" for the statement's
    own query; and the name it produces. Both "" when the key is not in that form.
    """
    match = re.match(rf"^\d+\.(.*)\.{re.escape(role)}\.\d+\.(.*)$", key)
    if match is None:
        return "", ""
    path, name = match.groups()
    named = [segment.split(":", 1)[1] for segment in path.split(".")
             if segment.startswith(("cte:", "sub:"))]
    return (named[-1] if named else ""), name


def _sql_body(definition: str, parse_sql: Any) -> str | None:
    """The body of a SQL-language routine, taken from its own definition.

    ``None`` where the definition does not parse or carries no body, which the
    caller turns into ``unparsed`` rather than into an empty result.
    """
    from pglast import ast as A

    try:
        tree = parse_sql(definition)
    except Exception:
        return None
    for node in _walk(tree):
        if isinstance(node, A.CreateFunctionStmt):
            for option in node.options or ():
                if option.defname == "as" and option.arg:
                    return option.arg[0].sval
    return None


@dataclass
class _Found:
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    ctes: set[str] = field(default_factory=set)
    gaps: list[str] = field(default_factory=list)
    derivations: list["Derivation"] = field(default_factory=list)
    derivation_gaps: list[str] = field(default_factory=list)
    variables: frozenset[str] = frozenset()
    ordering: list["DerivationInput"] = field(default_factory=list)

    def as_analysis(self) -> RoutineAnalysis:
        """Settle reads against writes and CTEs, and state the status."""
        # A relation written is not also read merely because the statement
        # named it, and a CTE is a name inside the query rather than an object.
        reads = self.reads - self.writes - self.ctes
        return RoutineAnalysis(
            status="partial" if self.gaps else "complete",
            reads=reads,
            writes=set(self.writes),
            calls=set(self.calls),
            gaps=list(self.gaps),
            derivations=list(self.derivations),
            derivation_gaps=list(self.derivation_gaps),
            variables=self.variables,
            ordering=list(self.ordering),
        )


#: A routine returning a set of a composite from a schema libpg_query cannot
#: look up. Anchored on RETURNS, and only ever applied to the signature.
_QUALIFIED_SETOF_RETURN = re.compile(
    r"\bRETURNS\s+SETOF\s+[A-Za-z_][\w$]*\s*\.\s*[A-Za-z_][\w$]*",
    re.IGNORECASE,
)

#: Where the signature stops and the body starts. pg_get_functiondef emits the
#: body dollar-quoted; the single-quoted form is accepted because a definition
#: may arrive from somewhere else.
_BODY_DELIMITER = re.compile(r"\bAS\s+(\$[\w$]*\$|')", re.IGNORECASE)


def _definition_readings(definition: str) -> tuple[str, ...]:
    """The ways this whole definition might be parsed, best first.

    The definition as written is always tried first, so nothing that parses
    today takes a different route.

    THE SECOND READING EXISTS FOR ONE LIBPG_QUERY LIMIT. ``parse_plpgsql``
    resolves the routine's RETURN TYPE, and the library -- parsing outside a
    running server -- refuses any schema but ``pg_catalog`` and ``public``:

        Not implemented (LookupExplicitNamespace only supports pg_catalog
        and public)

    So ``RETURNS SETOF control.file_manifest`` cannot be read, while the body
    beneath it is perfectly ordinary. The return type is not something this
    module reports on -- it collects what a body READS, WRITES and CALLS -- so
    substituting a type the parser can resolve removes an obstacle without
    touching the answer. Only the RETURNS clause is rewritten; the body is
    never altered.

    ``SETOF record`` and not ``record``: plpgsql validates the body against the
    declared return, and a body using ``RETURN QUERY`` is rejected outright by
    a non-SETOF signature. Set-ness has to survive the substitution.

    Routines in ``LANGUAGE sql`` never hit this -- ``parse_sql`` does not
    resolve the return type -- which is why nine sibling routines returning the
    same composite parse today and the one converted to plpgsql does not.
    """
    # THE SIGNATURE ONLY. Split at the body delimiter and substitute in the head,
    # so a routine whose body happens to contain this text -- a literal that
    # builds DDL, say -- cannot have that literal rewritten. A first-match
    # substitution over the whole definition would reach it.
    body = _BODY_DELIMITER.search(definition)
    head, tail = ((definition[:body.start()], definition[body.start():])
                  if body else (definition, ""))
    if not _QUALIFIED_SETOF_RETURN.search(head):
        return (definition,)
    return (
        definition,
        _QUALIFIED_SETOF_RETURN.sub("RETURNS SETOF record", head, count=1) + tail,
    )


def _readings(fragment: str, mode: int | None) -> tuple[str, ...]:
    """The ways this fragment might be parsed, best first.

    An assignment's reference is in its right-hand side: ``v := f(x)`` calls
    ``f``, and the target is a variable rather than a database object. Handing
    the whole assignment to a statement parser fails, which is why 26 of this
    estate's 65 routines were first reported ``partial`` -- honestly, but
    needlessly.

    The bare fragment is always tried last, so a mode this function does not
    know still gets a chance before it is recorded as a gap.
    """
    if mode in _ASSIGNMENT_MODES:
        _, separator, right = fragment.partition(":=")
        if separator and right.strip():
            return (f"SELECT {right.strip()}", fragment)
    if mode == _EXPRESSION_MODE:
        return (f"SELECT {fragment}", fragment)
    return (fragment,)


def _fragments(tree: Any, out: list[tuple[str, int]] | None = None) -> list[tuple[str, int]]:
    """Every SQL fragment in a PL/pgSQL tree, with its parse mode.

    The mode matters: libpg_query marks expressions distinctly, and handing one
    to a statement parser fails. Losing that distinction is how a call
    disappears.
    """
    out = out if out is not None else []
    if isinstance(tree, dict):
        for key, value in tree.items():
            if key == "PLpgSQL_expr" and isinstance(value, dict) and "query" in value:
                out.append((value["query"], value.get("parseMode")))
            else:
                _fragments(value, out)
    elif isinstance(tree, list):
        for item in tree:
            _fragments(item, out)
    return out


def _collect(tree: Any, found: _Found) -> None:
    """Read one parsed statement into reads, writes, calls and CTE names."""
    from pglast import ast as A

    for node in _walk(tree):
        if isinstance(node, A.CommonTableExpr):
            found.ctes.add(node.ctename)
        elif isinstance(node, (A.InsertStmt, A.UpdateStmt, A.DeleteStmt)):
            if node.relation is not None:
                found.writes.add(_qualified(node.relation))
        elif isinstance(node, A.RangeVar):
            found.reads.add(_qualified(node))
        elif isinstance(node, A.CallStmt):
            if node.funccall is not None:
                found.calls.add(_dotted(node.funccall.funcname))
        elif isinstance(node, A.FuncCall):
            found.calls.add(_dotted(node.funcname))


def _walk(node: Any, seen: list[Any] | None = None) -> list[Any]:
    """Every node in a pglast tree."""
    from pglast import ast as A

    seen = seen if seen is not None else []
    if isinstance(node, A.Node):
        seen.append(node)
        for name in node.__slots__:
            _walk(getattr(node, name, None), seen)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, seen)
    return seen


def _qualified(relation: Any) -> str:
    """A relation as written -- qualified only where the author qualified it."""
    if relation.schemaname:
        return f"{relation.schemaname}.{relation.relname}"
    return relation.relname


def _dotted(funcname: Any) -> str:
    return ".".join(part.sval for part in funcname)


# -- column derivations (#1425) ------------------------------------------------
#
# Columns In -> Transform -> Columns Out, read from the same pglast trees the
# reference analysis walks. A transform is one expression with the columns it
# reads and where its result goes, or one row-shaping predicate (a join, a
# filter, a grouping) with the columns it reads. Names are reported as the
# statement writes them -- an alias resolved to the relation it stands for, an
# unqualified column given every relation it could come from -- and binding them
# to indexed members is the caller's, exactly as binding relation names is.
#
# The honesty contract holds at column level: what cannot be established is a
# gap, never a missing transform.

#: The roles a transform plays: what it does to values, or to rows.
VALUE, JOIN, FILTER, GROUP = "value", "join", "filter", "group"

#: Where a value transform's result goes.
#:   self         -- the analyzed object's own column at ``output_position`` (a view)
#:   relation     -- a column of ``output_relation`` (INSERT, UPDATE)
#:   return       -- the routine's output column at ``output_position``
#:   intermediate -- read only by other transforms (a CTE, a subquery)
SELF, RELATION, RETURN, INTERMEDIATE = "self", "relation", "return", "intermediate"


@dataclass(frozen=True)
class DerivationInput:
    """One value a transform reads, as the statement names it.

    ``candidates`` are where the name could come from, innermost query level
    first: ``("relation", "schema.name", level)``, ``("transforms", "k1|k2",
    level)`` for a CTE or subquery column (several keys where a set operation
    has several branches), or ``("unknown", alias, level)`` for a FROM item
    whose columns cannot be known. One candidate is a qualified reference;
    several are an unqualified one, settled by the binder against the catalog.
    ``position`` is set instead for a positional parameter (``$1``).
    """

    name: str
    observed: str
    candidates: tuple[tuple[str, str, int], ...] = ()
    position: int = 0
    #: An unqualified JOIN ... USING column: one value read from every side.
    merged: bool = False


@dataclass
class Derivation:
    """One transform: its expression, what it reads, and where its result goes.

    ``key`` is stable while the definition is unchanged:
    ``<statement>.<query path>.<role>.<ordinal>.<name>``.
    """

    key: str
    role: str
    statement_ordinal: int
    ordinal: int
    expression: str
    inputs: list[DerivationInput] = field(default_factory=list)
    output_kind: str = ""
    output_relation: str = ""
    output_column: str = ""
    output_position: int = 0
    #: The row-shaping transforms (join, filter, group) of the same query level.
    conditions: list[str] = field(default_factory=list)
    #: Function names the expression calls, as written.
    calls: set[str] = field(default_factory=set)


def analyse_view(definition: str) -> RoutineAnalysis:
    """Read one view's definition (``pg_get_viewdef``) into its derivations.

    A view is a query without procedural flow; its output columns are the
    view's own columns, by position. Its relation references come from the
    catalog, so ``reads`` stays empty here.

    Returns:
        ``complete`` with derivations, ``partial`` with the gaps that stopped a
        column being established, or ``unparsed`` where the text does not parse.
        Never raises for a definition it cannot read.
    """
    try:
        from pglast import parse_sql
        from pglast.parser import ParseError
    except ImportError:
        return RoutineAnalysis(status="unsupported")
    try:
        statements = parse_sql(definition)
    except ParseError:
        return RoutineAnalysis(status="unparsed")
    found = _Found()
    for ordinal, raw in enumerate(statements, start=1):
        _Deriver(ordinal, found).statement(raw.stmt, SELF)
    return found.as_analysis()


def _derive_sql_body(body: str, found: _Found) -> None:
    """A SQL-language routine's derivations: its last SELECT is what it returns."""
    from pglast import ast as A, parse_sql
    from pglast.parser import ParseError

    try:
        statements = parse_sql(body)
    except ParseError:
        # The reference pass parsed this same body; reaching here means the two
        # disagree, which must be said rather than shown as no derivations.
        found.derivation_gaps.append("derivation: body did not parse")
        return
    last = len(statements)
    for ordinal, raw in enumerate(statements, start=1):
        is_return = ordinal == last and isinstance(raw.stmt, A.SelectStmt)
        _Deriver(ordinal, found).statement(raw.stmt, RETURN if is_return else "")


def _derive_plpgsql(tree: Any, found: _Found) -> None:
    """A PL/pgSQL body's derivations: its SQL statements and RETURN QUERY.

    Only statements whose results land in columns are derived: INSERT and
    UPDATE into relations, RETURN QUERY into the routine's output columns. A
    SELECT INTO feeds a variable, which v1 does not trace; a later statement
    reading that variable records it as an input that binds to no column.
    """
    from pglast import parse_sql
    from pglast.parser import ParseError

    found.variables = frozenset(_declared_variables(tree))
    for ordinal, (query, context) in enumerate(_statement_fragments(tree), start=1):
        try:
            statements = parse_sql(query)
        except ParseError:
            # Already recorded as an unparseable fragment by the reference pass.
            continue
        for raw in statements:
            _Deriver(ordinal, found).statement(
                raw.stmt, RETURN if context == "return_query" else "")


def _declared_variables(tree: Any) -> list[str]:
    """Every variable a PL/pgSQL function declares, its parameters included."""
    names: list[str] = []
    for function in tree if isinstance(tree, list) else [tree]:
        for datum in (function.get("PLpgSQL_function") or {}).get("datums") or ():
            for value in datum.values():
                if isinstance(value, dict) and value.get("refname"):
                    names.append(str(value["refname"]))
    return names


def _statement_fragments(tree: Any, out: list[tuple[str, str]] | None = None,
                         ) -> list[tuple[str, str]]:
    """Each SQL statement in a PL/pgSQL tree, with what holds it.

    ``execsql`` for a statement run in place, ``return_query`` for RETURN QUERY.
    Expressions and assignments are not statements and are not derived.
    """
    out = out if out is not None else []
    if isinstance(tree, dict):
        for key, value in tree.items():
            held = {"PLpgSQL_stmt_execsql": ("sqlstmt", "execsql"),
                    "PLpgSQL_stmt_return_query": ("query", "return_query")}.get(key)
            if held and isinstance(value, dict):
                expr = (value.get(held[0]) or {}).get("PLpgSQL_expr") or {}
                if expr.get("query"):
                    out.append((expr["query"], held[1]))
            _statement_fragments(value, out)
    elif isinstance(tree, list):
        for item in tree:
            _statement_fragments(item, out)
    return out


@dataclass
class _Scope:
    """One query level's FROM items: what each alias stands for."""

    relations: dict[str, str] = field(default_factory=dict)
    derived: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)
    ctes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    #: alias -> "function as written#column aliases": a function in FROM, whose
    #: columns are its output parameters where the index holds it.
    functions: dict[str, str] = field(default_factory=dict)
    #: Columns a JOIN ... USING merged: unqualified, each names every side.
    merged: set[str] = field(default_factory=set)
    parent: "_Scope | None" = None
    level: int = 0

    def child(self) -> "_Scope":
        return _Scope(parent=self, level=self.level + 1)

    def cte(self, name: str) -> dict[str, list[str]] | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.ctes:
                return scope.ctes[name]
            scope = scope.parent
        return None


class _Deriver:
    """Derives one statement's transforms into ``found``."""

    def __init__(self, statement_ordinal: int, found: _Found) -> None:
        self._ordinal = statement_ordinal
        self._found = found
        # Subqueries are numbered in the order the statement is read, so a
        # transform's key is the same on every run over the same definition.
        self._subqueries = 0

    def _next_subquery(self) -> int:
        self._subqueries += 1
        return self._subqueries

    # -- statements -----------------------------------------------------------

    def statement(self, stmt: Any, sink: str) -> None:
        """Derive one statement. ``sink`` says where a top-level SELECT's results go."""
        from pglast import ast as A

        if isinstance(stmt, A.SelectStmt):
            if sink:
                self._select(stmt, "q", sink, _Scope(), "")
        elif isinstance(stmt, A.InsertStmt):
            self._insert(stmt)
        elif isinstance(stmt, A.UpdateStmt):
            self._update(stmt)
        elif type(stmt).__name__ == "MergeStmt":
            self._gap("MERGE: column derivations not analysed")

    def _insert(self, stmt: Any) -> None:
        target = _qualified(stmt.relation)
        columns = [one.name for one in stmt.cols or ()]
        scope = _Scope()
        produced: list[tuple[str, list[str]]] = []
        if stmt.selectStmt is not None:
            produced = self._select(stmt.selectStmt, "q", RELATION, scope, target,
                                    columns=columns)
        conflict = stmt.onConflictClause
        if conflict is not None and conflict.targetList:
            # ON CONFLICT DO UPDATE: EXCLUDED is the row the INSERT proposed.
            by_name = {name: keys for name, keys in produced if name}
            alias = stmt.relation.alias.aliasname if stmt.relation.alias else stmt.relation.relname
            upsert = _Scope(relations={alias: target},
                            derived={"excluded": by_name})
            conditions = self._predicate(conflict.whereClause, FILTER, "u", 1, upsert)
            for position, one in enumerate(conflict.targetList, start=1):
                self._value(one.val, "u", position, one.name, upsert,
                            conditions, RELATION, target, one.name)

    def _update(self, stmt: Any) -> None:
        from pglast import ast as A

        target = _qualified(stmt.relation)
        scope = _Scope()
        alias = stmt.relation.alias.aliasname if stmt.relation.alias else stmt.relation.relname
        scope.relations[alias] = target
        conditions: list[str] = []
        for item in stmt.fromClause or ():
            conditions += self._from(item, "q", scope)
        conditions += self._predicate(stmt.whereClause, FILTER, "q", 1, scope)
        for position, one in enumerate(stmt.targetList or (), start=1):
            if isinstance(one.val, A.MultiAssignRef):
                self._gap(f"UPDATE ... SET ({one.name}, ...) = (...): not analysed")
                continue
            self._value(one.val, "q", position, one.name, scope, conditions,
                        RELATION, target, one.name)

    # -- queries --------------------------------------------------------------

    def _select(self, stmt: Any, path: str, sink: str, parent: _Scope, target: str,
                columns: list[str] | None = None) -> list[tuple[str, list[str]]]:
        """Derive one SELECT; answer its output columns as (name, keys) by position."""
        from pglast.enums import SetOperation

        if stmt.op not in (None, SetOperation.SETOP_NONE):
            left = self._select(stmt.larg, path + ".l", sink, parent, target, columns)
            right = self._select(stmt.rarg, path + ".r", sink, parent, target, columns)
            return [(name, keys + other) for (name, keys), (_n, other) in zip(left, right)]
        # Every query is its own level: its FROM items are visible to the queries
        # nested in it and never to the query that contains it.
        scope = parent.child()
        recursive = bool(stmt.withClause and stmt.withClause.recursive)
        for cte in (stmt.withClause.ctes if stmt.withClause else ()):
            names = [one.sval for one in cte.aliascolnames or ()]
            cte_path = f"{path}.cte:{cte.ctename}"
            query = cte.ctequery
            if recursive and query.op not in (None, SetOperation.SETOP_NONE):
                # WITH RECURSIVE: the anchor states the columns; the recursive
                # term reads the CTE itself, so it is named before that term.
                anchor = self._select(query.larg, cte_path + ".l", INTERMEDIATE, scope, "")
                scope.ctes[cte.ctename] = _named(anchor, names)
                step = self._select(query.rarg, cte_path + ".r", INTERMEDIATE, scope, "")
                produced = [(name, keys + more)
                            for (name, keys), (_n, more) in zip(anchor, step)]
            else:
                produced = self._select(query, cte_path, INTERMEDIATE, scope, "")
            scope.ctes[cte.ctename] = _named(produced, names)
        conditions: list[str] = []
        for item in stmt.fromClause or ():
            conditions += self._from(item, path, scope)
        conditions += self._predicate(stmt.whereClause, FILTER, path, 1, scope)
        conditions += self._group(stmt, path, scope)
        conditions += self._predicate(stmt.havingClause, FILTER, path, 2, scope)
        for one in stmt.sortClause or ():
            self._found.ordering += self._parts(one, path, scope)[0]
        produced: list[tuple[str, list[str]]] = []
        if stmt.valuesLists:
            for row, values in enumerate(stmt.valuesLists, start=1):
                for position, value in enumerate(values, start=1):
                    name = _output_name(columns, position, None)
                    key = self._value(value, f"{path}.row{row}", position, name, scope,
                                      conditions, sink, target, name)
                    if row == 1:
                        produced.append((name, [key]))
                    else:
                        produced[position - 1][1].append(key)
            return produced
        for position, one in enumerate(stmt.targetList or (), start=1):
            name = _output_name(columns, position, one)
            if name == "*":
                self._gap(f"SELECT * at {path}: columns not established")
                produced.append(("", []))
                continue
            key = self._value(one.val, path, position, name, scope, conditions,
                              sink, target, name)
            produced.append((name, [key]))
        return produced

    def _from(self, item: Any, path: str, scope: _Scope) -> list[str]:
        """Bring one FROM item into scope; answer the join transforms it makes."""
        from pglast import ast as A

        if isinstance(item, A.RangeVar):
            alias = item.alias.aliasname if item.alias else item.relname
            columns = None if item.schemaname else scope.cte(item.relname)
            if columns is not None:
                scope.derived[alias] = columns
            else:
                scope.relations[alias] = _qualified(item)
            return []
        if isinstance(item, A.JoinExpr):
            made = self._from(item.larg, path, scope) + self._from(item.rarg, path, scope)
            joins = sum(1 for one in self._found.derivations if one.role == JOIN) + 1
            if item.quals is not None:
                made += self._predicate(item.quals, JOIN, path, joins, scope)
            elif item.usingClause:
                # Each name is a column of BOTH sides, so each side is named.
                names = [one.sval for one in item.usingClause]
                scope.merged.update(names)
                sides = [_alias(item.larg), _alias(item.rarg)]
                made.append(self._transform(
                    JOIN, path, joins, "join",
                    "USING (" + ", ".join(names) + ")",
                    [self._in(side, name, scope) for name in names for side in sides]))
            return made
        if isinstance(item, A.RangeSubselect):
            alias = item.alias.aliasname if item.alias else f"sub{self._next_subquery()}"
            produced = self._select(item.subquery, f"{path}.sub:{alias}",
                                    INTERMEDIATE, scope, "")
            renamed = [one.sval for one in (item.alias.colnames if item.alias else ()) or ()]
            scope.derived[alias] = _named(produced, renamed)
            return []
        alias = item.alias.aliasname if getattr(item, "alias", None) else type(item).__name__
        called = _single_function(item)
        if called:
            # A set-returning function: its columns are its output parameters,
            # renamed by the alias's column list where it gives one.
            renamed = [one.sval for one in (item.alias.colnames if item.alias else ()) or ()]
            scope.functions[alias if item.alias else called.rpartition(".")[2]] = \
                called + "#" + ",".join(renamed)
            return []
        # A row source whose columns the index does not state.
        scope.unknown.add(alias)
        return []

    def _group(self, stmt: Any, path: str, scope: _Scope) -> list[str]:
        if not stmt.groupClause:
            return []
        inputs, texts, calls = [], [], set()
        for one in stmt.groupClause:
            found = self._parts(one, path, scope)
            inputs += found[0]
            calls |= found[1]
            texts.append(_deparse(one, self))
        return [self._transform(GROUP, path, 1, "group", "GROUP BY " + ", ".join(texts),
                                inputs, calls=calls)]

    def _predicate(self, node: Any, role: str, path: str, ordinal: int,
                   scope: _Scope) -> list[str]:
        if node is None:
            return []
        inputs, calls = self._parts(node, path, scope)
        return [self._transform(role, path, ordinal, role, _deparse(node, self),
                                inputs, calls=calls)]

    def _value(self, node: Any, path: str, position: int, name: str, scope: _Scope,
               conditions: list[str], sink: str, target: str, column: str) -> str:
        inputs, calls = self._parts(node, path, scope)
        return self._transform(
            VALUE, path, position, name or f"column{position}", _deparse(node, self),
            inputs, conditions=conditions, calls=calls, output_kind=sink,
            output_relation=target if sink == RELATION else "",
            output_column=column if sink == RELATION else "",
            output_position=position if sink in (SELF, RETURN, RELATION) else 0)

    def _transform(self, role: str, path: str, ordinal: int, name: str, expression: str,
                   inputs: list[DerivationInput], *, conditions: list[str] | None = None,
                   calls: set[str] | None = None, output_kind: str = "",
                   output_relation: str = "", output_column: str = "",
                   output_position: int = 0) -> str:
        key = f"{self._ordinal}.{path}.{role}.{ordinal}.{name}"
        self._found.derivations.append(Derivation(
            key=key, role=role, statement_ordinal=self._ordinal, ordinal=ordinal,
            expression=expression, inputs=inputs, output_kind=output_kind,
            output_relation=output_relation, output_column=output_column,
            output_position=output_position, conditions=list(conditions or ()),
            calls=set(calls or ())))
        return key

    # -- expressions ----------------------------------------------------------

    def _parts(self, node: Any, path: str, scope: _Scope,
               ) -> tuple[list[DerivationInput], set[str]]:
        """The inputs and function calls of one expression.

        A subquery inside it is derived as intermediate transforms of its own,
        and the expression reads their results.
        """
        from pglast import ast as A

        inputs: list[DerivationInput] = []
        calls: set[str] = set()
        for one in _expression_nodes(node):
            if isinstance(one, A.ColumnRef):
                found = self._column(one, scope)
                if found is not None:
                    inputs.append(found)
            elif isinstance(one, A.ParamRef):
                inputs.append(DerivationInput(name=f"${one.number}",
                                              observed=f"${one.number}",
                                              position=int(one.number)))
            elif isinstance(one, A.FuncCall):
                calls.add(_dotted(one.funcname))
            elif isinstance(one, A.SubLink) and one.subselect is not None:
                produced = self._select(one.subselect,
                                        f"{path}.sublink{self._next_subquery()}",
                                        INTERMEDIATE, scope, "")
                keys = [key for _name, found_keys in produced[:1] for key in found_keys]
                if keys:
                    inputs.append(DerivationInput(
                        name="(subquery)", observed="(subquery)",
                        candidates=(("transforms", "|".join(keys), scope.level),)))
        return inputs, calls

    def _column(self, ref: Any, scope: _Scope) -> DerivationInput | None:
        from pglast import ast as A

        fields = [one.sval if isinstance(one, A.String) else "*" for one in ref.fields]
        observed = ".".join(fields)
        if "*" in fields:
            self._gap(f"{observed}: columns not established")
            return None
        name = fields[-1]
        if len(fields) == 1:
            row = self._whole_row(name, scope)
            return row if row is not None else self._unqualified(name, scope)
        qualifier = ".".join(fields[:-1])
        level: _Scope | None = scope
        while level is not None:
            if qualifier in level.derived:
                keys = level.derived[qualifier].get(name) or []
                return DerivationInput(name, observed, (
                    ("transforms", "|".join(keys), level.level),) if keys else ())
            if qualifier in level.relations:
                return DerivationInput(name, observed, (
                    ("relation", level.relations[qualifier], level.level),))
            if qualifier in level.functions:
                return DerivationInput(name, observed, (
                    ("function", level.functions[qualifier], level.level),))
            if qualifier in level.unknown:
                return DerivationInput(name, observed, (
                    ("unknown", qualifier, level.level),))
            level = level.parent
        # A qualifier naming nothing in FROM: a record variable's field, or the
        # relation written in full. The binder says which, or that it cannot.
        return DerivationInput(name, observed, (("relation", qualifier, scope.level),))

    def _whole_row(self, name: str, scope: _Scope) -> DerivationInput | None:
        """A FROM item's alias used as a value -- its whole row -- or None.

        ``to_jsonb(t)`` reads every column of ``t``: the input is the row, which
        the binder states as each of its columns.
        """
        level: _Scope | None = scope
        while level is not None:
            if name in level.relations:
                return DerivationInput("*", name, (("row", level.relations[name], level.level),))
            if name in level.derived:
                keys = [key for found in level.derived[name].values() for key in found]
                return DerivationInput("*", name, (
                    ("transforms", "|".join(keys), level.level),) if keys else ())
            level = level.parent
        return None

    def _in(self, alias: str, name: str, scope: _Scope) -> DerivationInput:
        """``name`` as a column of the FROM item ``alias``."""
        from pglast import ast as A

        return self._column(A.ColumnRef(fields=(A.String(sval=alias), A.String(sval=name))),
                            scope)

    def _unqualified(self, name: str, scope: _Scope) -> DerivationInput:
        candidates: list[tuple[str, str, int]] = []
        merged = False
        level: _Scope | None = scope
        while level is not None:
            merged = merged or (not candidates and name in level.merged)
            for relation in level.relations.values():
                candidates.append(("relation", relation, level.level))
            for columns in level.derived.values():
                if name in columns:
                    candidates.append(("transforms", "|".join(columns[name]), level.level))
            for function in level.functions.values():
                candidates.append(("function", function, level.level))
            for alias in level.unknown:
                candidates.append(("unknown", alias, level.level))
            level = level.parent
        return DerivationInput(name, name, tuple(candidates), merged=merged)

    def _gap(self, reason: str) -> None:
        self._found.derivation_gaps.append(f"derivation: {reason}")


def _expression_nodes(node: Any) -> list[Any]:
    """The nodes of one expression, without descending into its subqueries."""
    from pglast import ast as A

    seen: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, A.Node):
            seen.append(current)
            if isinstance(current, A.SubLink):
                # Its test expression belongs here; its query is its own scope.
                stack.append(current.testexpr)
                continue
            for name in current.__slots__:
                stack.append(getattr(current, name, None))
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return seen


def _named(produced: list[tuple[str, list[str]]], names: list[str],
           ) -> dict[str, list[str]]:
    """A query's output columns by name, renamed by an alias column list."""
    return {
        (names[at] if at < len(names) else name): keys
        for at, (name, keys) in enumerate(produced) if (names[at] if at < len(names) else name)
    }


def _single_function(item: Any) -> str:
    """The function a FROM item calls, as written, where it calls exactly one."""
    from pglast import ast as A

    if not isinstance(item, A.RangeFunction) or len(item.functions or ()) != 1:
        return ""
    call = item.functions[0][0] if isinstance(item.functions[0], (list, tuple)) \
        else item.functions[0]
    return _dotted(call.funcname) if isinstance(call, A.FuncCall) else ""


def _alias(item: Any) -> str:
    """The name a FROM item is known by in its query."""
    alias = getattr(item, "alias", None)
    if alias is not None:
        return alias.aliasname
    return getattr(item, "relname", "") or ""


def _output_name(columns: list[str] | None, position: int, target: Any) -> str:
    """The column a result lands in: the INSERT's column list, else the target's name."""
    from pglast import ast as A

    if columns:
        return columns[position - 1] if position <= len(columns) else ""
    if target is None:
        return ""
    if target.name:
        return target.name
    if isinstance(target.val, A.ColumnRef):
        last = target.val.fields[-1]
        return last.sval if isinstance(last, A.String) else "*"
    return "?column?"


def _deparse(node: Any, deriver: _Deriver) -> str:
    """An expression as SQL text, or the reason it could not be written back."""
    from pglast.stream import RawStream

    try:
        return RawStream()(node)
    except (NotImplementedError, ValueError) as exc:
        deriver._gap(f"expression not deparseable ({type(exc).__name__})")  # noqa: SLF001
        return f"({type(node).__name__})"
