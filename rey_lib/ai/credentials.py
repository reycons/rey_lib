"""Resolving a configured credential, at the moment it is used.

The estate's rule, stated at `config/env_reference.py:103`: resolve *"the single
field being used, immediately before using it -- when the connection is opened,
when the client is constructed. Resolving a whole configuration block, or
resolving early and carrying the result around, puts the value back into
circulation."*

Two constraints meet here and only one shape satisfies both:

    the credential must not be resolved early or held    (estate rule)
    no ``rey_lib.ai`` object may retain ``ctx``          (this subsystem's rule)

``resolve_env_reference`` needs ctx, but only for its ``env`` declaration block --
and ``declaration_map`` builds that block into a plain mapping **without ctx**.
So the mapping is taken once at construction, and the environment is read at each
point of use:

    construction   declaration_map(ctx.env) -> this resolver
                   (configuration, not a secret, and not ctx)
    point of use   resolver(reference) -> reads os.environ now -> used -> gone

Nothing here caches. A resolved value is returned to the caller and this object
keeps no copy, so a rotated credential is picked up by the next call rather than
requiring a restart.

The mechanism lives here once. A provider adapter *declares* that it needs a
credential by holding a resolver and a reference; it does not implement
resolution, because three adapters each implementing it would be three places
for the rule to drift.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from rey_lib.ai.errors import AIConfigurationError

__all__ = ["CredentialResolver"]

#: The spelling a configured value uses to name an environment variable.
_REFERENCE_PREFIX = "env."


class CredentialResolver:
    """Turns a configured credential reference into its value, on demand.

    Args:
        declarations: Declaration name to environment variable name, as
            ``rey_lib.config.env_reference.declaration_map`` builds it. Plain
            configuration -- it holds no secret and no context.
    """

    __slots__ = ("_declarations",)

    def __init__(self, declarations: Mapping[str, str] | None = None) -> None:
        self._declarations = dict(declarations or {})

    def __call__(self, reference: Any) -> str:
        """What this reference names, read from the environment now.

        A value that does not name a variable is returned as it is, so a caller
        may pass any configured field without testing it first.

        Raises:
            AIConfigurationError: when the reference names a declaration this
                runtime does not declare, or a variable that is not set. Both
                are configuration faults and are reported where the value is
                used, which is the only place they can be known.
        """
        if not isinstance(reference, str) or not reference.startswith(_REFERENCE_PREFIX):
            return "" if reference is None else str(reference)

        name = reference[len(_REFERENCE_PREFIX):]
        variable = self._declarations.get(name)
        if not variable:
            raise AIConfigurationError(
                f"The credential reference '{reference}' names declaration "
                f"'{name}', which this runtime's env block does not declare."
            )
        value = os.environ.get(variable, "")
        if not value:
            raise AIConfigurationError(
                f"The credential reference '{reference}' resolves to environment "
                f"variable '{variable}', which is not set."
            )
        return value

    def declares(self, reference: Any) -> bool:
        """Whether this reference is one this resolver could resolve."""
        return (
            isinstance(reference, str)
            and reference.startswith(_REFERENCE_PREFIX)
            and bool(self._declarations.get(reference[len(_REFERENCE_PREFIX):]))
        )
