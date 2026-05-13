"""
Low-level LLM dispatch utilities.

Provides direct provider calls for callers that do not need the full
RunRequest / RunResponse / ExecutionRecord machinery.  All SDK access
is delegated to the provider abstraction layer.

For ctx-based dispatch (reading provider config from a rey_lib Namespace
application context), use rey_lib.llm.adapters.ask_with_ctx instead.

Public API
----------
direct_ask(prompt, model, provider, api_key, ...)
    Call an LLM directly with explicit credentials and return the response text.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.llm.providers.base import Message
from rey_lib.llm.providers.registry import resolve as resolve_provider

__all__ = ["direct_ask"]


def direct_ask(
    prompt:        str,
    model:         str,
    provider:      str,
    api_key:       str,
    max_tokens:    int           = 4000,
    system_prompt: Optional[str] = None,
    temperature:   float         = 0.0,
) -> str:
    """Call an LLM directly without the full orchestration stack.

    Intended for standalone tools that supply credentials explicitly and do
    not need execution records, retries, or schema validation.  All SDK
    access is delegated to the provider abstraction layer — no SDK is
    imported here.

    Parameters
    ----------
    prompt : str
        User-facing prompt text.
    model : str
        Model identifier (e.g. 'claude-opus-4-5', 'gpt-4o').
    provider : str
        Provider name: 'anthropic', 'openai', 'ollama', or 'mock'.
    api_key : str
        API key for the provider.  Pass an empty string for 'ollama'
        and 'mock'.
    max_tokens : int
        Maximum tokens in the response.
    system_prompt : Optional[str]
        Optional system-level instruction.
    temperature : float
        Sampling temperature.  Defaults to 0.0 for deterministic output.

    Returns
    -------
    str
        LLM response text.

    Raises
    ------
    ConfigurationFailure
        If the provider name is not recognised.
    ProviderFailure
        If the provider API call fails.
    """
    messages: list[Message] = []
    if system_prompt:
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=prompt))

    llm_provider = resolve_provider(provider, api_key=api_key)
    response     = llm_provider.run(
        messages    = messages,
        model       = model,
        max_tokens  = max_tokens,
        temperature = temperature,
    )
    return response.content
