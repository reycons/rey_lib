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


def test_editor_filtering_and_real_configured_launch(tmp_path):
    import json
    import sys
    import time
    source = tmp_path / "file with spaces.py"
    source.write_text("pass")
    received = tmp_path / "launched.json"
    code = "import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))"
    registry = ToolRegistry.from_configuration({"editors": [{
        "name": "test", "label": "Test editor", "command": sys.executable,
        "file_types": [".py"], "args": ["-c", code, str(received),
        "{file_path}", "{line_number}", "{column_number}"]},
        {"name": "off", "command": sys.executable, "enabled": False},
        {"name": "wrong_type", "command": sys.executable, "file_types": [".sql"]},
        {"name": "not_editor", "command": sys.executable, "category": "tool"}]})
    assert [e.name for e in registry.editors_for_file(source)] == ["test"]
    registry.open_editor("test", source, line=12, column=3)
    until = time.monotonic() + 3
    while not received.exists() and time.monotonic() < until:
        time.sleep(.01)
    assert json.loads(received.read_text()) == [str(source), "12", "3"]


def test_undeclared_disabled_and_wrong_kind_editors_cannot_launch(tmp_path, monkeypatch):
    import sys
    import pytest
    from rey_lib.errors.error_utils import AppError
    source = tmp_path / "file.py"
    source.write_text("pass")
    registry = ToolRegistry.from_configuration({"editors": [
        {"name": "off", "command": sys.executable, "enabled": False},
        {"name": "wrong_type", "command": sys.executable, "file_types": [".sql"]},
        {"name": "not_editor", "command": sys.executable, "category": "tool"}]})
    launched = []
    monkeypatch.setattr(tool_registry.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    for name in ("missing", "off", "wrong_type", "not_editor"):
        with pytest.raises(AppError): registry.open_editor(name, source)
    assert launched == []
