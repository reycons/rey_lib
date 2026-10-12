"""Semantic reference evidence from a language's own indexer (backlog 872).

Rey's extractors decide which facts exist: every symbol, every edge. A
language's semantic indexer -- SCIP's, here -- knows what a reference *names*,
which a syntactic extractor cannot. This module runs the pinned indexers over
one repository and keeps only what attaches to a fact Rey already has:

* **definition evidence** -- a definition of a declaration Rey records, with
  the fields Rey's symbol identity resolves by (file, qualified name, name
  token position) and the producer's symbol name;
* **reference evidence** -- the producer's symbol for the name that ends
  exactly where an extracted edge ends;
* **coverage** -- per producer, whether it ran, failed or does not apply.

Nothing here resolves anything. Publication correlates references with
definitions across every repository and resolves them to Rey ids; a symbol
name never survives as an identity.

**Positions.** Everything is converted to Rey's canonical position -- a
zero-based line and a UTF-8 byte offset -- before it is compared. A document
states its own encoding where it carries one; otherwise the measured encoding
of the exact pinned producer and version applies, and a producer with neither
yields no evidence at all and is recorded as failed. Nothing is inferred from
the text.

**Index-build only.** The indexers are the Node packages pinned in
``semantic_tools/``; the protobuf bindings are imported only when an index is
read. Nothing outside the repository-map scan reaches this module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.logs.app_logger import AppLogger
from rey_lib.repository_map.records import (
    DefinitionEvidence,
    ReferenceEdge,
    ReferenceEvidence,
    SemanticCoverage,
)

__all__ = [
    "MEASURED_POSITION_ENCODING",
    "PRODUCERS",
    "SemanticEvidence",
    "semantic_evidence",
]

logger = AppLogger(__name__)

#: Where the pinned indexers are declared and installed.
TOOLS = Path(__file__).with_name("semantic_tools")

#: How long one indexer run may take before its producer is recorded failed.
RUN_TIMEOUT_SECONDS = 1800

COVERAGE_RAN = "ran"
COVERAGE_FAILED = "failed"
COVERAGE_NOT_APPLICABLE = "not_applicable"

OCCURRENCE_LOCAL = "local"
OCCURRENCE_LOCAL_NO_DEFINITION = "local_no_definition"
OCCURRENCE_INDEXED = "indexed"
OCCURRENCE_EXTERNAL = "external"

#: SCIP SymbolRole.Definition.
_DEFINITION_ROLE = 0x1

#: The SCIP PositionEncoding values, by name, and the unit each counts.
_UTF8 = "UTF8CodeUnitOffsetFromLineStart"
_UTF16 = "UTF16CodeUnitOffsetFromLineStart"
_UTF32 = "UTF32CodeUnitOffsetFromLineStart"
_UNSPECIFIED = "UnspecifiedPositionEncoding"

#: The encoding each exact pinned producer emits, measured -- neither declares
#: one. Measured 2026-10-12 on a fixture with an emoji before the token, where
#: UTF-8, UTF-16 and UTF-32 offsets all differ: only UTF-16 slices out the
#: name, for both. A version change revalidates its entry before it is used
#: (tests/test_semantic_index.py fails until it is).
MEASURED_POSITION_ENCODING: dict[tuple[str, str], str] = {
    ("scip-python", "0.6.6"): _UTF16,
    ("scip-typescript", "0.4.0"): _UTF16,
}


@dataclass(frozen=True)
class Producer:
    """One pinned semantic indexer.

    Attributes:
        key: Its name, as coverage and evidence record it.
        package: Its npm package in ``semantic_tools/``.
        entry: Its executable, relative to the installed package.
        languages: The inventory languages it covers.
        standard_packages: The package names it gives the language's own
            library, as #866 observed them.
    """

    key: str
    package: str
    entry: str
    languages: frozenset[str]
    standard_packages: frozenset[str]


PRODUCERS: tuple[Producer, ...] = (
    Producer(
        key="scip-python",
        package="@sourcegraph/scip-python",
        entry="index.js",
        languages=frozenset({"Python"}),
        standard_packages=frozenset({"python-stdlib"}),
    ),
    Producer(
        key="scip-typescript",
        package="@sourcegraph/scip-typescript",
        entry="dist/src/main.js",
        languages=frozenset({"TypeScript", "TSX"}),
        standard_packages=frozenset({"typescript", "@types/node"}),
    ),
)


@dataclass
class SemanticEvidence:
    """One repository's semantic evidence.

    Attributes:
        coverage: One entry per producer.
        definitions: Definitions of declarations Rey records.
        references: References ending exactly where an extracted edge ends.
    """

    coverage: list[SemanticCoverage] = field(default_factory=list)
    definitions: list[DefinitionEvidence] = field(default_factory=list)
    references: list[ReferenceEvidence] = field(default_factory=list)


class _ProducerFailed(Exception):
    """A producer that applies could not yield evidence; the message says why."""


def semantic_evidence(
    repo_root: Path,
    file_languages: dict[str, str],
    edges: list[ReferenceEdge],
    import_roots: tuple[Path, ...] = (),
) -> SemanticEvidence:
    """Run every applicable producer over one repository.

    A producer that fails yields no evidence and a ``failed`` coverage entry;
    indexing carries on without it. Rey's own resolution is unaffected either
    way.

    Args:
        repo_root: The repository.
        file_languages: Each inventoried file Rey extracts facts from, by
            repository-relative path, with its language.
        edges: The extracted edges, carrying their end positions.
        import_roots: The source roots a producer may resolve imports
            against: the repositories of this publication, and nothing else.

    Returns:
        The evidence and the coverage that qualifies it.
    """
    edge_ends = {
        (edge.source_path, edge.end_line, edge.end_column)
        for edge in edges
        if edge.end_line is not None and edge.end_column is not None
    }
    evidence = SemanticEvidence()
    for producer in PRODUCERS:
        if not any(language in producer.languages for language in file_languages.values()):
            evidence.coverage.append(
                SemanticCoverage(producer=producer.key, status=COVERAGE_NOT_APPLICABLE)
            )
            continue
        version = ""
        try:
            version = _provisioned_version(producer)
            logger.info("%s: running %s %s", repo_root.name, producer.key, version)
            produced = _produce(
                producer, version, repo_root, file_languages, edge_ends, import_roots
            )
        except _ProducerFailed as failure:
            logger.warning("%s: %s failed: %s", repo_root.name, producer.key, failure)
            evidence.coverage.append(
                SemanticCoverage(
                    producer=producer.key,
                    producer_version=version,
                    status=COVERAGE_FAILED,
                    reason=str(failure),
                )
            )
            continue
        evidence.coverage.extend(produced.coverage)
        evidence.definitions.extend(produced.definitions)
        evidence.references.extend(produced.references)
    return evidence


def _provisioned_version(producer: Producer) -> str:
    """Return the version that will run, after proving it is the pinned one.

    The lockfile is the authority for which implementation runs; an installed
    package that differs from it, or none at all, fails the producer.

    Args:
        producer: The producer.

    Returns:
        Its pinned version.

    Raises:
        _ProducerFailed: Where the tool is not provisioned exactly as pinned.
    """
    lock_path = TOOLS / "package-lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        pinned = lock["packages"][f"node_modules/{producer.package}"]["version"]
    except (OSError, ValueError, KeyError) as exc:
        raise _ProducerFailed(f"pin_unreadable: {exc}") from exc
    installed_path = TOOLS / "node_modules" / producer.package / "package.json"
    try:
        installed = json.loads(installed_path.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError) as exc:
        raise _ProducerFailed("tool_not_installed") from exc
    if installed != pinned:
        raise _ProducerFailed(f"installed_{installed}_differs_from_pinned_{pinned}")
    if shutil.which("node") is None:
        raise _ProducerFailed("node_not_found")
    return str(pinned)


def _produce(
    producer: Producer,
    version: str,
    repo_root: Path,
    file_languages: dict[str, str],
    edge_ends: set[tuple[str, int, int]],
    import_roots: tuple[Path, ...],
) -> SemanticEvidence:
    """Run one producer over one repository and read what it says.

    Args:
        producer: The producer.
        version: Its pinned version.
        repo_root: The repository.
        file_languages: Inventoried files by path, with language.
        edge_ends: Every extracted edge's file and end position.
        import_roots: The publication's source roots.

    Returns:
        This producer's evidence, with its one coverage entry.

    Raises:
        _ProducerFailed: Where it cannot run, or its output cannot be read.
    """
    documents: list[tuple[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rey_semantic_") as scratch:
        for ordinal, project_dir in enumerate(_project_dirs(producer, repo_root, file_languages)):
            output = Path(scratch) / f"{ordinal}.scip"
            _run(producer, version, repo_root, project_dir, output, import_roots)
            index = _read_index(output)
            reported = (index.metadata.tool_info.name, index.metadata.tool_info.version)
            if reported != (producer.key, version):
                raise _ProducerFailed(f"producer_reports_{reported[0]}_{reported[1]}")
            prefix = project_dir.relative_to(repo_root).as_posix()
            for document in index.documents:
                path = (
                    document.relative_path if prefix == "."
                    else f"{prefix}/{document.relative_path}"
                )
                documents.append((path, document))

    return _read_documents(producer, version, repo_root, file_languages, edge_ends, documents)


def _project_dirs(
    producer: Producer, repo_root: Path, file_languages: dict[str, str]
) -> list[Path]:
    """Return the directories one producer is run in.

    Python is indexed from the repository root. TypeScript is indexed once per
    inventoried ``tsconfig.json``, because a TypeScript project is what its
    configuration says it is.

    Args:
        producer: The producer.
        repo_root: The repository.
        file_languages: Inventoried files by path.

    Returns:
        The project directories.

    Raises:
        _ProducerFailed: Where TypeScript files exist but no project declares them.
    """
    if producer.key != "scip-typescript":
        return [repo_root]
    configs = sorted(path for path in file_languages if Path(path).name == "tsconfig.json")
    if not configs:
        raise _ProducerFailed("no_tsconfig")
    return [(repo_root / config).parent for config in configs]


def _run(
    producer: Producer,
    version: str,
    repo_root: Path,
    project_dir: Path,
    output: Path,
    import_roots: tuple[Path, ...],
) -> None:
    """Run one pinned indexer in one project directory.

    scip-python names the project by the repository's own distribution name
    and version: that is how every other repository's installed copy names it,
    which is what lets independently produced symbol names line up across
    repositories. Name and version are inputs to the producer, never identity.

    Args:
        producer: The producer.
        version: Its pinned version.
        repo_root: The repository.
        project_dir: Where to run it.
        output: Where it writes its index.
        import_roots: The publication's source roots.

    Raises:
        _ProducerFailed: Where it cannot be started, fails or times out.
    """
    entry = TOOLS / "node_modules" / producer.package / producer.entry
    command = ["node", str(entry), "index", "--output", str(output)]
    if producer.key == "scip-python":
        name, project_version = _distribution(repo_root)
        command += ["--project-name", name, "--project-version", project_version]
    # scip-python resolves imports against the first python on PATH. That is
    # the interpreter running this scan, whatever shell launched it -- so the
    # repositories installed beside the indexer are the ones it resolves to.
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), environment.get("PATH", "")]
    )
    # pyright reads sys.path and runs no import hooks, so a repository
    # installed editable through a setuptools finder -- rey_lib, here -- is
    # invisible to it. The producer is given exactly this publication's
    # checkouts as its import path, replacing whatever the machine has: the
    # same local packages the development environment makes importable, and
    # no unrelated source tree. Only this subprocess sees it.
    environment["PYTHONPATH"] = os.pathsep.join(str(root) for root in import_roots)
    logger.debug("Running %s %s in %s", producer.key, version, project_dir)
    try:
        completed = subprocess.run(
            command,
            cwd=project_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ProducerFailed(f"timed_out_after_{RUN_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise _ProducerFailed(f"could_not_start: {exc}") from exc
    if completed.returncode != 0 or not output.exists():
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
        raise _ProducerFailed(f"exited_{completed.returncode}: {' '.join(tail)}")


def _distribution(repo_root: Path) -> tuple[str, str]:
    """Return the repository's distribution name and version.

    Args:
        repo_root: The repository.

    Returns:
        Name and version from its ``pyproject.toml``.

    Raises:
        _ProducerFailed: Where it declares neither.
    """
    try:
        project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(project["project"]["name"]), str(project["project"]["version"])
    except (OSError, ValueError, KeyError) as exc:
        raise _ProducerFailed("no_distribution_metadata") from exc


def _read_index(output: Path) -> Any:
    """Parse one SCIP index through the generated bindings.

    Imported here, not at module level: the bindings need the protobuf
    runtime, which only an index build installs.

    Args:
        output: The index file.

    Returns:
        The parsed ``scip.Index``.

    Raises:
        _ProducerFailed: Where protobuf is absent or the file does not parse.
    """
    try:
        from google.protobuf.message import DecodeError

        from rey_lib.repository_map import scip_pb2
    except ImportError as exc:
        raise _ProducerFailed("protobuf_not_installed") from exc
    index = scip_pb2.Index()
    try:
        index.ParseFromString(output.read_bytes())
    except (OSError, DecodeError) as exc:
        raise _ProducerFailed(f"index_unreadable: {type(exc).__name__}") from exc
    return index


def _read_documents(
    producer: Producer,
    version: str,
    repo_root: Path,
    file_languages: dict[str, str],
    edge_ends: set[tuple[str, int, int]],
    documents: list[tuple[str, Any]],
) -> SemanticEvidence:
    """Convert one producer's documents into evidence.

    Args:
        producer: The producer.
        version: Its pinned version.
        repo_root: The repository.
        file_languages: Inventoried files by path, with language.
        edge_ends: Every extracted edge's file and end position.
        documents: Each document with its repository-relative path.

    Returns:
        This producer's evidence, with its one coverage entry.

    Raises:
        _ProducerFailed: Where a document's position encoding is unknown.
    """
    from rey_lib.repository_map import scip_pb2

    read: list[tuple[str, list[tuple[Any, int, int, int, int]], set[str]]] = []
    encodings: set[str] = set()
    refused = 0
    defined: set[str] = set()
    for path, document in documents:
        if file_languages.get(path) not in producer.languages:
            continue
        declared = scip_pb2.PositionEncoding.Name(document.position_encoding)
        encoding = (
            declared if declared != _UNSPECIFIED
            else MEASURED_POSITION_ENCODING.get((producer.key, version))
        )
        if encoding is None:
            raise _ProducerFailed("unknown_position_encoding")
        encodings.add(encoding)
        lines = _source_lines(repo_root / path)
        converted: list[tuple[Any, int, int, int, int]] = []
        # A local symbol is only a local binding where this document defines
        # it. Without a definition here the producer named the occurrence and
        # bound it to nothing (scip-python does this for builtins it special-
        # cases, such as len and isinstance, and for genuinely undefined names).
        defined_locals = {
            occurrence.symbol for occurrence in document.occurrences
            if occurrence.symbol.startswith("local ")
            and occurrence.symbol_roles & _DEFINITION_ROLE
        }
        for occurrence in document.occurrences:
            position = _canonical_range(occurrence, lines, encoding)
            if position is None:
                refused += 1
                continue
            converted.append((occurrence, *position))
            if occurrence.symbol_roles & _DEFINITION_ROLE:
                defined.add(occurrence.symbol)
        read.append((path, converted, defined_locals))

    evidence = SemanticEvidence()
    occurrences = 0
    # A producer may report one occurrence twice -- scip-typescript 0.4.0 does,
    # for some library types -- and a repeat says nothing the first did not.
    # Each fact is kept once: a definition per (file, name position, symbol), a
    # reference per (file, end position, symbol).
    seen: set[tuple[str, str, int, int, str]] = set()
    for path, converted, defined_locals in read:
        for occurrence, start_line, start_column, end_line, end_column in converted:
            occurrences += 1
            symbol = occurrence.symbol
            if occurrence.symbol_roles & _DEFINITION_ROLE:
                qualified = None if symbol.startswith("local ") else _qualified_name(symbol)
                key = ("definition", path, start_line, start_column, symbol)
                if qualified is not None and key not in seen:
                    seen.add(key)
                    evidence.definitions.append(DefinitionEvidence(
                        source_path=path,
                        qualified_name=qualified,
                        name_line=start_line,
                        name_column=start_column,
                        symbol=symbol,
                        producer=producer.key,
                    ))
                continue
            if (path, end_line, end_column) not in edge_ends:
                continue
            key = ("reference", path, end_line, end_column, symbol)
            if key in seen:
                continue
            seen.add(key)
            package = _package(symbol)
            evidence.references.append(ReferenceEvidence(
                source_path=path,
                end_line=end_line,
                end_column=end_column,
                symbol=symbol,
                occurrence_kind=(
                    (OCCURRENCE_LOCAL if symbol in defined_locals else OCCURRENCE_LOCAL_NO_DEFINITION)
                    if symbol.startswith("local ")
                    else OCCURRENCE_INDEXED if symbol in defined
                    else OCCURRENCE_EXTERNAL
                ),
                package=package,
                is_standard_library=package in producer.standard_packages,
                producer=producer.key,
            ))
    evidence.coverage.append(SemanticCoverage(
        producer=producer.key,
        producer_version=version,
        status=COVERAGE_RAN,
        position_encoding=",".join(sorted(encodings)),
        documents=len(read),
        occurrences=occurrences,
        refused_occurrences=refused,
    ))
    return evidence


def _source_lines(path: Path) -> list[str]:
    """Return a file's lines, split at CR LF, CR and LF only.

    Args:
        path: A source file.

    Returns:
        Its lines; empty where it cannot be read as UTF-8.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _canonical_range(
    occurrence: Any, lines: list[str], encoding: str
) -> tuple[int, int, int, int] | None:
    """Return an occurrence's range in Rey's canonical position, or None.

    Supported: the ``range`` field in its two SCIP forms, three elements
    (single line) or four. The typed range fields and any other length are
    refused rather than reinterpreted, as is a position past its line.

    Args:
        occurrence: A ``scip.Occurrence``.
        lines: The document's source lines.
        encoding: The unit its columns count.

    Returns:
        Start line, start byte, end line, end byte -- or None.
    """
    if occurrence.HasField("single_line_range") or occurrence.HasField("multi_line_range"):
        return None
    values = list(occurrence.range)
    if len(values) == 3:
        start_line, start_unit, end_unit = values
        end_line = start_line
    elif len(values) == 4:
        start_line, start_unit, end_line, end_unit = values
    else:
        return None
    if not (0 <= start_line < len(lines) and 0 <= end_line < len(lines)):
        return None
    start = _byte_offset(lines[start_line], start_unit, encoding)
    end = _byte_offset(lines[end_line], end_unit, encoding)
    if start is None or end is None:
        return None
    return start_line, start, end_line, end


def _byte_offset(line: str, offset: int, encoding: str) -> int | None:
    """Return a column, counted in ``encoding``'s units, as a UTF-8 byte offset.

    Args:
        line: The source line.
        offset: The column in the producer's units.
        encoding: The SCIP PositionEncoding name.

    Returns:
        The UTF-8 byte offset, or None where the column is not on a character
        boundary within the line, or the encoding is not one of the three.
    """
    widths = {
        _UTF8: lambda character: len(character.encode("utf-8")),
        _UTF16: lambda character: 2 if ord(character) > 0xFFFF else 1,
        _UTF32: lambda character: 1,
    }
    width = widths.get(encoding)
    if width is None:
        return None
    units = 0
    for index, character in enumerate(line):
        if units == offset:
            return len(line[:index].encode("utf-8"))
        if units > offset:
            return None
        units += width(character)
    return len(line.encode("utf-8")) if units == offset else None


def _package(symbol: str) -> str:
    """Return the package a SCIP symbol is named under, or empty for a local.

    A global symbol is ``<scheme> <manager> <package> <version> <descriptors>``.

    Args:
        symbol: A SCIP symbol name.

    Returns:
        The package name.
    """
    parts = symbol.split(" ", 4)
    return parts[2] if len(parts) == 5 and not symbol.startswith("local ") else ""


def _qualified_name(symbol: str) -> str | None:
    """Return the qualified name Rey would give a SCIP definition, or None.

    The descriptors are read by the SCIP grammar. Namespaces (the module path)
    are dropped, since the defining file already says which module this is;
    types, terms and methods join with dots, as Rey qualifies a method by its
    class. Anything Rey does not record as a symbol answers None: a parameter,
    a type parameter, a meta or macro descriptor, and anything declared inside
    a callable.

    Args:
        symbol: A global SCIP symbol name.

    Returns:
        The qualified name, or None.
    """
    descriptors = _descriptors(symbol)
    if not descriptors:
        return None
    named = [(name, kind) for name, kind in descriptors if kind != "namespace"]
    if not named or any(kind not in ("type", "term", "method") for _name, kind in named):
        return None
    if any(kind == "method" for _name, kind in named[:-1]):
        return None
    return ".".join(name for name, _kind in named)


#: The descriptor suffixes that end a plain descriptor, and the kind each marks.
_SUFFIX_KINDS = {"/": "namespace", "#": "type", ".": "term", ":": "meta", "!": "macro"}


def _descriptors(symbol: str) -> list[tuple[str, str]] | None:
    """Parse a SCIP symbol's descriptors into (name, kind) pairs.

    Args:
        symbol: A global SCIP symbol name.

    Returns:
        The descriptors, or None where the symbol does not parse.
    """
    parts = symbol.split(" ", 4)
    if len(parts) != 5:
        return None
    text = parts[4]
    descriptors: list[tuple[str, str]] = []
    at = 0
    try:
        while at < len(text):
            if text[at] in "([":
                closing = ")" if text[at] == "(" else "]"
                name, at = _descriptor_name(text, at + 1)
                if text[at] != closing:
                    return None
                descriptors.append((name, "parameter" if closing == ")" else "type_parameter"))
                at += 1
                continue
            name, at = _descriptor_name(text, at)
            if text[at] == "(":
                close = text.index(")", at)
                if text[close + 1] != ".":
                    return None
                descriptors.append((name, "method"))
                at = close + 2
                continue
            kind = _SUFFIX_KINDS.get(text[at])
            if kind is None:
                return None
            descriptors.append((name, kind))
            at += 1
    except (IndexError, ValueError):
        return None
    return descriptors


def _descriptor_name(text: str, at: int) -> tuple[str, int]:
    """Read one descriptor name -- plain, or backtick-escaped -- at an offset.

    Args:
        text: The descriptor text.
        at: Where the name begins.

    Returns:
        The name and the offset just past it.

    Raises:
        ValueError: Where no name is there.
        IndexError: Where a backtick name is not closed.
    """
    if text[at] == "`":
        name: list[str] = []
        at += 1
        while True:
            if text[at] == "`":
                if at + 1 < len(text) and text[at + 1] == "`":
                    name.append("`")
                    at += 2
                    continue
                return "".join(name), at + 1
            name.append(text[at])
            at += 1
    end = at
    while end < len(text) and (text[end].isalnum() or text[end] in "_+-$"):
        end += 1
    if end == at:
        raise ValueError(f"no descriptor name at {at}")
    return text[at:end], end
