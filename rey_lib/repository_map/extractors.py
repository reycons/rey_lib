"""Language extractor registry and the public extraction entry points.

Contract: rey_repository_map_generator.sgc.yaml (INC-002A, REQ-020, REQ-030).

Adding a language is one registry entry and one extractor module. There is no
switch over language names anywhere in this package (ARCH-006): the two public
functions look the language up and delegate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rey_lib.repository_map.js_extractor import (
    extract_js_class_attributes,
    extract_js_parameters,
    extract_js_writes_and_accesses,
    extract_js_references,
    extract_js_symbols,
    supported_js_languages,
)
from rey_lib.repository_map.python_extractor import (
    extract_python_class_attributes,
    extract_python_parameters,
    extract_python_writes_and_accesses,
    extract_python_references,
    extract_python_symbols,
)
from rey_lib.repository_map.records import ReferenceEdge, SymbolInventory

__all__ = [
    "LANGUAGE_EXTRACTORS",
    "LanguageExtractor",
    "extract_class_attributes",
    "extract_declared_parameters",
    "extract_writes_and_accesses",
    "extract_executable_references",
    "extract_symbols",
    "supported_languages",
]


@dataclass(frozen=True)
class LanguageExtractor:
    """The two extraction behaviours one language owns.

    Attributes:
        language: Language name as recorded by the file inventory.
        symbols: Callable returning the file's top-level symbol inventory.
        references: Callable returning the file's executable reference edges.
        parameters: Callable returning the parameters its declarations declare.
        class_attributes: Callable returning the names bound directly in its
            class bodies.
        writes_and_accesses: Callable returning the writes and indexed access
            forms inside those declarations, from one walk.
    """

    language: str
    symbols: Callable[[Path, str, str | None], SymbolInventory]
    references: Callable[[Path, str, str | None], list[ReferenceEdge]]
    parameters: Callable[[Path, str, str | None], list[ParameterRecord]]
    class_attributes: Callable[[Path, str, str | None], list[ClassAttributeRecord]]
    writes_and_accesses: Callable[
        [Path, str, str | None],
        tuple[list[AssignmentRecord], list[AccessRecord]],
    ]


# The registry is data: language name to the object that owns that language.
# Adding a language is one entry plus its extractor module, never a branch.
LANGUAGE_EXTRACTORS: dict[str, LanguageExtractor] = {
    "Python": LanguageExtractor(
        language="Python",
        symbols=extract_python_symbols,
        references=extract_python_references,
        parameters=extract_python_parameters,
        class_attributes=extract_python_class_attributes,
        writes_and_accesses=extract_python_writes_and_accesses,
    ),
}

# JavaScript, TypeScript and TSX share one Tree-sitter-backed extractor and
# differ only by grammar, so they register from that extractor's own list.
LANGUAGE_EXTRACTORS.update(
    {
        language: LanguageExtractor(
            language=language,
            symbols=extract_js_symbols,
            references=extract_js_references,
            parameters=extract_js_parameters,
            class_attributes=extract_js_class_attributes,
            writes_and_accesses=extract_js_writes_and_accesses,
        )
        for language in supported_js_languages()
    }
)


def supported_languages() -> list[str]:
    """Return the languages that currently have an extractor, sorted."""
    return sorted(LANGUAGE_EXTRACTORS)


def extract_symbols(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> SymbolInventory:
    """Extract the top-level symbol inventory for one file (REQ-020).

    Args:
        path: File to read and parse.
        language: Language name from the file inventory.
        source_path: Path to record. Defaults to ``path`` in POSIX form.

    Returns:
        The file's symbol inventory.

    Raises:
        ValueError: If no extractor is registered for the language, or the
            file cannot be parsed.
    """
    return _extractor_for(language).symbols(path, language, source_path)


def extract_writes_and_accesses(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> tuple[list[AssignmentRecord], list[AccessRecord]]:
    """Return one file's syntactic writes and indexed access forms.

    Args:
        path: Source file to read and parse.
        language: Language name as the inventory recorded it.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The writes and the accesses, both empty for a language with no
        registered extractor.
    """
    extractor = LANGUAGE_EXTRACTORS.get(language)
    if extractor is None:
        return [], []
    return extractor.writes_and_accesses(path, language, source_path)


def extract_declared_parameters(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ParameterRecord]:
    """Return the parameters one file's declarations declare.

    Declared, not inferred: what the signature says, never what a caller may
    pass.

    Args:
        path: Source file to read and parse.
        language: Language name as the inventory recorded it.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The parameters, empty for a language with no registered extractor.
    """
    extractor = LANGUAGE_EXTRACTORS.get(language)
    if extractor is None:
        return []
    return extractor.parameters(path, language, source_path)


def extract_class_attributes(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ClassAttributeRecord]:
    """Return the names one file's class bodies bind directly.

    Syntax, never meaning: what the class body binds, not which of those a
    decorator elsewhere later turns into something else.

    Args:
        path: Source file to read and parse.
        language: Language name as the inventory recorded it.
        source_path: Path to record. Defaults to POSIX ``path``.

    Returns:
        The attributes, empty for a language with no registered extractor.
    """
    extractor = LANGUAGE_EXTRACTORS.get(language)
    if extractor is None:
        return []
    return extractor.class_attributes(path, language, source_path)


def extract_executable_references(
    path: Path,
    language: str,
    source_path: str | None = None,
) -> list[ReferenceEdge]:
    """Extract executable reference edges for one file (REQ-030).

    Args:
        path: File to read and parse.
        language: Language name from the file inventory.
        source_path: Path to record on each edge. Defaults to POSIX ``path``.

    Returns:
        The file's reference edges.

    Raises:
        ValueError: If no extractor is registered for the language, or the
            file cannot be parsed.
    """
    return _extractor_for(language).references(path, language, source_path)


def _extractor_for(language: str) -> LanguageExtractor:
    """Return the registered extractor for a language.

    Args:
        language: Language name from the file inventory.

    Returns:
        The registered extractor.

    Raises:
        ValueError: If the language has no registered extractor. An
            unsupported language is an explicit refusal, never a silent
            empty result that would read as 'this file references nothing'.
    """
    extractor = LANGUAGE_EXTRACTORS.get(language)
    if extractor is None:
        raise ValueError(
            f"No repository-map extractor registered for language '{language}'. "
            f"Registered languages: {', '.join(supported_languages()) or 'none'}."
        )
    return extractor
