"""
LLM dispatch utilities.

All provider calls route through the provider abstraction layer so no
application code ever imports an SDK directly.

Supports Anthropic (Claude) and OpenAI (GPT) via a common ask() interface.
The provider is selected by the llm argument, which is resolved against the
ctx.llm config.  All credentials come from ctx — no hardcoded keys.

Public API
----------
default_llm(ctx)
    Return the name of the default LLM instance from ctx.llm config.
ask(ctx, prompt, llm, max_tokens, system_prompt)
    Dispatch a prompt to the selected LLM provider and return the response.
direct_ask(prompt, model, provider, api_key, ...)
    Call an LLM directly without an app context.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.config.config_utils import Namespace
from rey_lib.llm.exceptions import ConfigurationFailure
from rey_lib.llm.providers.base import Message
from rey_lib.llm.providers.registry import resolve as resolve_provider

__all__ = [
    "default_llm",
    "ask",
    "direct_ask",
]


def direct_ask(
    prompt:        str,
    model:         str,
    provider:      str,
    api_key:       str,
    max_tokens:    int           = 4000,
    system_prompt: Optional[str] = None,
    temperature:   float         = 0.0,
) -> str:
    """Call an LLM directly without an app context.

    Intended for standalone tools that have no app ctx available.
    Credentials and model are supplied explicitly rather than read from ctx.
    All SDK access is delegated to the provider abstraction layer.

    Parameters
    ----------
    prompt : str
        User-facing prompt text to send.
    model : str
        Model identifier (e.g. 'claude-opus-4-5', 'gpt-4o').
    provider : str
        Provider name: 'anthropic' or 'openai'.
    api_key : str
        API key for the provider.
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


def default_llm(ctx: Namespace) -> str:
    """Return the name of the default LLM instance from ctx.llm config.

    Parameters
    ----------
    ctx : Namespace
        Application context with a populated ctx.llm mapping.

    Returns
    -------
    str
        The key of the default LLM config entry, or 'claude' as a fallback.
    """
    return next(
        (name for name, cfg in ctx.llm.items() if cfg.get("default")),
        "claude",
    )


def ask(
    ctx:           Namespace,
    prompt:        str,
    llm:           str,
    max_tokens:    int           = 450,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Dispatch a prompt to the selected LLM provider and return the response.

    The provider and model are resolved from ctx.llm[llm] so dispatch never
    depends on hardcoded strings.  All SDK access is delegated to the provider
    abstraction layer.

    Parameters
    ----------
    ctx : Namespace
        Application context. ctx.llm must contain the named instance config.
    prompt : str
        User-facing prompt text to send.
    llm : str
        LLM instance name from config (e.g. 'claude', 'gpt4o').
    max_tokens : int
        Maximum tokens in the response.
    system_prompt : Optional[str]
        Optional system-level instruction passed separately from the user
        prompt.

    Returns
    -------
    str
        LLM response text.

    Raises
    ------
    ConfigurationFailure
        If the LLM instance is not found in ctx or the provider is unknown.
    ProviderFailure
        If the provider API call fails.
    """
    if llm not in ctx.llm:
        raise ConfigurationFailure(
            f"LLM instance '{llm}' not found in ctx.llm. "
            "Check your config/llm/*.yaml files."
        )

    llm_cfg  = ctx.llm[llm]
    provider = llm_cfg.provider.lower()
    model    = llm_cfg.model
    api_key  = llm_cfg.get("api_key", "")

    if not api_key:
        raise ConfigurationFailure(
            f"API key not set for LLM instance '{llm}'. "
            "Check your .env file."
        )

    messages: list[Message] = []
    if system_prompt:
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=prompt))

    llm_provider = resolve_provider(provider, api_key=api_key)
    response     = llm_provider.run(
        messages    = messages,
        model       = model,
        max_tokens  = max_tokens,
        temperature = 0.0,
    )
    return response.content
