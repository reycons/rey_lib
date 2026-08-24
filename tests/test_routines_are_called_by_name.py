"""Routines are called by parameter name, never by position.

The procedure map binds a routine's parameter names to values. Those names are
the contract: they are why the map exists. A transport that passed the values
positionally reduced them to documentation -- the map's *order* silently became
load-bearing, so a binding listing the right names in the wrong order put every
value in the next parameter's slot, and a binding naming a parameter the routine
does not declare failed with "procedure does not exist" instead of saying which
name was wrong.

Both failures are invisible to every other test: the SQL is well formed, the
call succeeds, and the wrong column is written.

The rule is the connector's, not one server's. Every backend that calls a
routine states its parameter names in the dialect it has:

    postgres     CALL p(name => :name)      SELECT f(name => :name)
    sqlserver    EXEC p @name = ?

A backend that grows a routine call and binds it positionally is caught here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKENDS = Path(__file__).resolve().parents[1] / "rey_lib" / "db"


POSTGRES = BACKENDS / "postgres_utils.py"

#: How each backend names an argument in the dialect it speaks. A backend with
#: no routine calls is absent rather than exempt: the check finds callers by
#: reading the module, so one that grows a call arrives here unnamed and fails.
NAMED_FORMS = {
    "postgres_utils.py": r'f"\{key\}\s*=>\s*:\{key\}"',
    "sqlserver_utils.py": r'f"@\{name\} = \?"',
}


class TestNamedNotation:
    """What the transport may and may not send."""

    def test_the_renderer_states_argument_names(self) -> None:
        """`name => :name`, in the one place a routine call is written.

        There is a single renderer now -- the connector decides the invocation
        shape and hands it over -- so the rule is asserted against that, not
        counted across call sites.
        """
        source = POSTGRES.read_text(encoding="utf-8")
        assert "def render_and_execute(" in source, (
            "postgres_utils has no renderer; this guard has lost its subject"
        )
        assert re.search(r'f"\{key\}\s*=>\s*:\{key\}"', source), (
            "the renderer does not state argument names. The procedure map "
            "binds parameter names; a positional call makes the map's order "
            "load-bearing and its names decorative."
        )

    def test_the_exact_regression_cannot_return(self) -> None:
        """`", ".join(f":{key}" ...)` is what fed the positional CALL."""
        source = POSTGRES.read_text(encoding="utf-8")
        offender = re.search(r'join\(\s*f":\{[a-z_]+\}"', source)
        assert offender is None, (
            f"postgres_utils joins bare placeholders: {offender.group(0) if offender else ''}. "
            "Use '{key} => :{key}' so the routine is called by parameter name."
        )


class TestEveryBackend:
    """One rule, every connector that reaches a routine."""

    def test_each_backend_that_calls_a_routine_names_its_arguments(self) -> None:
        for path in sorted(BACKENDS.glob("*_utils.py")):
            source = path.read_text(encoding="utf-8")
            calls = "CALL " in source or "EXEC " in source
            if not calls:
                continue
            form = NAMED_FORMS.get(path.name)
            assert form is not None, (
                f"{path.name} calls a routine but this guard names no form for "
                "it. Add the dialect's named-argument form, or the backend is "
                "binding positionally."
            )
            assert re.search(form, source), (
                f"{path.name} calls a routine without naming its arguments. "
                "The procedure map binds parameter names; a positional call "
                "makes the map's order load-bearing and its names decorative."
            )


class TestTheGuardReadsRealCode:
    """A guard that stops matching its subject is worse than no guard."""

    def test_the_backend_module_still_defines_the_renderer(self) -> None:
        source = POSTGRES.read_text(encoding="utf-8")
        names = {
            node.name
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
        }
        assert "render_and_execute" in names, (
            "postgres_utils no longer defines render_and_execute; this guard is "
            "pointing at code that moved."
        )
