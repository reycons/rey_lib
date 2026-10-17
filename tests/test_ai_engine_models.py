"""AI builds a usable Pydantic AI model from each governed engine configuration.

Backlog 1019 (ai.redesign.ai_engine_models). The configurations are the governed
ai.ai_engine_profile values (model_identifier, base_url) approved 2026-10-17.
Deterministic: building a model opens no connection, so no engine is reached and
no live service is needed -- that is the live integration check, run on the Mac.

Only the public model contract is asserted (a Pydantic AI ``Model`` with the
configured model name and endpoint), not which internal class Pydantic AI chose,
so Rey is not bound to Pydantic AI internals.
"""

from __future__ import annotations

import pytest
from pydantic_ai.models import Model

from rey_lib.ai.ai import AI
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.registry import AIRegistry

#: The test's own environment variable, declared to the resolver as TEST_KEY.
KEY_VARIABLE = "REY_AI_ENGINE_MODELS_TEST_KEY"
KEY_REFERENCE = "env.TEST_KEY"
OLLAMA = "http://localhost:11434/v1"

#: (model_identifier, base_url, credential_ref) for every governed engine.
ENGINES = [
    ("anthropic:claude-sonnet-4-6", "", KEY_REFERENCE),
    ("google:gemini-3.5-flash", "", KEY_REFERENCE),
    ("openai-chat:gpt-4o", "", KEY_REFERENCE),
    ("ollama:qwen2.5-coder:32b", OLLAMA, ""),
    ("ollama:qwen2.5-coder:14b", OLLAMA, ""),
    ("ollama:qwen2.5:72b-instruct", OLLAMA, ""),
    ("ollama:deepseek-r1:70b", OLLAMA, ""),
    ("ollama:qwen3-coder-next:latest", OLLAMA, ""),
]


@pytest.fixture
def ai(monkeypatch: pytest.MonkeyPatch) -> AI:
    """An AI holding the credential resolver construction would hand it."""
    monkeypatch.setenv(KEY_VARIABLE, "test-key-not-a-secret")
    return AI(
        registry=AIRegistry(),
        credentials=CredentialResolver({"TEST_KEY": KEY_VARIABLE}),
    )


@pytest.mark.parametrize(("identifier", "base_url", "credential_ref"), ENGINES,
                         ids=[engine[0] for engine in ENGINES])
def test_a_governed_engine_resolves_to_a_usable_model(
    ai: AI, identifier: str, base_url: str, credential_ref: str,
) -> None:
    model = ai._model(identifier, base_url=base_url, credential_ref=credential_ref)

    assert isinstance(model, Model)
    assert model.model_name == identifier.split(":", 1)[1]
    if base_url:
        assert str(model.base_url).rstrip("/") == base_url.rstrip("/")


def test_an_engine_pydantic_ai_cannot_resolve_is_a_configuration_error(ai: AI) -> None:
    """A provider name outside Pydantic AI's vocabulary fails as Rey's own error."""
    with pytest.raises(AIConfigurationError, match="no-such-provider:some-model"):
        ai._model("no-such-provider:some-model", credential_ref=KEY_REFERENCE)


def test_an_identifier_without_a_provider_is_a_configuration_error(ai: AI) -> None:
    with pytest.raises(AIConfigurationError, match="gpt-4o"):
        ai._model("gpt-4o", credential_ref=KEY_REFERENCE)


def test_an_unset_credential_is_reported_where_the_model_is_built(
    ai: AI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_VARIABLE)

    with pytest.raises(AIConfigurationError, match="not set"):
        ai._model("anthropic:claude-sonnet-4-6", credential_ref=KEY_REFERENCE)
