"""ToolRegistry interprets loaded tool configuration (backlog 1093).

Configuration I/O loads tool configuration; ToolRegistry interprets it and reads
no configuration file of its own. These hold the interpretation that moved from
the retired file loader -- unchanged -- and that nothing in the module reads a
file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rey_lib.config.config_namespace import Namespace
from rey_lib.tools import ToolRegistry
from rey_lib.tools import tool_registry

EDITORS = [
    {"name": "vscode", "label": "VS Code", "command": "code", "args": ["{file_path}"]},
    {"name": "vim", "command": "vim", "enabled": False},
]
LAUNCHERS = [
    {"name": "chat", "category": "llm_test_launcher", "launch_mode": "browser",
     "url": "https://example.invalid"},
]
TOOLS = [{"name": "jq", "command": "jq"}]


def names(registry: ToolRegistry) -> list[str]:
    return [tool.name for tool in registry._tools]


def test_the_sections_are_read_in_their_order() -> None:
    registry = ToolRegistry.from_configuration({
        "llm_test_launchers": LAUNCHERS, "tools": TOOLS, "editors": EDITORS,
    })

    assert names(registry) == ["vscode", "vim", "jq", "chat"]


def test_a_flat_list_is_its_entries() -> None:
    assert names(ToolRegistry.from_configuration([*EDITORS, *TOOLS])) == ["vscode", "vim", "jq"]


def test_the_block_as_the_context_carries_it_reads_as_its_plain_form() -> None:
    loaded = Namespace({"editors": EDITORS, "llm_test_launchers": LAUNCHERS})

    assert names(ToolRegistry.from_configuration(loaded)) == names(
        ToolRegistry.from_configuration({"editors": EDITORS, "llm_test_launchers": LAUNCHERS}))


def test_an_unknown_section_is_not_a_tool() -> None:
    assert names(ToolRegistry.from_configuration({"plugins": TOOLS})) == []


def test_absent_configuration_is_an_empty_registry() -> None:
    assert names(ToolRegistry.from_configuration(None)) == []
    assert names(ToolRegistry.from_configuration({})) == []
    assert names(ToolRegistry.from_configuration("tools")) == []


def test_an_entry_that_is_not_a_mapping_is_skipped() -> None:
    assert names(ToolRegistry.from_configuration({"editors": ["vscode", *EDITORS[:1]]})) == [
        "vscode"]


def test_enabled_filtering_is_the_registry_s() -> None:
    registry = ToolRegistry.from_configuration({"editors": EDITORS})

    assert [tool.name for tool in registry.all_tools()] == ["vscode"]
    assert registry.find_tool("vim").enabled is False
    assert registry.find_tool("vscode").label == "VS Code"
    assert registry.find_tool("vscode").build_command({"{file_path}": "/a"}) == ["code", "/a"]


def test_the_registry_reads_no_configuration_file() -> None:
    """Configuration I/O is the sole reader of the YAML."""
    tree = ast.parse(Path(tool_registry.__file__).read_text(encoding="utf-8"))
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert "load_tool_registry" not in defined
    assert not any(name.endswith(("parse_yaml", "read_text_file")) for name in imported)
