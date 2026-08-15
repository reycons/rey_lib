"""Runtime entry-point discovery for the repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-003, REQ-050 to REQ-053).

An entry point is a place execution can begin. Templates are parsed with an
HTML parser rather than scanned as text, so a script tag inside an HTML comment
does not become a root.

Classic scripts and bundled entry points are recorded as different kinds
because they are reached differently: a classic script is loaded by src and
publishes onto the global object, while a bundle is compiled output whose
internals were resolved by a bundler. A template that is not the primary window
is recorded as an alternate window, because it bootstraps its own object set
and is exactly where a second registration list hides.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from rey_lib.files.file_utils import read_text_file
from rey_lib.repository_map.records import (
    ENTRY_POINT_KIND_ALTERNATE_WINDOW,
    ENTRY_POINT_KIND_BUNDLE,
    ENTRY_POINT_KIND_CLASSIC_SCRIPT,
    ENTRY_POINT_KIND_INLINE_CALL,
    ENTRY_POINT_KIND_MODULE,
    ENTRY_POINT_KIND_TEMPLATE,
    EntryPointRecord,
    FileRecord,
    ScanRules,
    matches_any_glob,
)

__all__ = ["extract_runtime_entry_points"]

# Inline script types that carry data rather than executable code.
_DATA_SCRIPT_TYPES = frozenset(
    {"application/json", "application/ld+json", "text/template", "text/x-template"}
)


class _ScriptCollector(HTMLParser):
    """Collect script tags and their positions from one template.

    Attributes:
        scripts: Tuples of src, type, line and column for each script tag.
        inline: Tuples of type, line and column for each inline script that
            carries executable content.
    """

    def __init__(self) -> None:
        """Initialize the collector."""
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[str, str, int, int]] = []
        self.inline: list[tuple[str, int, int]] = []
        self._open_inline: tuple[str, int, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record a script start tag.

        Args:
            tag: Element name.
            attrs: Element attributes.
        """
        if tag != "script":
            return
        attributes = {name: (value or "") for name, value in attrs}
        line, column = self.getpos()
        source = attributes.get("src", "")
        if source:
            self.scripts.append((source, attributes.get("type", ""), line, column))
            self._open_inline = None
            return
        self._open_inline = (attributes.get("type", ""), line, column)

    def handle_data(self, data: str) -> None:
        """Record inline script content.

        Args:
            data: Element text content.
        """
        if self._open_inline is None or not data.strip():
            return
        script_type, line, column = self._open_inline
        if script_type.lower() not in _DATA_SCRIPT_TYPES:
            self.inline.append((script_type, line, column))
        self._open_inline = None

    def handle_endtag(self, tag: str) -> None:
        """Close the current inline script.

        Args:
            tag: Element name.
        """
        if tag == "script":
            self._open_inline = None


def extract_runtime_entry_points(
    repo_root: Path,
    files: list[FileRecord],
    rules: ScanRules,
) -> list[EntryPointRecord]:
    """Extract every runtime entry point declared by a repository's templates.

    Args:
        repo_root: Repository root the file paths are relative to.
        files: The inventoried files to scan.
        rules: The scanned repository's own scan rules.

    Returns:
        Entry points sorted by host, line and target.
    """
    templates = [
        record for record in files if matches_any_glob(record.path, rules.template_globs)
    ]
    records: list[EntryPointRecord] = []

    for template in templates:
        is_primary = template.path == rules.primary_template
        # The template itself is a root. A non-primary one is an alternate
        # window: it bootstraps its own object set.
        records.append(
            EntryPointRecord(
                source_path=template.path,
                source_line=1,
                source_column=0,
                entry_point_kind=(
                    ENTRY_POINT_KIND_TEMPLATE if is_primary else ENTRY_POINT_KIND_ALTERNATE_WINDOW
                ),
                target=template.path,
                window_or_host=template.path,
            )
        )
        records.extend(_template_entry_points(repo_root / template.path, template.path, rules))

    records.sort(
        key=lambda record: (record.window_or_host, record.source_line, record.target)
    )
    return records


def _template_entry_points(
    path: Path,
    source_path: str,
    rules: ScanRules,
) -> list[EntryPointRecord]:
    """Return the entry points one template declares.

    Args:
        path: Absolute path to the template.
        source_path: Repository-relative path to record.
        rules: The scanned repository's scan rules.

    Returns:
        The entry points found in the template.
    """
    collector = _ScriptCollector()
    collector.feed(read_text_file(path))
    collector.close()

    records = []
    for source, script_type, line, column in collector.scripts:
        target = _normalize_target(source)
        records.append(
            EntryPointRecord(
                source_path=source_path,
                source_line=line,
                source_column=column,
                entry_point_kind=_script_kind(target, script_type, rules),
                target=target,
                window_or_host=source_path,
            )
        )
    for script_type, line, column in collector.inline:
        records.append(
            EntryPointRecord(
                source_path=source_path,
                source_line=line,
                source_column=column,
                entry_point_kind=ENTRY_POINT_KIND_INLINE_CALL,
                target=script_type or "inline",
                window_or_host=source_path,
            )
        )
    return records


def _script_kind(target: str, script_type: str, rules: ScanRules) -> str:
    """Return the entry-point kind for a loaded script.

    Args:
        target: The normalized script source.
        script_type: The tag's type attribute.
        rules: The scanned repository's scan rules.

    Returns:
        One of the ``ENTRY_POINT_KIND_*`` constants.
    """
    if matches_any_glob(target, rules.bundle_globs) and rules.bundle_globs:
        return ENTRY_POINT_KIND_BUNDLE
    if script_type.lower() == "module":
        return ENTRY_POINT_KIND_MODULE
    return ENTRY_POINT_KIND_CLASSIC_SCRIPT


def _normalize_target(source: str) -> str:
    """Return a script src without its cache-busting query.

    Args:
        source: The raw src attribute.

    Returns:
        The src with any query string removed, so the same asset does not
        appear as several targets.
    """
    return source.split("?", 1)[0]
