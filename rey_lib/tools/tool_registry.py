"""
Tool and editor registry loader.

Loads tool definitions from a YAML config file. The UI renders actions
from the registry — never from hardcoded tool names or editor assumptions.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.files.file_utils import read_text_file

__all__ = ["ToolEntry", "ToolRegistry", "load_tool_registry"]

# Supported runtime token placeholders in launch arg templates.
_TOKEN_FILE_PATH = "{file_path}"
_TOKEN_FOLDER_PATH = "{folder_path}"
_TOKEN_LINE_NUMBER = "{line_number}"
_TOKEN_COLUMN_NUMBER = "{column_number}"


@dataclass
class ToolEntry:
    """One configured tool or editor from the registry."""

    name: str
    label: str
    enabled: bool
    command: str
    # Mutable fields use field(default_factory=...) to avoid shared state.
    args: list[str] = field(default_factory=list)
    use_for: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    launch_mode: str = "local_cli"
    category: str = "editor"
    # Launcher-specific fields (used by llm_test_launcher category).
    url: str = ""
    payload_mode: str = ""
    provider: str = ""

    def is_available(self) -> bool:
        """Return True when the tool or launcher is usable.

        - ``browser``: available when a URL is configured.
        - ``local_cli`` / ``local_app``: command must exist in PATH.
        - All other modes return False until an adapter is registered.
        """
        if self.launch_mode == "browser":
            return bool(self.url)
        if self.launch_mode in {"local_cli", "local_app"}:
            return shutil.which(self.command) is not None
        return False

    def supports_file(self, path: str | Path) -> bool:
        """Return True when this tool is configured to handle the file extension."""
        if not self.file_types:
            # No restriction — tool accepts all file types.
            return True
        suffix = Path(path).suffix.lower()
        return suffix in {ft.lower() for ft in self.file_types}

    def build_command(self, tokens: dict[str, str] | None = None) -> list[str]:
        """Return the full command list with runtime tokens substituted in args."""
        resolved_args = _substitute_tokens(self.args, tokens or {})
        return [self.command] + resolved_args

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation, including runtime availability."""
        return {
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "command": self.command,
            "args": self.args,
            "use_for": self.use_for,
            "file_types": self.file_types,
            "launch_mode": self.launch_mode,
            "category": self.category,
            "url": self.url,
            "payload_mode": self.payload_mode,
            "provider": self.provider,
            "available": self.is_available(),
        }


class ToolRegistry:
    """Registry of configured tools and editors loaded from YAML configuration."""

    def __init__(self, tools: list[ToolEntry]) -> None:
        """Initialize with a list of parsed tool entries."""
        self._tools = tools

    def all_tools(self) -> list[ToolEntry]:
        """Return all enabled tools regardless of PATH availability."""
        return [t for t in self._tools if t.enabled]

    def available_tools(self) -> list[ToolEntry]:
        """Return enabled tools whose command is found in PATH."""
        return [t for t in self.all_tools() if t.is_available()]

    def tools_for_file(self, path: str | Path) -> list[ToolEntry]:
        """Return available tools that support the given file's extension."""
        return [t for t in self.available_tools() if t.supports_file(path)]

    def find_tool(self, name: str) -> ToolEntry | None:
        """Return a tool entry by name, or None if not found."""
        return next((t for t in self._tools if t.name == name), None)

    def to_list(self) -> list[dict[str, Any]]:
        """Return a JSON-safe list of all enabled tools with availability status."""
        return [t.to_dict() for t in self.all_tools()]


def load_tool_registry(config_path: Path) -> ToolRegistry:
    """Load a ToolRegistry from a tools.yaml file.

    Returns an empty registry when the file does not exist, so callers
    can treat a missing config as "no tools configured" rather than an error.

    Expected YAML shape::

        tools:
          editors:
            - name: vscode
              ...

    The top-level key is ``tools``; its value is a dict with an ``editors``
    (or ``tools``) list. A flat top-level list is also accepted.
    """
    if not config_path.exists():
        return ToolRegistry([])

    data = parse_yaml(read_text_file(config_path)) or {}

    if not isinstance(data, dict):
        return ToolRegistry([])

    tools_block = data.get("tools") or {}
    entries_raw: list[Any] = []

    if isinstance(tools_block, dict):
        # Collect all known sections: editors, tools, llm_test_launchers.
        for key in ("editors", "tools", "llm_test_launchers"):
            section = tools_block.get(key)
            if isinstance(section, list):
                entries_raw.extend(section)
    elif isinstance(tools_block, list):
        # Flat list under the top-level ``tools`` key.
        entries_raw = tools_block

    tools = [
        _parse_tool_entry(entry)
        for entry in entries_raw
        if isinstance(entry, dict)
    ]
    return ToolRegistry(tools)


def _parse_tool_entry(raw: dict[str, Any]) -> ToolEntry:
    """Parse a raw YAML dict into a ToolEntry, using safe defaults."""
    return ToolEntry(
        name=str(raw.get("name", "")),
        label=str(raw.get("label", raw.get("name", ""))),
        enabled=bool(raw.get("enabled", True)),
        command=str(raw.get("command", "")),
        args=list(raw.get("args") or []),
        use_for=list(raw.get("use_for") or []),
        file_types=list(raw.get("file_types") or []),
        launch_mode=str(raw.get("launch_mode", "local_cli")),
        category=str(raw.get("category", "editor")),
        url=str(raw.get("url", "")),
        payload_mode=str(raw.get("payload_mode", "")),
        provider=str(raw.get("provider", "")),
    )


def _substitute_tokens(args: list[str], tokens: dict[str, str]) -> list[str]:
    """Replace runtime token placeholders in each arg string."""
    result = []
    for arg in args:
        resolved = arg
        for token, value in tokens.items():
            resolved = resolved.replace(token, value)
        result.append(resolved)
    return result
