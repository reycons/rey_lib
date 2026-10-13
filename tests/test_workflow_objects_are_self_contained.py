"""The workflow objects own their code: no source dependency on the legacy engine.

Backlog 956, rule replacement_objects_do_not_depend_on_the_legacy_code. Proves a
SOURCE property: the modules' own import statements name nothing in
``rey_lib.workflow.coordinator`` and nothing through the package
``rey_lib.workflow``. It does not prove the coordinator is unloaded: the
package's ``__init__`` still imports it until cut-over.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import rey_lib.workflow.workflow as workflow_module
import rey_lib.workflow.workflow_process as process_module
import rey_lib.workflow.workflow_step as step_module


def _imported(module: object) -> list[str]:
    """Every module name the given module's import statements name."""
    tree = ast.parse(Path(str(getattr(module, "__file__"))).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", [step_module, process_module, workflow_module],
                         ids=["workflow_step", "workflow_process", "workflow"])
def test_the_module_imports_nothing_from_the_legacy_coordinator(module: object) -> None:
    imported = _imported(module)

    assert not [name for name in imported if name.startswith("rey_lib.workflow.coordinator")]
    assert "rey_lib.workflow" not in imported
