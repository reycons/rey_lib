"""AI's own account of a held task: readiness, capabilities, contract (#1189 C5).

``AI.task_projection`` adds to ``AITask.projection`` what only the runtime can
answer, by the rules ``execute_task`` applies: whether each governed tool's
capability is bound (the task's connection's operations, then the caller's),
whether the engine's execution mechanism is one AI runs, the representations
the engine admits and the governed contract the task follows. A host forwards
it; nothing outside AI decides it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.ai import AI
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.task import AITask

CONFIGURATION = [
    {"row_type": "group", "task_key": None, "profile_key": "local_testing",
     "instruction_key": "code_tree", "is_enabled": True},
    {"row_type": "task", "task_key": "tree_inspection", "task_label": "Inspect",
     "is_enabled": True,
     "tools": [{"tool_key": "tree_expand", "description": "Children of a node.",
                "input_schema": {"type": "object"}, "replay_safe": True,
                "capability_key": "tree.children"}]},
    {"row_type": "task", "task_key": "search_index", "task_label": "Search",
     "is_enabled": True, "connection_name": "warehouse",
     "tools": [{"tool_key": "query", "description": "Run one query.",
                "input_schema": {"type": "object"}, "replay_safe": True,
                "capability_key": "database.query"}]},
]
ENGINES = [
    {"profile_key": "local_testing", "model_identifier": "ollama:qwen3-coder-next:latest",
     "credential_ref": "env.SOMETHING_SECRET", "access_allowed": ["redacted"],
     "access_default": "redacted", "is_enabled": True},
    {"profile_key": "elsewhere", "model_identifier": "other:model",
     "execution_mechanism": "carrier_pigeon", "is_enabled": True},
]
INSTRUCTIONS = [
    {"instruction_key": "code_tree", "kind": "contract", "contract_key": "code_tree",
     "contract_version": "3", "body": "Describe the node you were given."},
]


def _task(task_key: str, **choices: Any) -> AITask:
    return AITask.hydrate(CONFIGURATION, ENGINES, INSTRUCTIONS, installation="local",
                          task_key=task_key, **choices)


def _ai(connection_operations: dict[str, dict[str, Any]] | None = None) -> AI:
    return AI(registry=AIRegistry(), connection_operations=connection_operations)


def test_a_declared_capability_nothing_binds_makes_the_task_not_ready() -> None:
    projected = _ai().task_projection(_task("tree_inspection"))

    assert projected["capabilities"] == [{
        "tool": "tree_expand", "capability_key": "tree.children",
        "description": "Children of a node.", "bound": False}]
    assert projected["readiness"]["ready"] is False
    assert "tree.children" in projected["readiness"]["reasons"][0]


def test_the_callers_operations_bind_a_declared_capability() -> None:
    projected = _ai().task_projection(
        _task("tree_inspection"), {"tree.children": lambda arguments, tool: {}})

    assert projected["capabilities"][0]["bound"] is True
    assert projected["readiness"] == {"ready": True, "reasons": []}


def test_the_tasks_connection_binds_by_the_same_merge_an_execution_uses() -> None:
    ai = _ai({"warehouse": {"database.query": lambda arguments, tool: {}}})

    assert ai.task_projection(_task("search_index"))["readiness"]["ready"] is True


def test_an_execution_mechanism_ai_does_not_run_is_a_reason_it_would_be_refused() -> None:
    projected = _ai().task_projection(
        _task("tree_inspection", profile_key="elsewhere"),
        {"tree.children": lambda arguments, tool: {}})

    assert projected["readiness"]["ready"] is False
    assert "carrier_pigeon" in projected["readiness"]["reasons"][0]


def test_the_contract_and_representations_are_the_tasks_governed_ones_and_no_credential(
) -> None:
    projected = _ai().task_projection(_task("tree_inspection"))

    assert projected["contract"] == {
        "instruction_kind": "contract", "contract_key": "code_tree",
        "contract_version": "3", "body": "Describe the node you were given."}
    assert projected["representations"] == {"allowed": ["redacted"], "default": "redacted"}
    assert "SOMETHING_SECRET" not in str(projected)
    # Everything AITask states of itself is carried unchanged beside it.
    assert projected["task_key"] == "tree_inspection"
