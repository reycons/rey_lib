"""
Layout handler registry for file_operator.

Each file-type handler lives in its own module in this package and registers
itself with the ``@layout("name")`` decorator. Adding support for a new file
type means adding a new module here — no existing code changes, because the
package auto-discovers and imports every submodule so registrations take
effect.

file_operator redacts files (it does not load them): handlers parse only as
much as redaction needs, preserving the original file structure. Raw file
reading is delegated to ``rey_lib.files``; parsing lives in the handlers.

Public API
----------
layout              Decorator that registers a handler under a layout name.
get_handler         Return the handler registered for a layout name, or None.
registered_layouts  Return the sorted list of registered layout names.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__: list[str] = ["layout", "get_handler", "registered_layouts", "LayoutHandler"]

# A handler runs the full redact → sample → write pipeline for one file.
LayoutHandler = Callable[[Any, Path, Path, Any], None]

_REGISTRY: dict[str, LayoutHandler] = {}
_DISCOVERED = False


def layout(name: str) -> Callable[[LayoutHandler], LayoutHandler]:
    """Register the decorated function as the handler for ``name``.

    Parameters
    ----------
    name : str
        Layout identifier (e.g. ``'delimited'``, ``'excel'``, ``'fixed_width'``).

    Returns
    -------
    Callable[[LayoutHandler], LayoutHandler]
        The decorator that registers and returns the handler unchanged.
    """
    def _decorator(func: LayoutHandler) -> LayoutHandler:
        _REGISTRY[name] = func
        return func

    return _decorator


def get_handler(name: str) -> LayoutHandler | None:
    """Return the registered handler for ``name``, or ``None`` if unknown.

    Parameters
    ----------
    name : str
        Layout identifier to look up.

    Returns
    -------
    LayoutHandler | None
        The handler, or ``None`` when no handler is registered for ``name``.
    """
    _discover()
    return _REGISTRY.get(name)


def registered_layouts() -> list[str]:
    """Return the sorted names of all registered layout handlers."""
    _discover()
    return sorted(_REGISTRY)


def _discover() -> None:
    """Import every submodule once so each handler self-registers.

    Discovery is lazy (on first lookup) so the package finishes importing
    before submodules — which import names from this package — are loaded.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    for module in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if module.name not in {"common"}:
            importlib.import_module(f"{__name__}.{module.name}")
