"""
Generic runtime context lookup helpers.

Provides helper functions for navigating lists of named Namespace objects
inside ctx. Works with any project — has no knowledge of any application's
specific config structure.

The pattern this module supports
---------------------------------
  ctx.connections[i].name           — look up by name
  ctx.data_sources[i].name          — look up by name
  ctx.loads[i].name / .version      — look up by name + version

Public API
----------
  find_by_name(items, name)
  find_by_name_version(items, name, version)
  find_in_ctx(ctx, section, name)
  find_in_ctx_versioned(ctx, section, name, version)
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "find_by_name",
    "find_by_name_version",
    "find_in_ctx",
    "find_in_ctx_versioned",
]


def find_by_name(items: list[Any], name: str) -> Any | None:
    """
    Return the first item in *items* whose .name attribute matches *name*.

    Parameters
    ----------
    items : list[Any]
        List of Namespace objects to search.
    name : str
        Name to match against item.name.

    Returns
    -------
    Any | None
        Matching item, or None if not found.
    """
    for item in items:
        if getattr(item, "name", None) == name:
            return item
    return None


def find_by_name_version(
    items: list[Any],
    name: str,
    version: str,
) -> Any | None:
    """
    Return the first item matching both *name* and *version*.

    Parameters
    ----------
    items : list[Any]
        List of Namespace objects to search.
    name : str
        Name to match.
    version : str
        Version to match.

    Returns
    -------
    Any | None
        Matching item, or None if not found.
    """
    for item in items:
        if (
            getattr(item, "name", None) == name
            and getattr(item, "version", None) == version
        ):
            return item
    return None


def find_in_ctx(ctx: Any, section: str, name: str) -> Any | None:
    """
    Look up a named item from a top-level list section of ctx.

    Equivalent to find_by_name(ctx.<section>, name) with a safe
    fallback when the section is missing from ctx.

    Parameters
    ----------
    ctx : Any
        Namespace context object.
    section : str
        Attribute name on ctx that holds the list (e.g. 'connections').
    name : str
        Name to look up.

    Returns
    -------
    Any | None
        Matching item, or None if the section or name is not found.

    Example
    -------
    conn = find_in_ctx(ctx, "connections", "clienta")
    """
    items = getattr(ctx, section, None)
    if not items:
        return None
    return find_by_name(items, name)


def find_in_ctx_versioned(
    ctx: Any,
    section: str,
    name: str,
    version: str,
) -> Any | None:
    """
    Look up a named + versioned item from a top-level list section of ctx.

    Parameters
    ----------
    ctx : Any
        Namespace context object.
    section : str
        Attribute name on ctx that holds the list.
    name : str
        Name to match.
    version : str
        Version to match.

    Returns
    -------
    Any | None
        Matching item, or None if not found.
    """
    items = getattr(ctx, section, None)
    if not items:
        return None
    return find_by_name_version(items, name, version)
