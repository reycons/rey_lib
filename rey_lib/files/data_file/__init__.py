"""DataFile registry: one module per format, each registering itself.

Modelled on ``file_operator/layouts/``, which solved this problem properly
already: a decorator registers, and the package auto-discovers its submodules
so a new format needs **no change to any existing module**. A hand-maintained
``{name: factory}`` dict would be a central list someone must remember to
edit -- a milder form of the central branch this hierarchy exists to remove.

Public API
----------
data_file           Decorator registering a subtype under one or more tokens.
data_file_for       Build the DataFile for a path, by declared type or suffix.
registered_formats  The tokens that resolve to a subtype.
DataFile            The contract.

A structural failure is NOT re-exported here. It is
``rey_lib.data.errors.DataStructureError`` -- a data object's structure being
wrong is not a fact about files, and a convenience alias in this package would
put the file family back in front of it.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable

from rey_lib.files.data_file.base import DEFAULT_ENCODING, DataFile
from rey_lib.files.file_utils import file_type_for_suffix

__all__ = [
    "DataFile",
    "data_file",
    "data_file_for",
    "registered_formats",
]

_REGISTRY: dict[str, type[DataFile]] = {}
_DISCOVERED = False


def data_file(*file_types: str) -> Callable[[type[DataFile]], type[DataFile]]:
    """Register the decorated class as the subtype for these format tokens.

    Args:
        file_types: One or more tokens, matched case-insensitively. A format
            with aliases -- JSONL and NDJSON are one format under two names --
            registers both here rather than normalising them somewhere else.

    Returns:
        The decorator, which registers and returns the class unchanged.
    """
    def _decorator(subtype: type[DataFile]) -> type[DataFile]:
        for token in file_types:
            _REGISTRY[token.strip().upper()] = subtype
        return subtype

    return _decorator


def registered_formats() -> list[str]:
    """Return the sorted tokens that resolve to a subtype."""
    _discover()
    return sorted(_REGISTRY)


def data_file_for(
    path: Path | str,
    *,
    file_type: str = "",
    encoding: str = DEFAULT_ENCODING,
    **settings: Any,
) -> DataFile:
    """Return the DataFile for this path.

    A declared ``file_type`` wins. Where none is declared the suffix decides,
    because a caller naming one file has already said what it is by naming it.

    Args:
        path: The file.
        file_type: The declared format token, where configuration declares one.
        encoding: How to decode it.
        settings: Format-specific settings passed to the subtype.

    Returns:
        The subtype registered for that format.

    Raises:
        ValueError: If the format is unknown, or if no type was declared and
            the suffix does not name one. Refused by name rather than guessed
            at: a load that silently picked the wrong reader would fail later
            and somewhere else.
    """
    _discover()
    source = Path(path)
    token = (file_type or file_type_for_suffix(source.suffix)).strip().upper()

    if not token:
        raise ValueError(
            f"Cannot tell what kind of file '{source.name}' is: no file_type "
            f"was declared and '{source.suffix}' names no known format. "
            f"Known formats: {sorted(_REGISTRY)}."
        )
    if token not in _REGISTRY:
        raise ValueError(
            f"Unsupported file_type '{token}'. "
            f"Known formats: {sorted(_REGISTRY)}."
        )

    return _REGISTRY[token](source, encoding=encoding, **settings)


def _discover() -> None:
    """Import every submodule once so each subtype self-registers.

    Lazy, on first lookup, so this package finishes importing before
    submodules that import names from it are loaded.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    for module in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if module.name != "base":
            importlib.import_module(f"{__name__}.{module.name}")
