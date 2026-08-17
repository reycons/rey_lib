"""The one place an ``env.<name>`` reference becomes a value.

Configuration names an environment variable; it does not hold what the variable
contains. The finalized context carries the name, and the subsystem that needs
the value asks for it here, at the moment it uses it. So a context can be
serialized, logged or handed to a caller without carrying anything resolved,
and a variable changed after startup is seen by the next call rather than the
next restart.

A reference is read one way only::

    "env.<declaration_name>"     as it stands in the finalized context
        -> ctx.env               the declaration block says which variable
        -> os.environ            read now, not at build time
        -> the literal           returned to the immediate caller only

The suffix is a *declaration name*, never the variable name itself. The two
coincide often enough to be mistaken for each other -- a declaration may well
read ``name: OPENAI_API_KEY, env_var: OPENAI_API_KEY`` -- but configuration is
free to name them differently, and the declaration block is what decides. The
nested ``env:`` spelling is normalized into a declaration during the build for
exactly this reason: so there is one block to consult and one way to read a
reference.

Nothing here writes. The resolved literal is returned and not stored, not
cached, and never put back into the context: a value that went back would be
the thing this module exists to prevent.

Public API
----------
is_env_reference(value)
    Whether a configured value names an environment variable.
resolve_env_reference(ctx, value)
    The current value of the variable a reference names.
declaration_map(entries)
    Declaration name -> environment variable name.
"""

from __future__ import annotations

import os
from typing import Any

from rey_lib.errors.error_utils import ConfigError

__all__ = ["ENV_REFERENCE_PREFIX", "declaration_map", "is_env_reference", "resolve_env_reference"]

#: How a configured value names an environment variable rather than holding it.
#: Uniform for every field: nothing here decides by name whether a value is a
#: secret, so a host and a password are read exactly the same way.
ENV_REFERENCE_PREFIX = "env."


def is_env_reference(value: Any) -> bool:
    """Return whether ``value`` names an environment variable.

    Parameters
    ----------
    value : Any
        A configured value, of any type.

    Returns
    -------
    bool
        True for a string beginning ``env.``; False for everything else,
        including non-strings.
    """
    return isinstance(value, str) and value.startswith(ENV_REFERENCE_PREFIX)


def declaration_map(entries: Any) -> dict[str, str]:
    """Build declaration name -> environment variable name from an env block.

    Accepts the block in either shape it takes during a context's life: plain
    dictionaries while the configuration is still raw, and Namespaces once it
    has been finalized. One reader for both is what keeps a reference meaning
    the same thing before and after the context is built.

    Parameters
    ----------
    entries : Any
        The top-level ``env`` block: a list of declarations, each carrying a
        ``name`` and an ``env_var``. Anything else yields an empty map.

    Returns
    -------
    dict[str, str]
        Declaration name to environment variable name. Incomplete declarations
        are skipped rather than half-recorded.
    """
    if not isinstance(entries, (list, tuple)):
        return {}

    declared: dict[str, str] = {}
    for entry in entries:
        name = str(_field(entry, "name") or "").strip()
        env_var = str(_field(entry, "env_var") or "").strip()
        if name and env_var:
            declared[name] = env_var
    return declared


def resolve_env_reference(ctx: Any, value: Any) -> Any:
    """Return what ``value`` names, reading the environment as it is now.

    Call this on the single field being used, immediately before using it --
    when the connection is opened, when the client is constructed. Resolving a
    whole configuration block, or resolving early and carrying the result
    around, puts the value back into circulation and is what this is here to
    avoid.

    Parameters
    ----------
    ctx : Any
        The finalized application context. Used only for its ``env``
        declaration block; it is never modified.
    value : Any
        A configured value. One that does not name a variable is returned
        exactly as it is, so a caller may pass any field without asking first.

    Returns
    -------
    Any
        The environment variable's current value for a reference; ``value``
        itself for anything else.

    Raises
    ------
    ConfigError
        If the reference names no declaration, or the declared variable is
        unset or empty. Both are reported here, where the operation being
        attempted is still known, rather than passed on as a credential that
        cannot work.
    """
    if not is_env_reference(value):
        return value

    name = value[len(ENV_REFERENCE_PREFIX):]
    declared = declaration_map(getattr(ctx, "env", None))
    if not declared:
        raise ConfigError(
            f"Cannot resolve '{value}': this context has no env declaration block. "
            "The block naming each environment variable must be in the app's "
            "config include path."
        )

    env_var = declared.get(name)
    if env_var is None:
        raise ConfigError(
            f"Unknown env reference '{value}' — no matching key name in top-level env block."
        )

    # Read now. Nothing is kept: the next call sees whatever the environment
    # says then.
    literal = os.environ.get(env_var)
    if not literal:
        raise ConfigError(
            f"Environment variable '{env_var}', named by '{value}', is not set. "
            "Set it in the installation's environment before running this operation."
        )
    return literal


def _field(entry: Any, name: str) -> Any:
    """Read one field from a declaration, mapping or object alike."""
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)
