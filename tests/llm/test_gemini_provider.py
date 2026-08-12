"""Focused Google Gemini provider contract coverage.

The SDK is faked through sys.modules exactly as the Ollama tests fake theirs,
so these run without google-genai installed and assert Rey's normalisation
rather than Google's client behaviour.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.llm.exceptions import (
    CancellationFailure,
    ConfigurationFailure,
    ProviderFailure,
    RateLimitFailure,
    TimeoutFailure,
)
from rey_lib.llm.providers import registry
from rey_lib.llm.providers.base import Message
from rey_lib.llm.providers.gemini import GeminiProvider


class _APIError(Exception):
    """Stands in for google.genai.errors.APIError."""

    def __init__(self, code: int, message: str = "failed") -> None:
        super().__init__(message)
        self.code = code


class _ClientError(_APIError):
    """Stands in for google.genai.errors.ClientError (4xx)."""


class _ServerError(_APIError):
    """Stands in for google.genai.errors.ServerError (5xx)."""


class _ClosableStream:
    """A stream that records whether it was closed, as a generator would be."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self) -> "_ClosableStream":
        return self

    def __next__(self) -> Any:
        return next(self._chunks)

    def close(self) -> None:
        self.closed = True


def _usage(prompt: int, candidates: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=candidates
    )


def _install_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generate_content: Any = None,
    generate_content_stream: Any = None,
    record: dict[str, Any] | None = None,
) -> None:
    """Install a fake google.genai package for the duration of one test."""
    models = SimpleNamespace(
        generate_content=generate_content,
        generate_content_stream=generate_content_stream,
    )

    def _client(**kwargs: Any) -> SimpleNamespace:
        if record is not None:
            record["client_kwargs"] = kwargs
        return SimpleNamespace(models=models)

    errors_module = SimpleNamespace(
        APIError=_APIError, ClientError=_ClientError, ServerError=_ServerError
    )
    genai_module = SimpleNamespace(Client=_client, errors=errors_module)
    google_module = SimpleNamespace(genai=genai_module)

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_module)


# ---------------------------------------------------------------------------
# Registry and configuration
# ---------------------------------------------------------------------------


def test_registry_resolves_gemini() -> None:
    provider = registry.resolve("gemini", api_key="key")
    assert isinstance(provider, GeminiProvider)


def test_registry_resolution_is_case_insensitive() -> None:
    assert isinstance(registry.resolve("GEMINI", api_key="key"), GeminiProvider)


def test_gemini_appears_in_the_unknown_provider_vocabulary() -> None:
    """An unknown name lists gemini among the built-ins a caller may choose."""
    with pytest.raises(ConfigurationFailure, match="gemini"):
        registry.resolve("not_a_provider", api_key="key")


def test_missing_credentials_fail_as_a_provider_failure() -> None:
    with pytest.raises(ProviderFailure, match="api_key is required"):
        GeminiProvider(api_key="")


def test_capabilities_are_declared_without_claiming_false_parity() -> None:
    capabilities = GeminiProvider(api_key="key").capabilities
    assert capabilities.supports_streaming is True
    assert capabilities.supports_system_messages is True
    # Gemini genuinely offers structured output, unlike Anthropic's declaration.
    assert capabilities.supports_json_mode is True
    assert capabilities.max_context_tokens == 1_048_576


def test_the_sdk_is_only_required_when_the_provider_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing SDK is a provider failure naming the install, not an ImportError."""
    for name in ("google", "google.genai", "google.genai.errors"):
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(ProviderFailure, match="google-genai package is not installed"):
        GeminiProvider(api_key="key").run([Message(role="user", content="x")], "m")


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


def test_non_streaming_normalizes_into_the_shared_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _generate(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            text="  hello world  ",
            usage_metadata=_usage(11, 4),
            model_version="gemini-2.5-flash-001",
        )

    _install_sdk(monkeypatch, generate_content=_generate, record=captured)

    result = GeminiProvider(api_key="key").run(
        [
            Message(role="system", content="be terse"),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ],
        model="gemini-2.5-flash",
        max_tokens=256,
        temperature=0.25,
    )

    assert result.content == "hello world"
    assert result.tokens_in == 11
    assert result.tokens_out == 4
    assert result.model == "gemini-2.5-flash-001"
    assert result.raw == {
        "model": "gemini-2.5-flash-001",
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }
    # The API key reaches the client and never the request.
    assert captured["client_kwargs"] == {"api_key": "key"}


def test_system_messages_become_a_system_instruction_and_roles_are_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini has no 'assistant' role and takes the system prompt separately."""
    captured: dict[str, Any] = {}

    def _generate(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(text="ok", usage_metadata=_usage(1, 1))

    _install_sdk(monkeypatch, generate_content=_generate)

    GeminiProvider(api_key="key").run(
        [
            Message(role="system", content="first rule"),
            Message(role="system", content="second rule"),
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
        ],
        model="gemini-2.5-flash",
        max_tokens=64,
        temperature=0.5,
    )

    assert captured["contents"] == [
        {"role": "user", "parts": [{"text": "question"}]},
        {"role": "model", "parts": [{"text": "answer"}]},
    ]
    assert captured["config"] == {
        "max_output_tokens": 64,
        "temperature": 0.5,
        "system_instruction": "first rule\n\nsecond rule",
    }


def test_a_response_without_usage_reports_zero_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is the same answer every other Rey provider gives for absent usage."""
    _install_sdk(
        monkeypatch,
        generate_content=lambda **_kwargs: SimpleNamespace(text="ok", usage_metadata=None),
    )

    result = GeminiProvider(api_key="key").run(
        [Message(role="user", content="x")], model="gemini-2.5-flash"
    )
    assert (result.tokens_in, result.tokens_out) == (0, 0)
    # An absent model_version falls back to the requested model.
    assert result.model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_streaming_emits_ordered_text_deltas_matching_the_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ClosableStream(
        [
            SimpleNamespace(text="Hello ", usage_metadata=None, model_version=""),
            SimpleNamespace(text="strange ", usage_metadata=None, model_version=""),
            SimpleNamespace(
                text="world",
                usage_metadata=_usage(7, 3),
                model_version="gemini-2.5-pro-001",
            ),
        ]
    )
    _install_sdk(monkeypatch, generate_content_stream=lambda **_kwargs: stream)
    seen: list[str] = []

    result = GeminiProvider(api_key="key").run(
        [Message(role="user", content="x")],
        model="gemini-2.5-pro",
        on_chunk=seen.append,
    )

    assert seen == ["Hello ", "strange ", "world"]
    assert result.content == "".join(seen).strip()
    assert result.content == "Hello strange world"
    # Usage arrives on a later chunk, and the last reported counts win.
    assert (result.tokens_in, result.tokens_out) == (7, 3)
    assert result.model == "gemini-2.5-pro-001"
    assert stream.closed is True


def test_streaming_skips_empty_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk carrying only metadata is not reported as an empty delta."""
    stream = _ClosableStream(
        [
            SimpleNamespace(text="", usage_metadata=None, model_version=""),
            SimpleNamespace(text="only", usage_metadata=_usage(2, 1), model_version=""),
            SimpleNamespace(text=None, usage_metadata=None, model_version=""),
        ]
    )
    _install_sdk(monkeypatch, generate_content_stream=lambda **_kwargs: stream)
    seen: list[str] = []

    result = GeminiProvider(api_key="key").run(
        [Message(role="user", content="x")], model="m", on_chunk=seen.append
    )

    assert seen == ["only"]
    assert result.content == "only"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancellation_before_the_call_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _generate(**_kwargs: Any) -> SimpleNamespace:
        raise AssertionError("a cancelled run must not call the provider")

    _install_sdk(monkeypatch, generate_content=_generate)

    with pytest.raises(CancellationFailure):
        GeminiProvider(api_key="key").run(
            [Message(role="user", content="x")], model="m", cancelled=lambda: True
        )


def test_cancellation_mid_stream_stops_and_closes_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is cooperative: it is checked per chunk, as elsewhere in Rey."""
    stream = _ClosableStream(
        [
            SimpleNamespace(text="first", usage_metadata=None, model_version=""),
            SimpleNamespace(text="second", usage_metadata=None, model_version=""),
        ]
    )
    _install_sdk(monkeypatch, generate_content_stream=lambda **_kwargs: stream)
    seen: list[str] = []
    state = {"cancel": False}

    def _receive(text: str) -> None:
        # Cancel as soon as the first delta has been delivered, so the second
        # chunk is the one the loop refuses.
        seen.append(text)
        state["cancel"] = True

    with pytest.raises(CancellationFailure):
        GeminiProvider(api_key="key").run(
            [Message(role="user", content="x")],
            model="m",
            on_chunk=_receive,
            cancelled=lambda: state["cancel"],
        )

    assert seen == ["first"]
    assert stream.closed is True


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_ClientError(429), RateLimitFailure),
        (_ClientError(408), TimeoutFailure),
        (_ClientError(400), ProviderFailure),
        (_ClientError(403), ProviderFailure),
        (_ServerError(504), TimeoutFailure),
        (_ServerError(500), ProviderFailure),
        (_APIError(0), ProviderFailure),
    ],
)
def test_provider_errors_map_into_the_rey_hierarchy(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type
) -> None:
    def _generate(**_kwargs: Any) -> SimpleNamespace:
        raise error

    _install_sdk(monkeypatch, generate_content=_generate)

    with pytest.raises(expected):
        GeminiProvider(api_key="key").run([Message(role="user", content="x")], model="m")


def test_rate_limit_and_timeout_remain_provider_failures() -> None:
    """The hierarchy the retry policy keys on is unchanged by this provider."""
    assert issubclass(RateLimitFailure, ProviderFailure)
    assert issubclass(TimeoutFailure, ProviderFailure)
