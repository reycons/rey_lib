"""AI executes a hydrated AITask through Claude tooling (backlog 1136).

Deterministic: the SDK's own message types, with ``ClaudeSDKClient`` replaced by
a scripted session, so no Claude CLI is reached. What is asserted is Rey's side:
the engine's execution mechanism selects the run, the CLI is isolated to the
task, a rejected answer is corrected in the same session, the SDK's messages are
told as canonical AIEvents, a withdrawal interrupts the session, and failures
become Rey's own errors.
"""

from __future__ import annotations

import threading
from typing import Any, AsyncIterator, Callable

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (  # noqa: E402 -- after the extra's presence is known
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from rey_lib.ai.ai import AI  # noqa: E402
from rey_lib.ai.content import AIInput  # noqa: E402
from rey_lib.ai.errors import (  # noqa: E402
    AICancelled,
    AIConfigurationError,
    AIExecutionError,
    AIOutputError,
    AIProviderError,
)
from rey_lib.ai.policies import AIExecutionPolicy, ValidationCorrectionPolicy  # noqa: E402
from rey_lib.ai.registry import AIRegistry  # noqa: E402
from rey_lib.ai.requests import AIOutputSpec  # noqa: E402
from rey_lib.ai.streaming import AIEvent, AIEventKind  # noqa: E402
from rey_lib.ai.task import AITask  # noqa: E402
from rey_lib.ai.tools import AITool, AIToolSet  # noqa: E402
from rey_lib.errors.error_utils import AppError  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
}
TOOLS = (
    AITool(name="run_query", description="Run one read query.", input_schema=QUERY_SCHEMA,
           replay_safe=False, capability_key="database.query"),
    AITool(name="list_children", description="List a node's children.",
           input_schema={"type": "object", "properties": {"node": {"type": "string"}}},
           replay_safe=True, capability_key="tree.children"),
)


def _task(**facts: Any) -> AITask:
    given: dict[str, Any] = {
        "task_key": "explain", "profile_key": "claude_subscription",
        "model_identifier": "claude-sonnet-4-6",
        "execution_mechanism": "claude_agent_sdk",
        "instruction_key": "explain_v1", "contract_body": "Explain it.",
        "input": AIInput.prompt("What is this?"),
    }
    given.update(facts)
    return AITask(**given)


def _result(text: str | None = None, structured: Any = None, *, subtype: str = "success",
            is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
        session_id="s", usage={"input_tokens": 3, "output_tokens": 2},
        result=text, structured_output=structured,
    )


Turn = list[Any] | Callable[[Any], AsyncIterator[Any]]


class _Sessions:
    """Stands in for ClaudeSDKClient: each query answered by the next scripted turn."""

    def __init__(self, *turns: Turn, fails: Exception | None = None) -> None:
        self.turns = list(turns)
        self.fails = fails
        self.options: list[Any] = []
        self.queries: list[str] = []
        self.interrupted = threading.Event()

    def client(self, options: Any) -> "_Sessions._Client":
        self.options.append(options)
        return _Sessions._Client(self)

    class _Client:
        def __init__(self, sessions: "_Sessions") -> None:
            self._sessions = sessions

        async def __aenter__(self) -> "_Sessions._Client":
            if self._sessions.fails is not None:
                raise self._sessions.fails
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def query(self, prompt: str) -> None:
            self._sessions.queries.append(prompt)

        async def receive_response(self) -> AsyncIterator[Any]:
            turn = self._sessions.turns.pop(0)
            items = turn(self._sessions) if callable(turn) else turn
            if callable(turn):
                async for item in items:
                    yield item
            else:
                for item in items:
                    yield item

        async def interrupt(self) -> None:
            self._sessions.interrupted.set()


def _ai(sessions: _Sessions, monkeypatch: pytest.MonkeyPatch, corrections: int = 0) -> AI:
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient",
                        lambda options: sessions.client(options))
    return AI(registry=AIRegistry(), policy=AIExecutionPolicy(
        validation_correction=ValidationCorrectionPolicy(max_corrections=corrections),
    ))


class _Owner:
    """Stands in for a canonical owner; counts what reached it."""

    def __init__(self, refuses: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self._refuses = refuses

    def __call__(self, arguments: dict[str, Any], tool: AITool) -> Any:
        self.calls.append(arguments)
        if self._refuses:
            raise AppError(self._refuses)
        return {"rows": len(self.calls)}


class TestSelection:
    def test_the_engine_mechanism_selects_claude_tooling(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions = _Sessions([_result("Hello")])
        task = _task()

        result = _ai(sessions, monkeypatch).execute_task(task)

        assert result.text == "Hello"
        assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)
        assert task.status == AITask.COMPLETED
        assert sessions.queries == ["What is this?"]

    def test_a_mechanism_ai_does_not_run_fails_the_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = _task(execution_mechanism="elsewhere")

        with pytest.raises(AIConfigurationError, match="'elsewhere'"):
            _ai(_Sessions(), monkeypatch).execute_task(task)

        assert task.status == AITask.FAILED

    def test_an_unstated_mechanism_is_pydantic_ai(self) -> None:
        assert AITask(model_identifier="ollama:x").execution_mechanism == "pydantic_ai"

    def test_hydration_carries_the_engine_row_mechanism(self) -> None:
        configuration = [{"row_type": "group", "is_enabled": True,
                          "profile_key": "claude_subscription", "tools": []}]
        engines = [{"profile_key": "claude_subscription", "is_enabled": True,
                    "model_identifier": "claude-sonnet-4-6",
                    "execution_mechanism": "claude_agent_sdk"}]

        task = AITask.hydrate(configuration, engines, [], installation="local",
                              input=AIInput.prompt("x"))

        assert task.execution_mechanism == "claude_agent_sdk"


class TestIsolation:
    def test_the_cli_is_isolated_to_the_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions = _Sessions([_result("Hello")])
        ai = _ai(sessions, monkeypatch)

        ai.execute_task(_task())

        options = sessions.options[0]
        assert options.model == "claude-sonnet-4-6"
        assert options.system_prompt == "Explain it."
        assert (options.tools, options.setting_sources) == ([], [])
        assert (options.mcp_servers, options.allowed_tools) == ({}, [])
        assert options.max_turns == ai._policy.budget.max_turns
        assert options.output_format is None
        assert options.include_partial_messages is False
        assert options.env == {}


class TestStructuredAndCorrection:
    def test_structured_output_is_asked_for_and_corrected_in_the_same_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions = _Sessions([_result(structured={"wrong": 1})],
                             [_result(structured={"answer": "ready"})])

        result = _ai(sessions, monkeypatch, corrections=1).execute_task(
            _task(output=AIOutputSpec.schema_of(SCHEMA)),
        )

        assert result.value == {"answer": "ready"}
        assert len(sessions.options) == 1
        assert sessions.options[0].output_format == {"type": "json_schema", "schema": SCHEMA}
        assert len(sessions.queries) == 2
        assert sessions.queries[1] and sessions.queries[1] != "What is this?"
        assert (result.usage.input_tokens, result.usage.output_tokens) == (6, 4)

    def test_corrections_are_bounded_by_the_allowance(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions = _Sessions([_result(structured={"wrong": 1})],
                             [_result(structured={"wrong": 2})])
        task = _task(output=AIOutputSpec.schema_of(SCHEMA))

        with pytest.raises(AIOutputError, match="satisfies its contract"):
            _ai(sessions, monkeypatch, corrections=1).execute_task(task)

        assert len(sessions.queries) == 2
        assert task.status == AITask.FAILED


class TestEvents:
    def test_sdk_messages_are_told_as_canonical_events(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        turn = [
            StreamEvent(uuid="u", session_id="s", event={
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"},
            }),
            AssistantMessage(content=[
                TextBlock("Hel"),
                ToolUseBlock(id="t1", name="mcp__rey__run_query", input={"sql": "select 1"}),
            ], model="claude-sonnet-4-6"),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="1 row")]),
            RateLimitEvent(rate_limit_info=RateLimitInfo(
                status="allowed_warning", resets_at=1700000000, rate_limit_type="five_hour",
            ), uuid="u", session_id="s"),
            _result("Hello"),
        ]
        sessions = _Sessions(turn)
        events: list[AIEvent] = []

        _ai(sessions, monkeypatch).execute_task(_task(), observe=events.append)

        assert sessions.options[0].include_partial_messages is True
        assert [event.kind for event in events] == [
            AIEventKind.EXECUTION_STARTED, AIEventKind.CONTENT_DELTA,
            AIEventKind.TOOL_CALL_REQUESTED, AIEventKind.TOOL_RESULT_ACCEPTED,
            AIEventKind.RATE_LIMITED, AIEventKind.EXECUTION_COMPLETED,
        ]
        assert events[1].text == "Hel"
        assert events[2].tool_call.name == "run_query"
        assert events[2].tool_call.arguments == {"sql": "select 1"}
        assert (events[3].tool_result.call_id, events[3].tool_result.failed) == ("t1", False)
        assert events[4].metadata == {"status": "allowed_warning", "resets_at": 1700000000,
                                      "rate_limit_type": "five_hour"}
        assert events[4].projection()["kind"] == "rate_limited"


class TestTools:
    def test_the_task_tools_are_served_in_process_and_only_they_are_allowed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions = _Sessions([_result("done")])
        owner = _Owner()

        _ai(sessions, monkeypatch).execute_task(
            _task(tools=TOOLS), {"database.query": owner, "tree.children": owner},
        )

        options = sessions.options[0]
        assert set(options.mcp_servers) == {"rey"}
        assert options.allowed_tools == ["mcp__rey__run_query", "mcp__rey__list_children"]

    def test_a_call_runs_through_invoke_and_is_not_repeated(self) -> None:
        import asyncio

        owner = _Owner()
        run_query = {tool.name: tool for tool in AIToolSet(
            TOOLS, {"database.query": owner, "tree.children": owner},
        ).sdk_tools()}["run_query"]

        first = asyncio.run(run_query.handler({"sql": "select 1"}))
        again = asyncio.run(run_query.handler({"sql": "select 1"}))

        assert first == again == {"content": [{"type": "text", "text": '{"rows": 1}'}]}
        assert owner.calls == [{"sql": "select 1"}]
        assert run_query.input_schema == QUERY_SCHEMA

    def test_broken_arguments_and_refusals_are_error_results(self) -> None:
        import asyncio

        tools = {tool.name: tool for tool in AIToolSet(
            TOOLS, {"database.query": _Owner(refuses="not allowed"), "tree.children": _Owner()},
        ).sdk_tools()}

        broken = asyncio.run(tools["run_query"].handler({}))
        refused = asyncio.run(tools["run_query"].handler({"sql": "drop"}))

        assert broken["is_error"] is True
        assert refused == {"content": [{"type": "text", "text": "not allowed"}], "is_error": True}


class TestCancellation:
    def test_a_withdrawal_interrupts_the_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        under_way = threading.Event()

        async def waiting(sessions: _Sessions) -> AsyncIterator[Any]:
            import asyncio

            under_way.set()
            while not sessions.interrupted.is_set():
                await asyncio.sleep(0.01)
            yield _result(subtype="error_during_execution", is_error=True)

        sessions = _Sessions(waiting)
        ai, task = _ai(sessions, monkeypatch), _task()
        signalled: list[bool] = []

        def cancel() -> None:
            under_way.wait(5)
            signalled.append(ai.cancel_task(task.id))

        canceller = threading.Thread(target=cancel, daemon=True)
        canceller.start()
        events: list[AIEvent] = []

        with pytest.raises(AICancelled, match="was cancelled"):
            ai.execute_task(task, observe=events.append)
        canceller.join(5)

        assert signalled == [True]
        assert sessions.interrupted.is_set()
        assert task.status == AITask.CANCELLED
        assert events[-1].kind is AIEventKind.EXECUTION_CANCELLED
        assert AIEventKind.EXECUTION_FAILED not in [event.kind for event in events]


class TestFailures:
    @pytest.mark.parametrize(("turn", "error"), [
        ([_result(subtype="error_max_turns", is_error=True)], AIExecutionError),
        ([_result(subtype="error_during_execution", is_error=True)], AIProviderError),
        ([], AIProviderError),
    ])
    def test_an_unsuccessful_session_is_a_rey_error(
        self, monkeypatch: pytest.MonkeyPatch, turn: list[Any], error: type[Exception],
    ) -> None:
        task = _task()

        with pytest.raises(error):
            _ai(_Sessions(turn), monkeypatch).execute_task(task)

        assert task.status == AITask.FAILED

    @pytest.mark.parametrize(("fails", "error"), [
        (CLINotFoundError("no claude"), AIConfigurationError),
        (ProcessError("exited", exit_code=1), AIProviderError),
    ])
    def test_a_cli_that_cannot_run_is_a_rey_error(
        self, monkeypatch: pytest.MonkeyPatch, fails: Exception, error: type[Exception],
    ) -> None:
        task = _task()

        with pytest.raises(error):
            _ai(_Sessions(fails=fails), monkeypatch).execute_task(task)

        assert task.status == AITask.FAILED
