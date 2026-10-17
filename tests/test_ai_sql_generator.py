"""SQLGenerator: natural-language -> SQL through its governed task (backlog 1024).

Deterministic: Control is a stand-in for the three governed reads, the engine is
Pydantic AI's FunctionModel scripted to use the catalog tools, and the catalog
owner is DatabaseSchema with its two reads replaced by recorders -- what is
proved is that SQLGenerator delegates to it, not how it reads a database.
"""

from __future__ import annotations

from types import SimpleNamespace
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
from rey_lib.ai.errors import AIConfigurationError, AIRequestError
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.sql_generator import SQLGenerator
from rey_lib.db import database_objects
from rey_lib.db import database_query

CATALOG_TOOLS = [
    {"tool_key": "list_schemas", "description": "List the schemas.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
     "replay_safe": True, "capability_key": "database.schemas"},
    {"tool_key": "read_schema", "description": "Read one schema.",
     "input_schema": {"type": "object", "properties": {"schema": {"type": "string"}},
                      "required": ["schema"], "additionalProperties": False},
     "replay_safe": True, "capability_key": "database.schema"},
]

#: What the reader sends and what the scripted engine answers. Opaque text to
#: SQLGenerator, which only passes it through.
REQUEST = "/* AI: count orders per customer */"
GENERATED = "/* AI: count orders per customer */ -- generated statement"
#: A stand-in for an object's DDL, which must never reach the model.
DDL_MARKER = "the table's own DDL"


class _Control:
    """The three governed reads, as the extended routines return them."""

    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self._tools = CATALOG_TOOLS if tools is None else tools

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        return [
            {"row_type": "group", "is_enabled": True, "profile_key": "local_testing",
             "instruction_key": None, "temperature": None, "representation": None,
             "composed_first": False, "connection_name": None, "tools": []},
            {"row_type": "task", "is_enabled": True, "task_key": "generate_sql",
             "task_label": "Generate SQL", "profile_key": "local_precision",
             "instruction_key": "sql_generate_sql", "temperature": 0,
             "representation": None, "composed_first": False, "connection_name": None,
             "tools": self._tools},
        ]

    def ai_engine_profiles(self) -> list[dict[str, Any]]:
        return [{"profile_key": "local_precision", "is_enabled": True,
                 "model_identifier": "ollama:qwen2.5-coder:32b",
                 "base_url": "http://localhost:11434/v1"}]

    def ai_instructions(self) -> list[dict[str, Any]]:
        return [{"instruction_key": "sql_generate_sql", "kind": "contract",
                 "contract_key": "sql_generate_sql", "contract_version": "2.0.0",
                 "body": "Return the statement itself, as plain SQL."}]


class _Engine:
    """Lists the schemas, reads one, then answers with the generated SQL."""

    def __init__(self) -> None:
        self._calls = [("list_schemas", {}), ("read_schema", {"schema": "public"})]
        self.returned: list[Any] = []
        self.seen: list[AgentInfo] = []
        self.model = FunctionModel(self._turn)

    def _turn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(info)
        if isinstance(messages[-1], ModelRequest):
            self.returned += [part.content for part in messages[-1].parts
                              if isinstance(part, ToolReturnPart)]
        if self._calls:
            return ModelResponse(parts=[ToolCallPart(*self._calls.pop(0))])
        return ModelResponse(parts=[TextPart(GENERATED)])


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """DatabaseSchema's reads, replaced by recorders answering one schema."""
    asked: list[tuple] = []
    orders = SimpleNamespace(
        identity=SimpleNamespace(name="orders"), ddl=DDL_MARKER,
        columns=[SimpleNamespace(name="customer_id", type="integer", nullable=False,
                                 ddl=DDL_MARKER)],
    )

    def listed(conn: Any, connection: str, adapter: Any = None) -> tuple:
        asked.append(("list", conn, connection))
        return (SimpleNamespace(schema="public"),)

    def read(conn: Any, connection: str, catalog_name: str, schema: str,
             adapter: Any = None) -> SimpleNamespace:
        asked.append(("read", conn, connection, schema))
        return SimpleNamespace(schema=schema, tables=[orders], views=[])

    monkeypatch.setattr(database_objects.DatabaseSchema, "list", listed)
    monkeypatch.setattr(database_objects.DatabaseSchema, "read", read)
    return asked


@pytest.fixture
def nothing_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """DatabaseQuery's ways in fail loudly: SQLGenerator must use none of them."""
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SQLGenerator executed SQL")

    for name in ("rows", "run", "page", "read"):
        monkeypatch.setattr(database_query.DatabaseQuery, name, refuse)


def _generator(engine: _Engine, monkeypatch: pytest.MonkeyPatch,
               control: _Control | None = None) -> tuple[SQLGenerator, list[tuple]]:
    # Composed as bootstrap composes it: with the connections whose own
    # declaration lets a language model use them (backlog 1049, D-a).
    ai = AI(registry=AIRegistry(), connections=("warehouse",))
    built: list[tuple] = []

    def model(*engine_facts: Any) -> FunctionModel:
        built.append(engine_facts)
        return engine.model

    monkeypatch.setattr(ai, "_model", model)
    return SQLGenerator(ai, control or _Control(), "local"), built


class TestGeneration:
    def test_it_returns_the_generated_sql_without_executing_it(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list, nothing_executes: None,
    ) -> None:
        engine = _Engine()
        generator, _ = _generator(engine, monkeypatch)

        sql = generator.generate(REQUEST, connection="warehouse", conn="open-conn")

        assert sql == GENERATED

    def test_it_runs_its_governed_task_engine_and_contract(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list, nothing_executes: None,
    ) -> None:
        engine = _Engine()
        generator, built = _generator(engine, monkeypatch)

        generator.generate(REQUEST, connection="warehouse", conn="open-conn")

        assert built[0][0] == "ollama:qwen2.5-coder:32b"
        assert engine.seen[0].instructions == "Return the statement itself, as plain SQL."
        assert {tool.name for tool in engine.seen[0].function_tools} == {
            "list_schemas", "read_schema",
        }

    def test_the_catalog_is_read_through_its_owner_on_the_callers_connection(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list, nothing_executes: None,
    ) -> None:
        engine = _Engine()
        generator, _ = _generator(engine, monkeypatch)

        generator.generate(REQUEST, connection="warehouse", conn="open-conn")

        assert catalog == [("list", "open-conn", "warehouse"),
                           ("read", "open-conn", "warehouse", "public")]

    def test_the_model_receives_names_and_columns_and_no_ddl(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list, nothing_executes: None,
    ) -> None:
        engine = _Engine()
        generator, _ = _generator(engine, monkeypatch)

        generator.generate(REQUEST, connection="warehouse", conn="open-conn")

        assert engine.returned[0] == ["public"]
        assert engine.returned[1] == {
            "schema": "public",
            "tables": [{"name": "orders", "columns": [
                {"name": "customer_id", "type": "integer", "nullable": False}]}],
            "views": [],
        }
        assert DDL_MARKER not in str(engine.returned)


class TestRefusals:
    def test_nothing_to_generate_from_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        generator, _ = _generator(_Engine(), monkeypatch)

        with pytest.raises(AIRequestError):
            generator.generate("  ", connection="warehouse", conn="open-conn")

    def test_a_connection_a_model_may_not_use_is_refused_before_any_call(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list,
    ) -> None:
        engine = _Engine()
        generator, _ = _generator(engine, monkeypatch)

        with pytest.raises(AIRequestError, match="'payroll' is not one a language model"):
            generator.generate(REQUEST, connection="payroll", conn="open-conn")
        assert engine.seen == []
        assert catalog == []

    def test_a_tool_whose_capability_it_does_not_bind_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, catalog: list,
    ) -> None:
        engine = _Engine()
        unbound = [*CATALOG_TOOLS, {
            "tool_key": "drop_table", "description": "", "input_schema": {"type": "object"},
            "replay_safe": False, "capability_key": "database.drop",
        }]
        generator, _ = _generator(engine, monkeypatch, control=_Control(unbound))

        with pytest.raises(AIConfigurationError, match="database.drop"):
            generator.generate(REQUEST, connection="warehouse", conn="open-conn")
        assert engine.seen == []
