"""The governed query tool: AITool -> query operation -> DatabaseQuery.run (backlog 741).

Deterministic: no database and no engine. DatabaseQuery.run is replaced by a
recorder answering StatementResults, connections are stand-ins exposing
handle(), and Pydantic AI's FunctionModel calls the tool as a model would.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError, AIToolError
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AITool, AIToolSet
from rey_lib.config import bootstrap
from rey_lib.db import database_query
from rey_lib.db.database_query import StatementResult
from rey_lib.errors.error_utils import ConfigError

QUERY_ROW = {
    "tool_key": "query", "description": "Run one statement.",
    "input_schema": {"type": "object",
                     "properties": {"sql": {"type": "string", "maxLength": 20000}},
                     "required": ["sql"], "additionalProperties": False},
    "replay_safe": False, "capability_key": "database.query", "result_row_limit": 1000,
}
ENGINES = [{"profile_key": "local_testing", "is_enabled": True,
            "model_identifier": "ollama:qwen3-coder-next:latest",
            "base_url": "http://localhost:11434/v1"}]


def _configuration(connection: str | None) -> list[dict[str, Any]]:
    return [
        {"row_type": "group", "is_enabled": True, "profile_key": "local_testing",
         "instruction_key": None, "temperature": None, "representation": None,
         "composed_first": False, "connection_name": None, "tools": []},
        {"row_type": "task", "is_enabled": True, "task_key": "search_index",
         "task_label": "Search", "profile_key": "local_testing", "instruction_key": None,
         "temperature": None, "representation": None, "composed_first": False,
         "connection_name": connection, "tools": [QUERY_ROW]},
    ]


def _task(connection: str | None = "llm_reader") -> AITask:
    return AITask.hydrate(_configuration(connection), ENGINES, [], installation="local",
                          task_key="search_index", input=AIInput.prompt("Count them."))


class _Connection:
    """The database layer's Connection, as far as the operation uses it."""

    def __init__(self, name: str) -> None:
        self.handle_of = f"handle:{name}"

    def handle(self) -> str:
        return self.handle_of


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """DatabaseQuery.run, recording what reached it and answering one result."""
    seen: list[tuple] = []

    def run(self: Any, conn: Any, *, limit: int, adapter: Any = None) -> tuple:
        seen.append((self.statement, conn, limit))
        return (StatementResult(columns=["day"], rows=[{"day": date(2026, 10, 17)}],
                                row_count=None),)

    monkeypatch.setattr(database_query.DatabaseQuery, "run", run)
    return seen


@pytest.fixture
def operations(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Bootstrap's composition over two configured connections, one offered."""
    monkeypatch.setattr(bootstrap, "build_connections", lambda ctx: {
        "llm_reader": _Connection("llm_reader"), "warehouse": _Connection("warehouse"),
    })
    return bootstrap._ai_query_operations(object(), ("llm_reader",))


class TestGovernedFact:
    def test_the_limit_hydrates_into_the_tool(self) -> None:
        tool = _task().tools[0]

        assert (tool.name, tool.capability_key, tool.result_row_limit) == (
            "query", "database.query", 1000,
        )
        assert tool.replay_safe is False

    def test_the_text_bound_is_the_tools_governed_schema(self, operations: dict) -> None:
        toolset = AIToolSet(_task().tools, operations["llm_reader"])

        with pytest.raises(AIToolError, match="schema"):
            toolset.invoke("query", {"sql": "x" * 20001})


class TestComposition:
    def test_only_offered_connections_get_an_operation(self, operations: dict) -> None:
        assert set(operations) == {"llm_reader"}
        assert set(operations["llm_reader"]) == {"database.query"}

    def test_the_governed_limit_goes_straight_to_database_query(
        self, operations: dict, ran: list,
    ) -> None:
        answer = operations["llm_reader"]["database.query"](
            {"sql": "the statement"}, _task().tools[0],
        )

        assert ran == [("the statement", "handle:llm_reader", 1000)]
        assert answer == {
            "results": [{"columns": ["day"], "rows": [{"day": "2026-10-17"}],
                         "row_count": None, "at_row_limit": False}],
            "row_limit": 1000,
        }

    def test_the_incomplete_result_flag_derives_from_the_same_limit(
        self, operations: dict, ran: list,
    ) -> None:
        one_row = AITool(name="query", capability_key="database.query", result_row_limit=1)

        answer = operations["llm_reader"]["database.query"]({"sql": "the statement"}, one_row)

        assert ran[0][2] == 1
        assert answer["results"][0]["at_row_limit"] is True
        assert answer["row_limit"] == 1

    def test_a_tool_stating_no_limit_is_refused(self, operations: dict, ran: list) -> None:
        unlimited = AITool(name="query", capability_key="database.query")

        with pytest.raises(ConfigError, match="result_row_limit"):
            operations["llm_reader"]["database.query"]({"sql": "the statement"}, unlimited)
        assert ran == []


class _Engine:
    def __init__(self) -> None:
        self._calls = [("query", {"sql": "the statement"})]
        self.returned: list[Any] = []
        self.seen: list[AgentInfo] = []
        self.model = FunctionModel(self._turn)

    def _turn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(info)
        if isinstance(messages[-1], ModelRequest):
            self.returned += [p.content for p in messages[-1].parts
                              if isinstance(p, ToolReturnPart)]
        if self._calls:
            return ModelResponse(parts=[ToolCallPart(*self._calls.pop(0))])
        return ModelResponse(parts=[TextPart("one day")])


def _ai(engine: _Engine, monkeypatch: pytest.MonkeyPatch, operations: dict) -> AI:
    ai = AI(registry=AIRegistry(), connection_operations=operations)
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: engine.model)
    return ai


class TestThroughAI:
    def test_the_tasks_connection_selects_the_operation(
        self, monkeypatch: pytest.MonkeyPatch, operations: dict, ran: list,
    ) -> None:
        engine = _Engine()
        task = _task("llm_reader")

        result = _ai(engine, monkeypatch, operations).execute_task(task)

        assert ran == [("the statement", "handle:llm_reader", 1000)]
        assert engine.returned[0]["results"][0]["rows"] == [{"day": "2026-10-17"}]
        assert result.text == "one day"
        assert task.status == AITask.COMPLETED

    def test_a_task_without_a_connection_cannot_bind_the_query_tool(
        self, monkeypatch: pytest.MonkeyPatch, operations: dict, ran: list,
    ) -> None:
        engine = _Engine()
        task = _task(None)

        with pytest.raises(AIConfigurationError, match="database.query"):
            _ai(engine, monkeypatch, operations).execute_task(task)
        assert ran == [] and engine.seen == []
