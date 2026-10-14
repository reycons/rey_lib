"""The workflow objects own their code: no dependency on the legacy engine.

Backlog 950, rule replacement_objects_do_not_depend_on_the_legacy_code. Two
proofs. SOURCE: the modules' own import statements (the three workflow objects
and the canonical Run that executes their steps) name nothing in
``rey_lib.workflow.coordinator`` or ``rey_lib.workflow.engine`` and nothing
through the package ``rey_lib.workflow``. RUNTIME: in a fresh interpreter with
both legacy modules made unimportable, a Workflow is built and run to success
under a parent Run.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rey_lib.run.run as run_module
import rey_lib.workflow.workflow as workflow_module
import rey_lib.workflow.workflow_process as process_module
import rey_lib.workflow.workflow_step as step_module

_LEGACY = ("rey_lib.workflow.coordinator", "rey_lib.workflow.engine")

#: Run in a fresh interpreter: block the legacy modules, then build and run a graph.
_RUN_WITHOUT_LEGACY = textwrap.dedent("""
    import sys
    for legacy in ("rey_lib.workflow.coordinator", "rey_lib.workflow.engine"):
        sys.modules[legacy] = None
    from types import SimpleNamespace

    from tests.conftest import make_run_log
    from tests.support.workflow_runs import RecordingControl, parent_run
    from rey_lib.config.applications import Application, ApplicationOperation
    from rey_lib.workflow.workflow import Workflow
    from rey_lib.workflow.workflow_process import WorkflowProcess
    from rey_lib.workflow.workflow_step import WorkflowStep

    calls = []
    operation = ApplicationOperation(name="load", contract_definition={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
    })
    application = Application(name="app", workflow_operations=(operation,))
    step = WorkflowStep(id="s1", process_name="load", label="One", ordinal=1,
                        application=application, operation_name="load",
                        approved={"load": lambda *args: calls.append("load")},
                        settings={})
    process = WorkflowProcess(name="app", ordinal=1, application=application,
                              config=None, steps=[step])
    control = RecordingControl()
    workflow = Workflow(name="wf", enabled=True, tokens=None, config=None,
                        processes=[process], control=control)
    parent = parent_run(control)
    returned = workflow.run(parent, SimpleNamespace(), make_run_log(sys.argv[1]))
    assert returned is parent
    assert calls == ["load"], calls
    assert [c["status"] for c in control.children(parent)] == ["SUCCEEDED"]
    assert all(sys.modules.get(name) is None for name in sys.argv[2:]), "legacy loaded"
    print("ran without legacy")
""")


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


@pytest.mark.parametrize("module", [step_module, process_module, workflow_module, run_module],
                         ids=["workflow_step", "workflow_process", "workflow", "run"])
def test_the_module_imports_nothing_from_the_legacy_engine(module: object) -> None:
    imported = _imported(module)

    assert not [name for name in imported if name.startswith(_LEGACY)]
    assert "rey_lib.workflow" not in imported


def test_a_workflow_runs_with_the_legacy_coordinator_and_engine_unavailable(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _RUN_WITHOUT_LEGACY, str(tmp_path), *_LEGACY],
        cwd=root, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ran without legacy" in result.stdout
