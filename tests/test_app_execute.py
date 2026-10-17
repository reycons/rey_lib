"""AppExecute is the one implementation of a run; Run is its compatibility alias.

User decision, 2026-10-16 (backlog console.workflow_map): the run's identity,
start, state, execution, logging and finish are owned by AppExecute, and ``Run``
is literally the same class -- not a wrapper, a subclass or a second
implementation -- so existing callers are unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import rey_lib
from rey_lib.run import AppExecute, Run
from rey_lib.run import run as run_module

from tests.support.workflow_runs import RecordingControl


def test_run_is_literally_app_execute() -> None:
    assert Run is AppExecute
    assert run_module.Run is run_module.AppExecute
    assert AppExecute.__name__ == "AppExecute"


def test_there_is_one_implementation_not_two() -> None:
    # AppExecute is the only class in the run module; Run is a name bound to it,
    # never a class of its own.
    tree = ast.parse(Path(run_module.__file__).read_text(encoding="utf-8"))
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes == ["AppExecute"]


def test_the_app_channel_is_not_the_runs() -> None:
    # Backlog 1042: the shared app communication -- the two ends of the channel --
    # is its own, and an application reaches it without going through the Run.
    from rey_lib.run import app_channel

    tree = ast.parse(Path(app_channel.__file__).read_text(encoding="utf-8"))
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes == ["LaunchedApp", "AttachedApp"]
    for entry in ("launch", "launch_operation", "attach"):
        assert not hasattr(AppExecute, entry), f"AppExecute still offers {entry}"
    assert callable(app_channel.LaunchedApp.launch)
    assert callable(app_channel.LaunchedApp.launch_operation)
    assert callable(app_channel.AttachedApp.attach)


def test_a_run_started_through_the_alias_is_an_app_execute_with_the_hierarchy() -> None:
    control = RecordingControl()
    application = Run.start(control, subject_type="app", subject_id="workflow_app")
    wrapper = Run.start(control, subject_type="workflow", subject_id="wf", parent=application)
    step = Run.start(control, subject_type="step", subject_id="s1", parent=wrapper)

    assert all(isinstance(one, AppExecute) for one in (application, wrapper, step))
    assert wrapper.parent_run_id == application.run_id
    assert step.parent_run_id == wrapper.run_id
    assert len({application.run_id, wrapper.run_id, step.run_id}) == 3


def test_anyio_is_reached_only_through_the_rey_wrapper() -> None:
    # The Rey async wrapper, rey_lib/run/async_runtime.py, is the only module that
    # imports AnyIO.
    root = Path(rey_lib.__file__).parent
    direct = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            if any(name == "anyio" or name.startswith("anyio.") for name in names):
                direct.append(str(path.relative_to(root)))
    assert sorted(set(direct)) == ["run/async_runtime.py"]
