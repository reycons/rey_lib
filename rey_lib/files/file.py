"""A physical file, and what kind of file it is (#1133).

``File`` is the root of the file domain. Every physical file is a File: it has
a path and an encoding, carries its governed identity when something governs
it, can be shown as bounded, display-safe text, and can be moved, kicked out
and governed whatever its format. What a file IS -- which format it is -- is
also answered here, once:

    File                     any physical file; an unrecognised one is just this
    ├── PythonFile, YamlFile, TextFile      recognised formats with no records
    └── DataFile             a format with record / data semantics
        └── DelimitedHeaderFile, ExcelDataFile, JsonFile, JsonlFile, ...

A subclass adds the semantics of its format; it never competes with File for
what belongs to every file. ``File.for_path`` is the one construction and
type-resolution path: a declared ``file_type`` wins, otherwise the suffix
decides, and a file neither names is a plain File rather than a pretend
DataFile.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from rey_lib.files import primitive_file_io
from rey_lib.files.file_utils import bounded_text_preview

__all__ = ["DEFAULT_ENCODING", "SECRET_NAME_PATTERNS", "File", "file_format"]

#: What the loader already defaults to, kept so nothing changes by moving.
DEFAULT_ENCODING = "utf-8-sig"

#: Filenames whose contents a File never shows as text: secret-like by name.
SECRET_NAME_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    "*.env",
    "*.pem",
    "*.key",
    "*secret*",
    "*credential*",
    "*credentials*",
)

#: Filename suffix -> the file_type token that names the format.
#:
#: Suffixes only, and only where the suffix genuinely names the format. A
#: caller that knows better declares a file_type and is believed. Every token
#: here is one a registered File subclass answers to.
_SUFFIX_FILE_TYPES: dict[str, str] = {
    ".csv":   "CSV",
    ".json":  "JSON",
    ".jsonl": "JSONL",
    ".ndjson": "NDJSON",
    ".xls":   "XLS",
    ".xlsx":  "XLSX",
    ".xlsb":  "XLSB",
    ".xlsm":  "XLSM",
    ".py":    "PYTHON",
    ".yaml":  "YAML",
    ".yml":   "YAML",
    ".txt":   "TEXT",
    ".text":  "TEXT",
}

#: Format token -> the File subclass that is that format.
_FORMATS: dict[str, type["File"]] = {}
_DISCOVERED = False


def file_format(*file_types: str) -> Callable[[type["File"]], type["File"]]:
    """Register the decorated File subclass as the format these tokens name.

    One module per format registers itself, so a new format needs no change to
    any existing module.

    Args:
        file_types: One or more tokens, matched case-insensitively. A format
            with aliases -- JSONL and NDJSON are one format under two names --
            registers both here rather than normalising them somewhere else.

    Returns:
        The decorator, which registers and returns the class unchanged.
    """
    def _decorator(subtype: type[File]) -> type[File]:
        for token in file_types:
            _FORMATS[token.strip().upper()] = subtype
        return subtype

    return _decorator


def _discover() -> None:
    """Import every format module once so each subclass self-registers.

    Lazy, on first resolution, so this module finishes importing before the
    format modules that import File from it are loaded.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    importlib.import_module("rey_lib.files.formats")
    package = importlib.import_module("rey_lib.files.data_file")
    for module in pkgutil.iter_modules(package.__path__):
        if module.name != "base":
            importlib.import_module(f"{package.__name__}.{module.name}")


class File:
    """One physical file.

    **A GOVERNED FILE CARRIES ITS IDENTITY** -- ``file_manifest_id``, the
    ``file_mutation_id`` of the state it was built from, ``classification`` and
    ``base_path`` -- as ``ManifestSource.data_file()`` supplies them. All four
    are None for a file nothing governs. They are facts handed in, never
    looked up: which state a file is opened at is the caller's selection.
    """

    def __init__(
        self,
        path: Path,
        *,
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
    ) -> None:
        """Hold the file, its format settings and, when governed, its identity.

        Args:
            path: The file.
            encoding: How to decode it. The loader's existing default.
            file_manifest_id: The governed file this is, or None for a file
                nothing governs.
            file_mutation_id: The governed state this object was built from --
                the mutation the caller selected, never one worked out here.
            classification: What the governed file was classified as, at that
                state.
            base_path: Where the governed file's lifecycle is rooted, at that
                state.
            inbox: The directory containing the ORIGINAL file as inventoried
                (backlog 624). Where its kickouts go: <inbox>/kickouts.
            original_path: Where the ORIGINAL file physically is now -- what a
                failure moves to kickouts, which may not be this file (a
                sanitized copy's original is the delivered file).
            original_mutation_id: The original's current governed state; None
                for a file nothing governs.
            record_type: What the selected state's mutation records (backlog
                630) -- a fact of THAT state, so a rebuilt file states its own.
            conversion: The conversion that produced the selected state, where
                one did; a fact of that state.
            data_profile_key: The governed file's profile group, as its
                classification named it -- the manifest's, so it carries over.
            settings: Format-specific settings a subclass declares -- delimiter,
                sheet, field widths. Held rather than interpreted here, so a
                file rebuilt at a new path keeps them.
        """
        self.path = Path(path)
        self.encoding = encoding
        self.file_manifest_id = file_manifest_id
        self.file_mutation_id = file_mutation_id
        self.classification = (
            dict(classification) if classification is not None else None
        )
        self.base_path = base_path
        self.inbox = inbox
        self.original_path = original_path
        self.original_mutation_id = original_mutation_id
        self.record_type = record_type
        self.conversion = dict(conversion) if conversion is not None else None
        self.data_profile_key = data_profile_key
        self.settings = settings

    # -- what kind of file ------------------------------------------------------

    @classmethod
    def for_path(cls, path: Path | str, *, file_type: str = "", **facts: Any) -> File:
        """The File this path is: the registered format's subclass, or a plain File.

        THE ONE CONSTRUCTION AND TYPE-RESOLUTION PATH. A declared ``file_type``
        wins; where none is declared the suffix decides, because a caller
        naming one file has already said what it is by naming it. A file whose
        type names no registered format is a plain File -- it can still be
        inventoried, moved, kicked out and governed.

        Args:
            path: The file.
            file_type: The declared format token, where configuration declares one.
            facts: The File facts -- encoding, the governed identity and the
                format settings -- passed to the subclass untouched.
        """
        _discover()
        source = Path(path)
        token = (file_type or File.type_for_suffix(source.suffix)).strip().upper()
        return _FORMATS.get(token, File)(source, **facts)

    @staticmethod
    def type_for_suffix(suffix: str) -> str:
        """The file_type a suffix names, or "" when it names none.

        Args:
            suffix: A filename suffix, with or without its dot, any case.

        Returns:
            A registered format token, or an empty string. Empty is an answer
            -- "this suffix says nothing" -- and callers refuse on it rather
            than guessing a default.
        """
        key = str(suffix or "").strip().lower()
        if key and not key.startswith("."):
            key = f".{key}"
        return _SUFFIX_FILE_TYPES.get(key, "")

    @classmethod
    def formats(cls) -> dict[str, type[File]]:
        """The registered format tokens whose subclass is this class or below it.

        ``File.formats()`` is every recognised format; ``DataFile.formats()``
        only those with record semantics -- one registry, asked by its class.
        """
        _discover()
        return {token: subtype for token, subtype in _FORMATS.items() if issubclass(subtype, cls)}

    @property
    def file_type(self) -> str:
        """This file's format token: empty for a file no format claims."""
        return ""

    # -- what every file has ----------------------------------------------------

    def governed_facts(self) -> dict[str, Any]:
        """This file's governed facts, as ``File.for_path`` takes them.

        For building the File an operation leaves behind: the same governed
        file at a new path and state, so a caller passes these and states only
        the new ``file_mutation_id``. One list, so a new fact is carried by
        every rebuild rather than by the ones somebody remembered.
        """
        return {
            "file_manifest_id": self.file_manifest_id,
            "file_mutation_id": self.file_mutation_id,
            "classification": self.classification,
            "base_path": self.base_path,
            "inbox": self.inbox,
            "original_path": self.original_path,
            "original_mutation_id": self.original_mutation_id,
            "data_profile_key": self.data_profile_key,
        }

    def template_context(self) -> dict[str, Any]:
        """The fields a configured template or source field may name, from this
        file's own facts (backlog 630).

        ``<file_name>`` and ``<base_name>`` are the WORKING names -- this
        path's last segment, and that without its last extension, by the rule
        the selectors applied (not ``Path.stem``, which keeps a leading-dot
        name whole). ``<base_path>`` and ``<classification.*>`` are the
        governed classification at this state.
        """
        path = str(self.path)
        file_name = re.sub(r"^.*/", "", path)
        return {
            "file_manifest_id": self.file_manifest_id,
            "file_mutation_id": self.file_mutation_id,
            "path": path,
            "file_name": file_name,
            "base_name": re.sub(r"\.[^.]*$", "", file_name),
            "base_path": self.base_path,
            "classification": self.classification,
            "record_type": self.record_type,
            "conversion": self.conversion,
        }

    def text(self, max_bytes: int) -> dict[str, Any]:
        """This file's text, for LOOKING AT, bounded and display-safe.

        Not format-dependent, so every File answers it. Whether it may be shown
        at all is this File's own ``display_refusal``; a refused file carries no
        content and says why. The bounded read is the primitive IO's.

        Args:
            max_bytes: How much of the file at most.

        Returns:
            ``name``, ``size_bytes``, ``truncated``, ``content`` and
            ``blocked_reason`` (empty when the text may be shown).
        """
        refused = self.display_refusal()
        if not refused:
            preview = bounded_text_preview(self.path, max_bytes, encoding=self.encoding)
            refused = self.display_refusal(
                preview["content"].encode(self.encoding, "ignore"),
            )
            if not refused:
                return {
                    "name": preview["name"],
                    "size_bytes": preview["size_bytes"],
                    "truncated": preview["truncated"],
                    "content": preview["content"],
                    "blocked_reason": "",
                }
        return {
            "name": self.path.name,
            "size_bytes": self.path.stat().st_size,
            "truncated": False,
            "content": "",
            "blocked_reason": refused,
        }

    def write_text(self, text: str) -> Path:
        """Replace this file's content with ``text`` (#1133).

        Ordinary text, written as UTF-8 in place, its parent directory made
        where missing. Structured content is a DataFile's ``write``; recording
        the write as a run operation is the caller's, through rey_lib.logs.

        Args:
            text: The file's new content.

        Returns:
            The path written.
        """
        return primitive_file_io.write_text(self.path, text)

    def display_refusal(self, content: bytes | None = None) -> str:
        """Why this file must not be shown as text, or empty when it may be (#1133).

        The file's own representation safety, the one rule there is: a
        secret-like name is refused by name, and binary content once its
        leading bytes are known. Whether a caller may reach the path at all is
        not this -- that is the installation's PathResolver.

        Args:
            content: Leading bytes of the file when already read. Binary
                detection is skipped when none are given.

        Returns:
            The refusal message, or an empty string when the text may be shown.
        """
        lowered = self.path.name.lower()
        for pattern in SECRET_NAME_PATTERNS:
            if fnmatch(lowered, pattern.lower()):
                return "Sensitive file preview is blocked."
        if content is not None and b"\x00" in content[:4096]:
            return "Binary file preview is not supported."
        return ""

    def __repr__(self) -> str:
        """Name the kind of file and the file, for a failure that has to be read."""
        return f"{type(self).__name__}({self.path.name!r})"
