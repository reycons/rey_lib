"""The governed tree_expand declaration, with nothing bound to it (backlog 742).

Backlog 742 governs tree_expand and binds nothing: the canonical home of the
non-presentation tree behaviour AI needs is its own architectural row. Until it
is decided, the new path must refuse tree.children as unbound -- never fall
through to, or wrap, the legacy tree inspection.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.task import AITask

TREE_EXPAND_ROW = {
    "tool_key": "tree_expand", "description": "List one shown node's children.",
    "input_schema": {"type": "object",
                     "properties": {"node_key": {"type": "string"}},
                     "required": ["node_key"], "additionalProperties": False},
    "replay_safe": True, "capability_key": "tree.children", "result_row_limit": 50,
}

CONFIGURATION = [
    {"row_type": "group", "is_enabled": True, "profile_key": "local_testing",
     "instruction_key": None, "temperature": None, "representation": None,
     "composed_first": False, "connection_name": None, "tools": []},
    {"row_type": "task", "is_enabled": True, "task_key": "tree_inspection",
     "task_label": "Inspect", "profile_key": "local_testing", "instruction_key": None,
     "temperature": None, "representation": None, "composed_first": False,
     "connection_name": None, "tools": [TREE_EXPAND_ROW]},
]
ENGINES = [{"profile_key": "local_testing", "is_enabled": True,
            "model_identifier": "ollama:qwen3-coder-next:latest",
            "base_url": "http://localhost:11434/v1"}]


def _task() -> AITask:
    return AITask.hydrate(CONFIGURATION, ENGINES, [], installation="local",
                          task_key="tree_inspection", input=AIInput.prompt("What is here?"))


def test_tree_expand_hydrates_as_governed() -> None:
    tool = _task().tools[0]

    assert (tool.name, tool.capability_key, tool.replay_safe, tool.result_row_limit) == (
        "tree_expand", "tree.children", True, 50,
    )


def test_the_new_path_refuses_tree_children_as_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[AgentInfo] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.append(info)
        return ModelResponse(parts=[TextPart("never")])

    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: FunctionModel(answer))
    task = _task()

    with pytest.raises(AIConfigurationError, match="'tree.children'.*does not bind"):
        ai.execute_task(task)

    assert task.status == AITask.FAILED
    assert asked == []
