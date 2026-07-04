"""
Config value provenance metadata for rey_lib config/context construction.

This module is additive and non-breaking: it records where each resolved config
value came from (file, section, layer, raw/resolved value, token dependencies,
and override history) WITHOUT changing the runtime config values themselves.

The runtime config tree keeps returning plain strings/dicts/lists/paths. The
metadata is stored separately in a :class:`ConfigMetadata` container attached to
``ctx`` and queried through the helper functions below.

See SGC_Config_Utils_Value_Provenance_Metadata.

This module intentionally does not import ``yaml`` — YAML loading stays in
``config_utils`` — and holds no application-specific logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = [
    "ConfigValueMetadata",
    "ConfigMetadata",
    "extract_dependencies",
    "layer_for_source",
    "get_config_metadata",
    "get_config_source_files",
    "get_config_source_map",
    "explain_config_value",
]

# Matches ``{token_name}`` placeholders inside raw string values.
_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class _SafeFormat(dict):  # type: ignore[type-arg]
    """dict subclass that leaves unknown ``{placeholders}`` untouched."""

    def __missing__(self, key: str) -> str:
        """Return the placeholder unchanged for keys with no resolved value."""
        return f"{{{key}}}"


@dataclass
class ConfigValueMetadata:
    """Provenance for a single resolved config value.

    Attributes mirror the required metadata shape in the SGC. Optional fields
    default to empty/None so partial provenance never breaks construction.
    """

    name: str
    path: str
    raw_value: Any = None
    resolved_value: Any = None
    source_file: str | None = None
    source_line: int | None = None
    source_section: str | None = None
    layer: str | None = None
    depends_on: list[str] = field(default_factory=list)
    overrides: list["ConfigValueMetadata"] = field(default_factory=list)
    resolver: str | None = None
    resolution_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract_dependencies(raw_value: Any) -> list[str]:
    """Return the ordered, de-duplicated ``{token}`` names in a raw value.

    Non-string values have no textual token dependencies and yield an empty
    list. Detection is conservative: it only records placeholder names and does
    not attempt to resolve them.

    Parameters
    ----------
    raw_value : Any
        The value before token/path substitution.

    Returns
    -------
    list[str]
        Token names referenced by the value, in first-seen order.
    """
    if not isinstance(raw_value, str):
        return []
    seen: list[str] = []
    for token in _TOKEN_RE.findall(raw_value):
        if token not in seen:
            seen.append(token)
    return seen


def layer_for_source(source_file: Path | str, config_dir: Path | str) -> str:
    """Classify the precedence layer for a config source file.

    Conservative best-effort classification used only for explainability. The
    root ``config.yaml`` and installation-level files map to ``installation``;
    files under a ``workflows`` folder map to ``workflow``. Unknown/synthetic
    sources should be recorded as ``runtime`` by the caller instead.

    Parameters
    ----------
    source_file : Path | str
        The YAML/config file that supplied the value.
    config_dir : Path | str
        The installation config directory the source lives under.

    Returns
    -------
    str
        The classified layer name.
    """
    try:
        rel = Path(source_file).resolve().relative_to(Path(config_dir).resolve())
    except ValueError:
        # Source is outside the config directory — treat as installation-level.
        return "installation"
    parts = [part.lower() for part in rel.parts]
    if "workflows" in parts:
        return "workflow"
    return "installation"


class ConfigMetadata:
    """Container of :class:`ConfigValueMetadata` keyed by dotted config path.

    Values are recorded in config merge order so that a later layer replacing an
    earlier value carries the prior entry in its ``overrides`` history. The
    container never holds the runtime config values themselves — only provenance.
    """

    def __init__(self) -> None:
        """Create an empty metadata container."""
        self._entries: dict[str, ConfigValueMetadata] = {}

    # -- recording ---------------------------------------------------------

    def record_tree(
        self,
        data: Any,
        *,
        source_file: str,
        layer: str,
        prefix: str = "",
    ) -> None:
        """Record provenance for every leaf value in a loaded config dict.

        Nested dicts recurse by key. The top-level ``paths`` list is recorded as
        individual name tokens (``paths.<name>``). Other lists of named dicts
        recurse by item name (e.g. ``workflows.<name>...``); plain lists are
        recorded as a single opaque value.

        Parameters
        ----------
        data : Any
            A parsed config mapping (typically one YAML file's contents).
        source_file : str
            Path string of the file that supplied these values.
        layer : str
            Precedence layer for this source file.
        prefix : str
            Dotted-path prefix for recursion; empty at the top level.
        """
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            if key == "paths" and isinstance(value, list):
                self._record_path_tokens(value, source_file=source_file, layer=layer, prefix=child)
            elif isinstance(value, dict):
                self.record_tree(value, source_file=source_file, layer=layer, prefix=child)
            elif isinstance(value, list):
                self._record_list(value, source_file=source_file, layer=layer, prefix=child)
            else:
                self.record_value(child, value, source_file=source_file, layer=layer)

    def record_value(
        self,
        path: str,
        raw_value: Any,
        *,
        source_file: str | None,
        layer: str | None,
    ) -> None:
        """Record (or override) provenance for one dotted config path.

        When the path already exists with a different raw value, the new entry
        inherits the prior override chain plus a flattened copy of the previous
        entry, preserving override history without deep nesting.

        Parameters
        ----------
        path : str
            Dotted config path (e.g. ``paths.data``).
        raw_value : Any
            The value before token/path substitution.
        source_file : str | None
            The file that supplied the value, or ``None`` if synthetic.
        layer : str | None
            The precedence layer for this value.
        """
        section = path.split(".", 1)[0]
        entry = ConfigValueMetadata(
            name=path.rsplit(".", 1)[-1],
            path=path,
            raw_value=raw_value,
            resolved_value=raw_value,
            source_file=source_file,
            source_line=None,
            source_section=section,
            layer=layer,
            depends_on=extract_dependencies(raw_value),
        )
        existing = self._entries.get(path)
        if existing is not None and existing.raw_value != raw_value:
            prior = replace(existing, overrides=[])
            entry.overrides = list(existing.overrides) + [prior]
        self._entries[path] = entry

    def _record_path_tokens(
        self,
        entries: list[Any],
        *,
        source_file: str,
        layer: str,
        prefix: str,
    ) -> None:
        """Record each ``paths:`` list entry as a ``<prefix>.<name>`` token."""
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            template = entry.get("path")
            if not name or template is None:
                continue
            self.record_value(
                f"{prefix}.{name}", template, source_file=source_file, layer=layer
            )

    def _record_list(
        self,
        items: list[Any],
        *,
        source_file: str,
        layer: str,
        prefix: str,
    ) -> None:
        """Recurse named-dict lists by name; record other lists as one value."""
        dict_items = [item for item in items if isinstance(item, dict)]
        named = dict_items and len(dict_items) == len(items) and all(
            item.get("name") for item in dict_items
        )
        if named:
            for item in items:
                name = str(item.get("name"))
                self.record_tree(
                    item, source_file=source_file, layer=layer, prefix=f"{prefix}.{name}"
                )
        else:
            self.record_value(prefix, list(items), source_file=source_file, layer=layer)

    # -- resolution --------------------------------------------------------

    def set_resolved(self, path: str, resolved_value: Any) -> None:
        """Set the resolved value for a known path (e.g. a resolved path token)."""
        entry = self._entries.get(path)
        if entry is not None:
            entry.resolved_value = resolved_value

    def resolve_values(self, resolver_strs: dict[str, str]) -> None:
        """Fill ``resolved_value`` for tokenized string values.

        Uses the final token→string map to substitute ``{token}`` placeholders.
        Unknown placeholders are left intact, mirroring runtime behaviour. Values
        with no placeholder keep ``resolved_value == raw_value``.

        Parameters
        ----------
        resolver_strs : dict[str, str]
            Final resolved token name → string map.
        """
        fmt = _SafeFormat(resolver_strs)
        for entry in self._entries.values():
            if isinstance(entry.raw_value, str) and "{" in entry.raw_value:
                entry.resolved_value = entry.raw_value.format_map(fmt)

    # -- lookup ------------------------------------------------------------

    def get(self, path: str) -> ConfigValueMetadata | None:
        """Return metadata for a dotted path, or ``None`` if not tracked."""
        return self._entries.get(path)

    def paths(self) -> list[str]:
        """Return all tracked dotted config paths."""
        return list(self._entries)

    def source_files(self, prefix: str = "") -> list[str]:
        """Return the distinct source files for a subtree, in first-seen order.

        Parameters
        ----------
        prefix : str
            Dotted-path prefix to filter by. Empty returns every source file.

        Returns
        -------
        list[str]
            Distinct source file paths contributing to the subtree.
        """
        files: list[str] = []
        for path, entry in self._entries.items():
            if prefix and path != prefix and not path.startswith(f"{prefix}."):
                continue
            if entry.source_file and entry.source_file not in files:
                files.append(entry.source_file)
        return files

    def source_map(self, prefix: str = "") -> dict[str, dict[str, Any]]:
        """Return per-source-file contribution info for a subtree.

        For each distinct source file under ``prefix``, reports the layer and the
        sorted list of subtree sections it contributed (the first path segment
        after the prefix). Lets a consumer explain *what* each file provided
        without reading the internal entry structure.

        Parameters
        ----------
        prefix : str
            Dotted-path prefix to filter by. Empty maps the whole tree.

        Returns
        -------
        dict[str, dict[str, Any]]
            ``{source_file: {"layer": str | None, "sections": list[str]}}``.
        """
        out: dict[str, dict[str, Any]] = {}
        for path, entry in self._entries.items():
            if prefix and path != prefix and not path.startswith(f"{prefix}."):
                continue
            if not entry.source_file:
                continue
            info = out.setdefault(entry.source_file, {"layer": entry.layer, "sections": []})
            if prefix:
                remainder = "" if path == prefix else path[len(prefix) + 1:]
            else:
                remainder = path
            section = remainder.split(".", 1)[0] if remainder else (entry.source_section or "")
            if section and section not in info["sections"]:
                info["sections"].append(section)
        for info in out.values():
            info["sections"].sort()
        return out


# ---------------------------------------------------------------------------
# Public helper API (queried against a built ctx)
# ---------------------------------------------------------------------------

def _container(ctx: Any) -> ConfigMetadata | None:
    """Return the metadata container attached to *ctx*, if any."""
    return getattr(ctx, "_config_metadata", None)


def get_config_metadata(ctx: Any, path: str) -> ConfigValueMetadata | None:
    """Return provenance metadata for a dotted config path on *ctx*.

    Parameters
    ----------
    ctx : Any
        A context built by ``build_ctx_from_path``.
    path : str
        Dotted config path (e.g. ``paths.data``).

    Returns
    -------
    ConfigValueMetadata | None
        The metadata entry, or ``None`` when unavailable.
    """
    container = _container(ctx)
    return container.get(path) if container is not None else None


def get_config_source_files(ctx: Any, prefix: str = "") -> list[str]:
    """Return the source files contributing to a config subtree on *ctx*.

    Parameters
    ----------
    ctx : Any
        A context built by ``build_ctx_from_path``.
    prefix : str
        Dotted-path prefix to filter by; empty returns all source files.

    Returns
    -------
    list[str]
        Distinct source file paths, or an empty list when no metadata exists.
    """
    container = _container(ctx)
    return container.source_files(prefix) if container is not None else []


def get_config_source_map(ctx: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Return per-source-file contribution info for a config subtree on *ctx*.

    Public wrapper over :meth:`ConfigMetadata.source_map` so consumers can explain
    which files fed a subtree (and which sections) without touching the internal
    metadata store.

    Parameters
    ----------
    ctx : Any
        A context built by ``build_ctx_from_path``.
    prefix : str
        Dotted-path prefix to filter by; empty maps the whole tree.

    Returns
    -------
    dict[str, dict[str, Any]]
        ``{source_file: {"layer": str | None, "sections": list[str]}}``; empty
        when no metadata exists.
    """
    container = _container(ctx)
    return container.source_map(prefix) if container is not None else {}


def explain_config_value(ctx: Any, path: str) -> str:
    """Return a human-readable explanation of a config value's provenance.

    Intended for debug/console tooling. Returns a short multi-line summary of
    the value, its source, layer, raw/resolved values, dependencies, and any
    override chain. Returns a clear message when no metadata is available.

    Parameters
    ----------
    ctx : Any
        A context built by ``build_ctx_from_path``.
    path : str
        Dotted config path to explain.

    Returns
    -------
    str
        A formatted explanation string.
    """
    meta = get_config_metadata(ctx, path)
    if meta is None:
        return f"No provenance metadata for {path!r}."
    lines = [
        f"Config: {meta.path}",
        f"Value: {meta.resolved_value}",
        f"Defined in: {meta.source_file or '(unknown)'}",
        f"Layer: {meta.layer or '(unknown)'}",
        f"Raw value: {meta.raw_value}",
    ]
    if meta.depends_on:
        lines.append(f"Depends on: {', '.join(meta.depends_on)}")
    for prior in meta.overrides:
        lines.append(f"Overrides: {prior.source_file or '(unknown)'} = {prior.raw_value}")
    return "\n".join(lines)
