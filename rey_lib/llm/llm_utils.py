"""
Convenience LLM dispatch utilities.

direct_ask() is a convenience API over rey_lib.llm.runner.run for callers that
already have a fully formed prompt/package and do not need a persistent contract
file. It wraps the prompt as an in-memory contract and delegates to runner.run —
the single LLM execution owner — so it still gets provider execution, retries,
normal logging, and evaluation logging.

For ctx-based dispatch (reading provider config from a rey_lib Namespace
application context), use rey_lib.llm.adapters.ask_with_ctx instead.

Public API
----------
direct_ask(prompt, model, provider, api_key, ...)
    Run a fully-formed prompt through runner.run and return the response text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

__all__ = ["direct_ask"]


def direct_ask(
    prompt:        str,
    model:         str,
    provider:      str,
    api_key:       str,
    max_tokens:    int           = 4000,
    system_prompt: Optional[str] = None,
    temperature:   float         = 0.0,
    output_format: str           = "",
    eval_payload_log_path: Optional[Path] = None,
    eval_run_log_path:     Optional[Path] = None,
    payload_id:            Optional[str]  = None,
) -> str:
    """Run a fully-formed prompt through runner.run and return the response text.

    A convenience API for callers that already have a complete prompt/package
    and do not need a persistent contract file. Credentials are supplied
    explicitly. The prompt is wrapped as an in-memory contract and executed
    through the normal runner, so it gains provider execution, retries, normal
    logging, and evaluation logging without duplicating any of that here.

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
    output_format : str
        Optional expected output mode (e.g. "markdown"). Carried into the inline
        contract so the runner does not force a JSON-object response. Empty means
        the prompt is sent exactly as supplied.

    Returns
    -------
    str
        LLM response text.

    Notes
    -----
    Execution is delegated to runner.run() — the single LLM execution owner —
    so retries, normal logging, payload_id, llm_run_id, and evaluation logging
    all apply. Because a full prompt and credentials are supplied, no contract
    file is loaded and no JSON-object instruction is appended.
    """
    # Local imports avoid an import cycle (runner imports the llm package).
    from rey_lib.llm.api import RunRequest
    from rey_lib.llm.runner import run as _run

    contract_text = system_prompt if system_prompt else prompt
    if output_format:
        contract_text = f"{contract_text}\n\nProduce the response as {output_format}."

    response = _run(
        RunRequest(
            pipeline_id   = "direct_ask",
            stage_id      = "direct_ask",
            contract_path = Path("<direct_ask>"),
            input_data    = prompt,
            provider      = provider,
            model         = model,
            api_key       = api_key,
            max_tokens    = max_tokens,
            temperature   = temperature,
            raw_output    = True,
            contract_text = contract_text,
            eval_payload_log_path = eval_payload_log_path,
            eval_run_log_path     = eval_run_log_path,
            payload_id            = payload_id,
        )
    )
    return response.raw_text or ""
