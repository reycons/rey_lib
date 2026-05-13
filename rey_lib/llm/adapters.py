"""
Application-context adapters for the LLM framework.

Core modules (runner, pipeline, providers) must never depend on
application-specific context objects.  This module is the approved
boundary for code that reads provider config and credentials from
a rey_lib Namespace ctx.

Public API
----------
ask_with_ctx(ctx, prompt, llm, max_tokens, system_prompt)
    Dispatch a prompt using provider config from ctx.llm[llm].
"""

from __future__ import annotations

from typing import Optional

from rey_lib.llm.exceptions import ConfigurationFailure
from rey_lib.llm.providers.base import Message
from rey_lib.llm.providers.registry import resolve as resolve_provider

__all__ = ["ask_with_ctx"]


def ask_with_ctx(
    ctx:           object,
    prompt:        str,
    llm:           str,
    max_tokens:    int           = 450,
    system_prompt: Optional[str] = None,
) -> str:
    """Dispatch a prompt using LLM config from a rey_lib application context.

    Reads provider, model, and API key from ctx.llm[llm].  All SDK calls
    are delegated to the provider abstraction layer — no SDK is imported here.

    Parameters
    ----------
    ctx : object
        Application context with a populated ctx.llm mapping.
    prompt : str
        User-facing prompt text.
    llm : str
        LLM instance name from config (e.g. 'claude', 'gpt4o').
    max_tokens : int
        Maximum tokens in the response.
    system_prompt : Optional[str]
        Optional system-level instruction.

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
    # Read llm config from ctx — ctx is treated as an opaque object here.
    llm_map = getattr(ctx, "llm", None)
    if llm_map is None:
        raise ConfigurationFailure(
            "ctx.llm is not set. Ensure build_ctx() has been called."
        )

    llm_cfg = llm_map.get(llm) if hasattr(llm_map, "get") else getattr(llm_map, llm, None)
    if llm_cfg is None:
        raise ConfigurationFailure(
            f"LLM instance '{llm}' not found in ctx.llm. "
            "Check your config/llm/*.yaml files."
        )

    provider = getattr(llm_cfg, "provider", None) or (
        llm_cfg.get("provider") if hasattr(llm_cfg, "get") else None
    )
    model = getattr(llm_cfg, "model", None) or (
        llm_cfg.get("model") if hasattr(llm_cfg, "get") else None
    )
    api_key = getattr(llm_cfg, "api_key", "") or (
        llm_cfg.get("api_key", "") if hasattr(llm_cfg, "get") else ""
    )

    if not provider:
        raise ConfigurationFailure(
            f"ctx.llm['{llm}'].provider is not set."
        )
    if not api_key:
        raise ConfigurationFailure(
            f"API key not set for LLM instance '{llm}'. "
            "Check your .env file."
        )

    messages: list[Message] = []
    if system_prompt:
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=prompt))

    llm_provider = resolve_provider(provider.lower(), api_key=api_key)
    response     = llm_provider.run(
        messages    = messages,
        model       = model,
        max_tokens  = max_tokens,
        temperature = 0.0,
    )
    return response.content
