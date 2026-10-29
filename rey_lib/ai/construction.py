"""The one place installation context is read for AI: its credential declarations.

``ctx`` is a bootstrap input, never AI runtime state. The application hosting the
live AI (ai_app) asks this for the credential resolver it constructs the AI with;
nothing in ``rey_lib.ai`` retains a reference to ``ctx``.

Credentials are not resolved here: the resolver is bound to the ``env``
declaration block and reads the environment at each point of use, so no
cleartext credential is carried out of this module.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.credentials import CredentialResolver
from rey_lib.config.env_reference import declaration_map

__all__ = ["credentials_from_ctx"]


def credentials_from_ctx(ctx: Any) -> CredentialResolver:
    """The credential resolver for this runtime.

    Takes the ``env`` declaration block -- plain configuration naming variables,
    holding no values -- and binds it. The environment itself is read later, at
    each point of use, so nothing resolved is carried out of this function.
    """
    return CredentialResolver(declaration_map(getattr(ctx, "env", None)))
