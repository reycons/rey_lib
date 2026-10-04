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
from typing import Any, Callable, Mapping

from rey_lib.files.data_file.base import DEFAULT_ENCODING, DataFile
from rey_lib.files.data_file.untyped import UntypedFile
from rey_lib.files.file_utils import file_type_for_suffix
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "DataFile",
    "UntypedFile",
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
    file_manifest_id: int | None = None,
    file_mutation_id: int | None = None,
    classification: Mapping[str, Any] | None = None,
    base_path: str | None = None,
    inbox: str | None = None,
    original_path: str | None = None,
    original_mutation_id: int | None = None,
    record_type: str | None = None,
    conversion: Mapping[str, Any] | None = None,
    data_profile_key: str | None = None,
    **settings: Any,
) -> DataFile:
    """Return the DataFile for this path.

    A declared ``file_type`` wins. Where none is declared the suffix decides,
    because a caller naming one file has already said what it is by naming it.

    Args:
        path: The file.
        file_type: The declared format token, where configuration declares one.
        encoding: How to decode it.
        file_manifest_id: The governed identity, where the file is governed.
        file_mutation_id: The governed state the caller selected.
        classification: The governed classification at that state.
        base_path: The governed lifecycle root at that state.
        inbox: The directory containing the original file as inventoried.
        original_path: Where the original file physically is now.
        original_mutation_id: The original's current governed state.
        record_type: What the selected state's mutation records.
        conversion: The conversion that produced the selected state.
        data_profile_key: The governed file's profile group.
        settings: Format-specific settings passed to the subtype.

    Returns:
        The subtype registered for that format -- or, where no registered
        subtype claims it, an ``UntypedFile`` (backlog 624): every physical
        file is a DataFile, so every file can be moved, kicked out and
        governed. Reading one refuses by name, with the same message this
        function used to raise, so an unknown format still fails at the first
        operation that needs its content rather than being guessed at.
    """
    _discover()
    source = Path(path)
    token = (file_type or file_type_for_suffix(source.suffix)).strip().upper()

    facts = {
        "encoding": encoding,
        "file_manifest_id": file_manifest_id, "file_mutation_id": file_mutation_id,
        "classification": classification, "base_path": base_path,
        "inbox": inbox, "original_path": original_path,
        "original_mutation_id": original_mutation_id,
        "record_type": record_type, "conversion": conversion,
        "data_profile_key": data_profile_key,
    }
    if not token:
        return UntypedFile(source, refusal=(
            f"Cannot tell what kind of file '{source.name}' is: no file_type "
            f"was declared and '{source.suffix}' names no known format. "
            f"Known formats: {sorted(_REGISTRY)}."
        ), **facts, **settings)
    if token not in _REGISTRY:
        return UntypedFile(source, refusal=(
            f"Unsupported file_type '{token}'. "
            f"Known formats: {sorted(_REGISTRY)}."
        ), **facts, **settings)

    return _REGISTRY[token](source, **facts, **settings)


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
