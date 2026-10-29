"""Governed AITools, bound at execution into an AIToolSet (backlog 1023).

Deterministic: no database and no engine. Tool rows have the shape the extended
control.f_ai_configuration_get returns in its tools column; Pydantic AI's
FunctionModel stands in for the engine and calls the tools as a model would.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError, AIToolError
from rey_lib.ai import ai as ai_module
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AITool, AIToolSet
from rey_lib.errors.error_utils import AppError

QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
}

TOOL_ROWS = [
    {"tool_key": "run_query", "description": "Run one read query.",
     "input_schema": QUERY_SCHEMA, "replay_safe": False, "capability_key": "database.query"},
    {"tool_key": "list_children", "description": "List a node's children.",
     "input_schema": {"type": "object", "properties": {"node": {"type": "string"}},
                      "required": ["node"]},
     "replay_safe": True, "capability_key": "tree.children"},
]

CONFIGURATION = [
    {"row_type": "group", "is_enabled": True, "profile_key": "local_testing",
     "instruction_key": None, "temperature": None, "representation": None,
     "composed_first": False, "connection_name": None, "tools": []},
    {"row_type": "task", "is_enabled": True, "task_key": "search_index",
     "task_label": "Search", "profile_key": "local_testing", "instruction_key": None,
     "temperature": 0, "representation": None, "composed_first": False,
     "connection_name": "llm_reader", "tools": TOOL_ROWS},
]
ENGINES = [{"profile_key": "local_testing", "is_enabled": True,
            "model_identifier": "ollama:qwen3-coder-next:latest",
            "base_url": "http://localhost:11434/v1"}]


def _hydrated(task_key: str = "search_index") -> AITask:
    return AITask.hydrate(CONFIGURATION, ENGINES, [], installation="local",
                          task_key=task_key, input=AIInput.prompt("Find it."))


class _Owner:
    """Stands in for a canonical owner; counts what reached it."""

    def __init__(self, answer: Any = None, refuses: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self._answer = answer
        self._refuses = refuses

    def __call__(self, arguments: dict[str, Any], tool: AITool) -> Any:
        self.calls.append(arguments)
        if self._refuses:
            raise AppError(self._refuses)
        return self._answer if self._answer is not None else {"rows": len(self.calls)}


class TestHydrationAndBinding:
    def test_a_task_hydrates_its_governed_tools_as_facts(self) -> None:
        task = _hydrated()

        assert [tool.name for tool in task.tools] == ["run_query", "list_children"]
        query = task.tools[0]
        assert (query.replay_safe, query.capability_key) == (False, "database.query")
        assert query.input_schema == QUERY_SCHEMA

    def test_the_default_scope_offers_no_tools(self) -> None:
        assert _hydrated(task_key="").tools == ()

    def test_hydration_takes_no_runtime_operations(self) -> None:
        with pytest.raises(TypeError):
            AITask.hydrate(CONFIGURATION, ENGINES, [], installation="local",
                           task_key="search_index", operations={})  # type: ignore[call-arg]

    def test_an_unbound_capability_is_refused(self) -> None:
        with pytest.raises(AIConfigurationError, match="'tree.children'.*does not bind"):
            AIToolSet(_hydrated().tools, {"database.query": _Owner()})

    def test_an_unknown_capability_key_is_refused(self) -> None:
        tool = AITool(name="mystery", capability_key="", replay_safe=True)

        with pytest.raises(AIConfigurationError, match="'mystery' names capability '\\(none\\)'"):
            AIToolSet((tool,), {"database.query": _Owner()})

    def test_two_tools_with_one_name_are_refused(self) -> None:
        tool = _hydrated().tools[0]

        with pytest.raises(AIConfigurationError, match="Two AI tools"):
            AIToolSet((tool, tool), {"database.query": _Owner()})


class TestDispatch:
    def _set(self, owner: _Owner) -> AIToolSet:
        return AIToolSet(_hydrated().tools,
                         {"database.query": owner, "tree.children": owner})

    def test_a_valid_call_reaches_the_owner_with_its_arguments(self) -> None:
        owner = _Owner(answer=[{"id": 1}])

        assert self._set(owner).invoke("run_query", {"sql": "SELECT 1"}) == [{"id": 1}]
        assert owner.calls == [{"sql": "SELECT 1"}]

    def test_arguments_that_break_the_schema_never_reach_the_owner(self) -> None:
        owner = _Owner()

        with pytest.raises(AIToolError, match="breaks? its schema"):
            self._set(owner).invoke("run_query", {"query": "SELECT 1"})
        assert owner.calls == []

    def test_an_unoffered_tool_is_refused(self) -> None:
        with pytest.raises(AIToolError, match="No AI tool is offered as 'drop_table'"):
            self._set(_Owner()).invoke("drop_table", {})


class TestReplaySafety:
    def _set(self, owner: _Owner) -> AIToolSet:
        return AIToolSet(_hydrated().tools,
                         {"database.query": owner, "tree.children": owner})

    def test_a_replay_safe_tool_runs_every_time(self) -> None:
        owner = _Owner()
        toolset = self._set(owner)

        toolset.invoke("list_children", {"node": "a"})
        toolset.invoke("list_children", {"node": "a"})

        assert len(owner.calls) == 2

    def test_a_non_replay_safe_tool_runs_once_and_the_repeat_gets_the_first_result(self) -> None:
        owner = _Owner()
        toolset = self._set(owner)

        first = toolset.invoke("run_query", {"sql": "SELECT 1"})
        second = toolset.invoke("run_query", {"sql": "SELECT 1"})

        assert len(owner.calls) == 1
        assert second == first

    def test_normalized_arguments_are_the_same_call(self) -> None:
        owner = _Owner()
        tool = AITool(name="pair", capability_key="pair", replay_safe=False,
                      input_schema={"type": "object"})
        toolset = AIToolSet((tool,), {"pair": owner})

        toolset.invoke("pair", {"a": 1, "b": 2})
        toolset.invoke("pair", {"b": 2, "a": 1})

        assert len(owner.calls) == 1

    def test_different_arguments_run_again(self) -> None:
        owner = _Owner()
        toolset = self._set(owner)

        toolset.invoke("run_query", {"sql": "SELECT 1"})
        toolset.invoke("run_query", {"sql": "SELECT 2"})

        assert len(owner.calls) == 2

    def test_the_guarantee_is_scoped_to_one_execution(self) -> None:
        owner = _Owner()

        self._set(owner).invoke("run_query", {"sql": "SELECT 1"})
        self._set(owner).invoke("run_query", {"sql": "SELECT 1"})

        assert len(owner.calls) == 2


class _ScriptedEngine:
    """A FunctionModel that issues the given tool calls, one per turn, then answers."""

    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self._calls = list(calls)
        self.returned: list[Any] = []
        self.retried: list[str] = []
        self.seen: list[AgentInfo] = []
        self.model = FunctionModel(self._turn)

    def _turn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(info)
        last = messages[-1]
        if isinstance(last, ModelRequest):
            for part in last.parts:
                if isinstance(part, ToolReturnPart):
                    self.returned.append(part.content)
                elif isinstance(part, RetryPromptPart):
                    self.retried.append(str(part.content))
        if self._calls:
            name, arguments = self._calls.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, arguments)])
        return ModelResponse(parts=[TextPart("done")])


def _ai(engine: _ScriptedEngine, monkeypatch: pytest.MonkeyPatch, corrections: int = 0) -> AI:
    monkeypatch.setattr(ai_module, "_MAX_TOOL_CORRECTIONS", corrections)
    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: engine.model)
    return ai


class TestThroughPydanticAI:
    def test_the_model_sees_the_tools_and_non_replay_safe_ones_run_alone(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine()
        owner = _Owner()

        _ai(engine, monkeypatch).execute_task(
            _hydrated(), {"database.query": owner, "tree.children": owner},
        )

        offered = {tool.name: tool for tool in engine.seen[0].function_tools}
        assert set(offered) == {"run_query", "list_children"}
        assert offered["run_query"].sequential is True
        assert offered["list_children"].sequential is False
        assert offered["run_query"].parameters_json_schema == QUERY_SCHEMA

    def test_a_tool_call_reaches_the_owner_and_its_answer_reaches_the_model(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine(("run_query", {"sql": "SELECT 1"}))
        owner = _Owner(answer=[{"id": 1}])
        task = _hydrated()

        result = _ai(engine, monkeypatch).execute_task(
            task, {"database.query": owner, "tree.children": _Owner()},
        )

        assert owner.calls == [{"sql": "SELECT 1"}]
        assert engine.returned == [[{"id": 1}]]
        assert result.text == "done"
        assert task.status == AITask.COMPLETED

    def test_a_blocked_replay_gives_the_model_the_first_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine(("run_query", {"sql": "SELECT 1"}),
                                 ("run_query", {"sql": "SELECT 1"}))
        owner = _Owner()

        _ai(engine, monkeypatch).execute_task(
            _hydrated(), {"database.query": owner, "tree.children": _Owner()},
        )

        assert len(owner.calls) == 1
        assert engine.returned == [{"rows": 1}, {"rows": 1}]
        assert engine.retried == []

    def test_arguments_that_break_the_schema_are_sent_back_for_correction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine(("run_query", {"query": "SELECT 1"}),
                                 ("run_query", {"sql": "SELECT 1"}))
        owner = _Owner()

        _ai(engine, monkeypatch, corrections=1).execute_task(
            _hydrated(), {"database.query": owner, "tree.children": _Owner()},
        )

        assert len(engine.retried) == 1 and "schema" in engine.retried[0]
        assert owner.calls == [{"sql": "SELECT 1"}]

    def test_an_owner_refusal_is_that_calls_failure_and_the_run_continues(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine(("run_query", {"sql": "DROP TABLE x"}))
        owner = _Owner(refuses="read-only connection")
        task = _hydrated()

        result = _ai(engine, monkeypatch).execute_task(
            task, {"database.query": owner, "tree.children": _Owner()},
        )

        assert result.text == "done"
        assert task.status == AITask.COMPLETED
        assert any("read-only connection" in str(part)
                   for part in engine.returned + engine.retried)

    def test_an_unbound_capability_fails_the_task_before_the_model_is_asked(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _ScriptedEngine()
        task = _hydrated()

        with pytest.raises(AIConfigurationError, match="does not bind"):
            _ai(engine, monkeypatch).execute_task(task, {"database.query": _Owner()})

        assert task.status == AITask.FAILED
        assert engine.seen == []
