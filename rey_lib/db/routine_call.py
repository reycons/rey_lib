"""One routine call, described the same way for every provider.

The procedure map resolves *which* routine and *what* to send it. This is what
that resolution becomes once, before any provider sees it: a name, a shape, and
named arguments.

Why it exists
-------------
Every provider utility used to reinterpret the procedure-map contract for
itself -- deciding its own statement form and building its own argument list --
so the same rule was written once per backend and could drift in one without
the others noticing. PostgreSQL ended up binding three call sites positionally
while SQL Server bound the same routines by name, which put values in the wrong
parameters silently and reported it as ``procedure ... does not exist``.

The shape is decided here, not rendered here. A backend that had to re-derive
scalar-versus-row-returning from ``result_mode`` would be reinterpreting the
contract again, one field smaller.

What a provider is allowed to know
----------------------------------
This object and nothing else. Not a binding, not a result mode, not the word
"map".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["InvocationShape", "RoutineCall"]


class InvocationShape(str, Enum):
    """How a routine is invoked, decided before rendering.

    The three forms a provider renders. ``PROCEDURE`` returns whatever its
    OUT parameters produce; the two function shapes differ in whether the
    result is one value or a set, which is a property of the routine and not
    of how a caller wants to read it.
    """

    PROCEDURE = "procedure"
    SCALAR_FUNCTION = "scalar_function"
    ROW_FUNCTION = "row_function"


@dataclass(frozen=True)
class RoutineCall:
    """A provider-neutral routine call.

    Attributes
    ----------
    routine : str
        Fully-qualified routine name, as configuration named it.
    shape : InvocationShape
        Already decided. A provider renders it and does not choose it.
    arguments : dict[str, Any]
        DB parameter name -> value. Names are the contract: a provider states
        them in its own dialect and never relies on their order.
    """

    routine: str
    shape: InvocationShape
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.routine or "").strip():
            raise ValueError("routine_call: a call must name a routine.")
        if not isinstance(self.shape, InvocationShape):
            raise ValueError(
                "routine_call: shape must be decided before rendering; "
                f"got {self.shape!r}."
            )
        for name in self.arguments:
            if not str(name or "").strip():
                raise ValueError(
                    f"routine_call: {self.routine} was given an unnamed "
                    "argument. Arguments are matched by name, so an unnamed "
                    "one has nowhere to go."
                )
