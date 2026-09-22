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

__all__ = ["RoutineAnalysis", "analyse_routine", "ANALYZABLE_LANGUAGES"]

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

    return found.as_analysis()


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
