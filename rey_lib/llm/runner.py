"""
Standalone LLM workflow runner.

The runner has one job: take data and a contract, send it to an LLM via the
provider abstraction layer, and return a structured result.  It knows nothing
about any application, database connection, or config system.

Provider selection
------------------
The provider and model are resolved in order:

1. Explicit ``provider`` / ``model`` arguments to run().
2. LLM_PROVIDER / LLM_MODEL environment variables.
3. Built-in defaults (anthropic / claude-opus-4-5).

The API key is resolved from ANTHROPIC_API_KEY or OPENAI_API_KEY unless a
pre-registered provider is used (which carries its own credentials).

Output schema
-------------
If a ``<contract_stem>.schema.json`` file exists alongside the contract, it
is loaded automatically and used to validate the parsed JSON response.  The
caller can also pass a schema dict explicitly via the ``output_schema``
parameter.  Validation failures are treated the same as JSON parse failures
and trigger a retry.

Token budget
------------
The runner estimates input token count (~4 chars per token) and warns when
the input exceeds 80% of the model's known context window.

Public API
----------
run(data, contract_path, ...)
    Evaluate data against a contract and return an EvaluationResult.
run_from_file(data_path, contract_path, ...)
    Convenience wrapper that loads data from a CSV, Excel, or text file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Union

from rey_lib.llm import document_loader
from rey_lib.llm import result as result_module
from rey_lib.llm.contract import load as load_contract
from rey_lib.llm.exceptions import ParseFailure, ProviderFailure, SchemaMismatch
from rey_lib.llm.providers.base import BaseProvider, Message
from rey_lib.llm.providers.registry import resolve as resolve_provider
from rey_lib.llm.result import EvaluationResult

__all__ = ["run", "run_from_file"]

_logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL    = "claude-opus-4-5"
_MAX_RETRIES      = 3

# Approximate context window limits by model prefix (tokens).
_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-4":    200_000,
    "claude-sonnet-4":  200_000,
    "claude-haiku-4":   200_000,
    "claude-3-5":       200_000,
    "claude-3":         200_000,
    "gpt-4o":           128_000,
    "gpt-4-turbo":      128_000,
    "gpt-4":              8_192,
    "gpt-3.5":           16_385,
}
_WARN_THRESHOLD  = 0.80
_CHARS_PER_TOKEN = 4


def run(
    data:          Union[str, list[dict[str, Any]]],
    contract_path: Path,
    *,
    use_case:      str                      = "evaluation",
    stage:         str                      = "analysis",
    max_tokens:    int                      = 4000,
    max_rows:      int                      = 200,
    provider:      Optional[str]            = None,
    model:         Optional[str]            = None,
    api_key:       Optional[str]            = None,
    output_schema: Optional[dict[str, Any]] = None,
    log:           Optional[Path]           = None,
) -> EvaluationResult:
    """Evaluate data against a versioned contract.

    The caller supplies data as either a plain string (already formatted)
    or a list of row dicts (e.g. from a SQL query).  The runner formats
    row dicts as a markdown table, loads the contract, calls the LLM, and
    returns a structured EvaluationResult.

    Parameters
    ----------
    data : str | list[dict]
        Input data.  A string is sent as-is.  A list of dicts is formatted
        as a markdown table (up to max_rows rows).
    contract_path : Path
        Path to the versioned contract markdown file.
    use_case : str
        Logical use case name stored in the result audit envelope.
    stage : str
        Stage name within the use case (e.g. 'analysis', 'review').
    max_tokens : int
        Maximum LLM response tokens.
    max_rows : int
        Maximum rows included when data is a list of dicts.
    provider : Optional[str]
        LLM provider override.  Falls back to LLM_PROVIDER env var, then
        'anthropic'.
    model : Optional[str]
        Model override.  Falls back to LLM_MODEL env var, then
        'claude-opus-4-5'.
    api_key : Optional[str]
        API key override.  Falls back to the provider's environment variable.
    output_schema : Optional[dict]
        JSON Schema dict for validating the parsed LLM response.  When
        omitted, the runner looks for a ``<contract_stem>.schema.json``
        file alongside the contract.
    log : Optional[Path]
        If provided, the EvaluationResult is appended to this JSONL file.

    Returns
    -------
    EvaluationResult
        Evaluation with full audit envelope and structured result payload.
    """
    contract_path = Path(contract_path)
    contract      = load_contract(contract_path)

    resolved_provider = provider or os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER)
    resolved_model    = model    or os.environ.get("LLM_MODEL",    _DEFAULT_MODEL)
    resolved_key      = api_key  or _resolve_api_key(resolved_provider)

    schema = output_schema or _load_sidecar_schema(contract_path)

    if isinstance(data, list):
        input_text, input_hash = document_loader.from_query_result(data, max_rows=max_rows)
    else:
        input_text, input_hash = document_loader.from_string(data)

    _warn_token_budget(input_text, resolved_model)

    _logger.info(
        "runner.run: use_case=%s stage=%s contract=%s v%s provider=%s model=%s",
        use_case, stage, contract.name, contract.version,
        resolved_provider, resolved_model,
    )

    llm_provider = resolve_provider(resolved_provider, api_key=resolved_key)

    parsed, raw, retry_count, tokens_in, tokens_out, prompt_hash, validation_errors = (
        _call_with_retry(
            input_text    = input_text,
            system_prompt = contract.body,
            provider      = llm_provider,
            model         = resolved_model,
            max_tokens    = max_tokens,
            schema        = schema,
        )
    )

    if parsed is None:
        _logger.error(
            "runner.run: all retries failed for %s/%s — storing raw response",
            use_case, stage,
        )
        payload: dict[str, Any] = {"raw_response": raw, "parse_error": True}
    else:
        payload = parsed

    evaluation = result_module.new(
        use_case          = use_case,
        stage             = stage,
        contract_name     = contract.name,
        contract_version  = contract.version,
        contract_hash     = contract.hash,
        model             = resolved_model,
        input_hash        = input_hash,
        result            = payload,
        provider          = resolved_provider,
        tokens_in         = tokens_in,
        tokens_out        = tokens_out,
        retry_count       = retry_count,
        prompt_hash       = prompt_hash,
        raw_response      = raw,
        validation_errors = validation_errors,
    )

    if log:
        result_module.store(evaluation, Path(log))
        _logger.info("runner.run: result stored → %s", log)

    return evaluation


def run_from_file(
    data_path:     Path,
    contract_path: Path,
    *,
    sheet:         Optional[str] = None,
    max_rows:      int           = 200,
    **kwargs: Any,
) -> EvaluationResult:
    """Load data from a file then evaluate against a contract.

    Supported file types: .csv, .xlsx, .xls, .txt, .md.

    Parameters
    ----------
    data_path : Path
        Path to the data file.
    contract_path : Path
        Path to the versioned contract markdown file.
    sheet : Optional[str]
        Excel sheet name (ignored for non-Excel files).
    max_rows : int
        Maximum rows to include from tabular files.
    **kwargs
        Forwarded to run() (use_case, stage, max_tokens, provider, model,
        api_key, output_schema, log).

    Returns
    -------
    EvaluationResult
    """
    data_path = Path(data_path)
    suffix    = data_path.suffix.lower()

    if suffix == ".csv":
        text, _ = document_loader.from_csv(data_path, max_rows=max_rows)
    elif suffix in (".xlsx", ".xls"):
        text, _ = document_loader.from_excel(data_path, sheet=sheet, max_rows=max_rows)
    elif suffix in (".txt", ".md"):
        text, _ = document_loader.from_text(data_path)
    else:
        raise ValueError(
            f"Unsupported file type '{suffix}'. "
            "Supported: .csv, .xlsx, .xls, .txt, .md"
        )

    return run(text, contract_path, max_rows=max_rows, **kwargs)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_api_key(provider: str) -> str:
    """Read the API key for the given provider from environment variables."""
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "chatgpt":   "OPENAI_API_KEY",
        "ollama":    "",
        "mock":      "",
    }
    env_var = env_map.get(provider.lower())
    if env_var is None:
        # Unknown provider — attempt a generic env lookup, fall back to empty.
        return os.environ.get(f"{provider.upper()}_API_KEY", "")
    if not env_var:
        return ""
    key = os.environ.get(env_var, "")
    if not key:
        raise ProviderFailure(
            f"API key not found. Set the {env_var} environment variable."
        )
    return key


def _load_sidecar_schema(contract_path: Path) -> Optional[dict[str, Any]]:
    """Load a JSON Schema file named <contract_stem>.schema.json if it exists."""
    schema_path = contract_path.with_name(contract_path.stem + ".schema.json")
    if schema_path.exists():
        _logger.debug("runner: loaded sidecar schema from %s", schema_path)
        return json.loads(schema_path.read_text(encoding="utf-8"))
    return None


def _validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate data against a JSON Schema.  Returns a list of error messages."""
    try:
        import jsonschema  # noqa: PLC0415
        errors = list(jsonschema.Draft7Validator(schema).iter_errors(data))
        return [e.message for e in errors]
    except ImportError:
        _logger.warning(
            "runner: jsonschema not installed — skipping schema validation. "
            "Run: pip install jsonschema"
        )
        return []


def _warn_token_budget(text: str, model: str) -> None:
    """Log a warning if the estimated input token count is large."""
    estimated_tokens = len(text) // _CHARS_PER_TOKEN
    limit = next(
        (v for k, v in _CONTEXT_LIMITS.items() if model.startswith(k)),
        None,
    )
    if limit is None:
        return
    if estimated_tokens > limit * _WARN_THRESHOLD:
        _logger.warning(
            "runner: input is ~%s estimated tokens (%.0f%% of %s limit: %s). "
            "Consider pre-aggregating in SQL to reduce the dataset.",
            f"{estimated_tokens:,}",
            (estimated_tokens / limit) * 100,
            model,
            f"{limit:,}",
        )


def _hash_prompt(prompt: str) -> str:
    """Return the SHA-256 hex digest of a rendered prompt string."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _call_with_retry(
    input_text:    str,
    system_prompt: str,
    provider:      BaseProvider,
    model:         str,
    max_tokens:    int,
    schema:        Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], str, int, int, int, str, list[str]]:
    """Call the provider up to _MAX_RETRIES times until a valid response arrives.

    Returns
    -------
    tuple of:
        parsed          — dict or None if all retries failed
        raw             — last raw response text
        retry_count     — number of retries (0 = succeeded on first attempt)
        tokens_in       — input tokens from the last successful call
        tokens_out      — output tokens from the last successful call
        prompt_hash     — SHA-256 of the rendered prompt
        validation_errors — errors from the last failed attempt (empty on success)
    """
    prompt = (
        input_text
        + "\n\nRespond with a single valid JSON object. "
        "Return only the JSON — no explanation or markdown wrapper."
    )
    prompt_hash = _hash_prompt(prompt)

    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user",   content=prompt),
    ]

    raw               = ""
    tokens_in         = 0
    tokens_out        = 0
    validation_errors: list[str] = []

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = provider.run(
                messages    = messages,
                model       = model,
                max_tokens  = max_tokens,
                temperature = 0.0,
            )
        except ProviderFailure as exc:
            _logger.warning(
                "runner: provider failure on attempt %d/%d: %s", attempt, _MAX_RETRIES, exc
            )
            raw = str(exc)
            continue

        raw        = response.content
        tokens_in  = response.tokens_in
        tokens_out = response.tokens_out

        try:
            parsed = json.loads(_extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            _logger.warning(
                "runner: JSON parse failed on attempt %d/%d", attempt, _MAX_RETRIES
            )
            validation_errors = ["JSON parse failure"]
            continue

        if schema:
            validation_errors = _validate_schema(parsed, schema)
            if validation_errors:
                _logger.warning(
                    "runner: schema validation failed on attempt %d/%d: %s",
                    attempt, _MAX_RETRIES, "; ".join(validation_errors[:3]),
                )
                continue

        retry_count = attempt - 1
        return parsed, raw, retry_count, tokens_in, tokens_out, prompt_hash, []

    return None, raw, _MAX_RETRIES - 1, tokens_in, tokens_out, prompt_hash, validation_errors


def _extract_json(text: str) -> str:
    """Strip markdown code fences if the LLM wrapped its response in them."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli() -> None:
    """python -m rey_lib.llm.runner --data <file> --contract <file> [options]."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog        = "rey_lib.llm.runner",
        description = "Evaluate data against an LLM contract.",
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to data file (.csv, .xlsx, .txt, .md) or '-' to read stdin as text",
    )
    parser.add_argument(
        "--contract", required=True,
        help="Path to the contract markdown file",
    )
    parser.add_argument("--use-case",   default="evaluation",    help="Use case name")
    parser.add_argument("--stage",      default="analysis",      help="Stage name")
    parser.add_argument("--max-tokens", default=4000, type=int,  help="Max LLM tokens")
    parser.add_argument("--max-rows",   default=200,  type=int,  help="Max table rows")
    parser.add_argument("--provider",   default=None,            help="LLM provider")
    parser.add_argument("--model",      default=None,            help="LLM model")
    parser.add_argument("--schema",     default=None,            help="Path to JSON Schema file")
    parser.add_argument("--log",        default=None,            help="JSONL output path")
    parser.add_argument("--quiet",      action="store_true",     help="Suppress JSON output")
    args = parser.parse_args()

    schema: Optional[dict[str, Any]] = None
    if args.schema:
        schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))

    # Exit codes per design contract:
    # 0=success, 1=general, 2=validation, 3=provider, 6=configuration
    try:
        if args.data == "-":
            raw_text   = sys.stdin.read()
            evaluation = run(
                data          = raw_text,
                contract_path = Path(args.contract),
                use_case      = args.use_case,
                stage         = args.stage,
                max_tokens    = args.max_tokens,
                max_rows      = args.max_rows,
                provider      = args.provider,
                model         = args.model,
                output_schema = schema,
                log           = Path(args.log) if args.log else None,
            )
        else:
            evaluation = run_from_file(
                data_path     = Path(args.data),
                contract_path = Path(args.contract),
                use_case      = args.use_case,
                stage         = args.stage,
                max_tokens    = args.max_tokens,
                max_rows      = args.max_rows,
                provider      = args.provider,
                model         = args.model,
                output_schema = schema,
                log           = Path(args.log) if args.log else None,
            )
    except ProviderFailure as exc:
        _logger.error("provider failure: %s", exc)
        sys.exit(3)
    except SchemaMismatch as exc:
        _logger.error("schema mismatch: %s", exc)
        sys.exit(2)
    except ParseFailure as exc:
        _logger.error("parse failure: %s", exc)
        sys.exit(2)
    except Exception as exc:
        _logger.error("unexpected error: %s", exc)
        sys.exit(1)

    if not args.quiet:
        print(json.dumps(evaluation.result, indent=2, default=str))


if __name__ == "__main__":
    _cli()
