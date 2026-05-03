"""
Generic LLM dispatch layer.

Supports Anthropic (Claude) and OpenAI (GPT) via a common ask() interface.
The provider is selected by the llm argument, which is resolved against the
ctx.llm config. All credentials come entirely from ctx — no hardcoded keys.

Adding a new LLM provider requires only a new config/llm/llm.{name}.yaml
file and the corresponding .env key.

Public API
----------
default_llm(ctx)
    Return the name of the default LLM instance from ctx.llm config.
ask(ctx, prompt, llm, max_tokens)
    Dispatch a prompt to the selected LLM provider and return the response.
"""

from __future__ import annotations

from rey_lib.config.config_utils import Namespace

__all__ = [
    "default_llm",
    "ask",
]


def default_llm(ctx: Namespace) -> str:
    """Return the name of the default LLM instance from ctx.llm config."""
    return next(
        (name for name, cfg in ctx.llm.items() if cfg.get("default")),
        "claude",
    )


def _anthropic_ask(
    ctx: Namespace,
    prompt: str,
    max_tokens: int,
    llm_instance: str = "claude",
) -> str:
    """Call the Anthropic API for the given llm_instance config."""
    import anthropic  # noqa: PLC0415

    llm_cfg = ctx.llm[llm_instance]
    key     = llm_cfg.get("api_key", "")
    if not key:
        raise EnvironmentError(
            "Anthropic API key not set.\n"
            "Add ANTHROPIC_API_KEY to .env"
        )
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model      = llm_cfg.model,
        max_tokens = max_tokens,
        messages   = [{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _openai_ask(
    ctx: Namespace,
    prompt: str,
    max_tokens: int,
    llm_instance: str = "gpt4o",
) -> str:
    """Call the OpenAI API for the given llm_instance config."""
    import openai  # noqa: PLC0415

    llm_cfg = ctx.llm[llm_instance]
    key     = llm_cfg.get("api_key", "")
    if not key:
        raise EnvironmentError(
            "OpenAI API key not set.\n"
            "Add OPENAI_API_KEY to .env"
        )
    client   = openai.OpenAI(api_key=key)
    response = client.chat.completions.create(
        model      = llm_cfg.model,
        max_tokens = max_tokens,
        messages   = [{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def ask(
    ctx: Namespace,
    prompt: str,
    llm: str,
    max_tokens: int = 450,
) -> str:
    """
    Dispatch a prompt to the selected LLM provider and return the response.

    The provider is resolved from ctx.llm[llm].provider so dispatch never
    depends on hardcoded strings. Supports 'anthropic' and 'openai'/'chatgpt'.

    Parameters
    ----------
    ctx : Namespace
        Application context. ctx.llm must contain the named instance config.
    prompt : str
        Full prompt text to send.
    llm : str
        LLM instance name from config (e.g. 'claude', 'gpt4o').
    max_tokens : int
        Maximum tokens in the response.

    Returns
    -------
    str
        LLM response text.

    Raises
    ------
    ValueError
        If the provider resolved from config is not recognised.
    EnvironmentError
        If the required API key is not set in ctx.
    """
    if llm in ctx.llm:
        provider = ctx.llm[llm].provider.lower()
    else:
        provider = llm.lower()

    if provider == "anthropic":
        return _anthropic_ask(ctx, prompt, max_tokens, llm_instance=llm)
    if provider in ("openai", "chatgpt"):
        return _openai_ask(ctx, prompt, max_tokens, llm_instance=llm)
    raise ValueError(
        f"Unknown LLM provider '{provider}' for instance '{llm}'. "
        f"Check config/llm/llm.*.yaml"
    )
