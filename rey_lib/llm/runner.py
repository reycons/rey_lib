"""
LLM stage runner — the execution heart of the orchestration framework.

The runner accepts a RunRequest, calls the provider, validates the output,
and returns a RunResponse backed by a persisted ExecutionRecord.

Failure semantics
-----------------
ParseFailure        Retried up to RetryPolicy.max_attempts.
ProviderFailure     Retried up to RetryPolicy.max_attempts.
SchemaMismatch      Fails immediately — schema failures are not transient.

Provider resolution order
--------------------------
1. RunRequest.provider / model / api_key
2. LLM_PROVIDER / LLM_MODEL environment variables
3. ConfigurationFailure raised — no hardcoded fallback.

Public API
----------
run(request)
    Execute a RunRequest and return a RunResponse.
run_batch(requests)
    Execute a list of RunRequests sequentially.
run_from_file(data_path, contract_path, ...)
    Convenience wrapper: load data from a file then call run().
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from rey_lib.errors.error_utils import ConfigError
from rey_lib.llm import document_loader
from rey_lib.llm.api import RunRequest, RunResponse
from rey_lib.llm.artifacts import ArtifactStore
from rey_lib.llm.contract import Contract, load as load_contract
from rey_lib.llm.exceptions import (
    ConfigurationFailure,
    ParseFailure,
    ProviderFailure,
    SchemaMismatch,
    TimeoutFailure,
)
from rey_lib.llm.providers.base import BaseProvider, Message
from rey_lib.llm.providers.registry import resolve as resolve_provider
from rey_lib.llm.records import (
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_SUCCESS,
    ExecutionRecord,
    load_all_records,
    store_record,
)
from rey_lib.llm.redaction import NoopRedactor, RedactionFilter
from rey_lib.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from rey_lib.logs.log_utils import get_logger

__all__ = ["run", "run_batch", "run_from_file"]

_logger = get_logger(__name__)


# Approximate context window limits by model prefix (tokens).
_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-4":   200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4":  200_000,
    "claude-3-5":      200_000,
    "claude-3":        200_000,
    "gpt-4o":          128_000,
    "gpt-4-turbo":     128_000,
    "gpt-4":             8_192,
    "gpt-3.5":          16_385,
}
_WARN_THRESHOLD  = 0.80
_CHARS_PER_TOKEN = 4

# ---------------------------------------------------------------------------
# Internal grouping types — not part of the public API.
# ---------------------------------------------------------------------------

@dataclass
class _ProviderConfig:
    """Resolved provider name, model, and initialised provider instance."""

    name:     str
    model:    str
    provider: BaseProvider


@dataclass
class _ExecuteResult:
    """Output of the retry loop — always populated, status encodes outcome."""

    parsed:            Optional[dict[str, Any]]
    raw:               str
    tokens_in:         int
    tokens_out:        int
    retry_count:       int
    validation_errors: list[str]
    status:            str  # STATUS_SUCCESS or STATUS_FAILED


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run(
    request:          RunRequest,
    *,
    redaction_filter: Optional[RedactionFilter] = None,
    artifact_store:   Optional[ArtifactStore]   = None,
) -> RunResponse:
    """Execute a RunRequest and return a RunResponse.

    Parameters
    ----------
    request : RunRequest
        Fully describes the stage to execute.
    redaction_filter : Optional[RedactionFilter]
        When provided, applied to input text before it is sent to the
        provider.  Use to mask PII or confidential data.
    artifact_store : Optional[ArtifactStore]
        When provided, the parsed_response is written to the store after
        a successful execution and the URI is recorded in artifact_uris.

    Returns
    -------
    RunResponse
        Stable response envelope.  status is one of the STATUS_* constants.
        A stage with requires_approval=True that succeeds returns
        status='pending_approval', not 'success'.
    """
    # 1. Idempotency check before doing any work.
    idempotency_response = _check_idempotency(request)
    if idempotency_response is not None:
        return idempotency_response

    contract     = _load_contract(request.contract_path)
    provider_cfg = _resolve_provider_config(request)

    raw_text, input_hash = _prepare_input(request)

    # 2. Apply redaction before any provider interaction.
    redactor   = redaction_filter or NoopRedactor()
    input_text = redactor.redact(raw_text)

    schema               = _load_schema(request)
    schema_hash          = _hash_schema(schema)
    prompt_hash          = _hash_text(contract.body)

    _warn_token_budget(input_text, provider_cfg.model)

    messages             = _build_messages(input_text, contract.body)
    rendered_prompt_hash = _hash_messages(messages)

    # 3. Capability check before touching the provider.
    _check_capabilities(provider_cfg.provider, messages)

    policy     = request.retry_policy or DEFAULT_RETRY_POLICY
    started_at = datetime.now(timezone.utc).isoformat()
    t0         = time.monotonic()

    _logger.info(
        "runner.run: pipeline=%s stage=%s contract=%s v%s provider=%s model=%s",
        request.pipeline_id, request.stage_id,
        contract.name, contract.version,
        provider_cfg.name, provider_cfg.model,
    )

    result = _execute_with_retry(
        provider   = provider_cfg.provider,
        messages   = messages,
        model      = provider_cfg.model,
        max_tokens = request.max_tokens,
        schema     = schema,
        policy     = policy,
        raw_output = getattr(request, "raw_output", False),
    )

    # 4. Resolve the final stored status once.
    # A successful stage that requires human approval is stored as
    # pending_approval — not success.  This is the single point of truth;
    # the pipeline never patches the record after storage.
    final_status = result.status
    if result.status == STATUS_SUCCESS and request.requires_approval:
        final_status = STATUS_PENDING_APPROVAL

    # 5. Generate the run_id now so the artifact URI embeds the real ID.
    run_id = str(uuid.uuid4())

    artifact_uris: list[str] = []
    if artifact_store is not None and result.parsed is not None:
        uri = artifact_store.write(
            run_id   = run_id,
            stage_id = request.stage_id,
            data     = result.parsed,
        )
        artifact_uris.append(uri)

    record = _build_record(
        request              = request,
        contract             = contract,
        provider_cfg         = provider_cfg,
        started_at           = started_at,
        t0                   = t0,
        input_hash           = input_hash,
        schema_hash          = schema_hash,
        prompt_hash          = prompt_hash,
        rendered_prompt_hash = rendered_prompt_hash,
        result               = result,
        schema_version       = request.schema_version,
        policy               = policy,
        final_status         = final_status,
        artifact_uris        = artifact_uris,
        run_id               = run_id,
    )

    if request.log:
        store_record(record, request.log)
        _logger.info("runner.run: record stored → %s", request.log)

    return RunResponse(
        run_id          = record.run_id,
        status          = record.status,
        parsed_response = record.parsed_response,
        raw_text        = result.raw if getattr(request, "raw_output", False) else None,
        errors          = record.validation_errors,
        record          = record,
    )


def run_batch(
    requests:         list[RunRequest],
    *,
    redaction_filter: Optional[RedactionFilter] = None,
    artifact_store:   Optional[ArtifactStore]   = None,
) -> list[RunResponse]:
    """Execute a list of RunRequests sequentially and return all responses.

    Each request is independent — a failure in one does not stop the others.
    The response list is the same length as the request list and in the same order.

    Parameters
    ----------
    requests : list[RunRequest]
        Requests to execute in order.
    redaction_filter : Optional[RedactionFilter]
        Applied to each request's input.  Shared across all items in the batch.
    artifact_store : Optional[ArtifactStore]
        Artifact backend used for each successful execution in the batch.

    Returns
    -------
    list[RunResponse]
        One response per request, in input order.
    """
    responses: list[RunResponse] = []
    for i, request in enumerate(requests):
        _logger.info(
            "runner.run_batch: item %d/%d — pipeline=%s stage=%s",
            i + 1, len(requests), request.pipeline_id, request.stage_id,
        )
        responses.append(run(
            request,
            redaction_filter = redaction_filter,
            artifact_store   = artifact_store,
        ))
    return responses


def run_from_file(
    data_path:     Path,
    contract_path: Path,
    pipeline_id:   str,
    stage_id:      str,
    *,
    sheet:         Optional[str] = None,
    max_rows:      int           = 200,
    **kwargs: Any,
) -> RunResponse:
    """Load data from a file then execute against a contract.

    Supported file types: .csv, .xlsx, .xls, .txt, .md.

    Parameters
    ----------
    data_path : Path
        Path to the data file.
    contract_path : Path
        Path to the versioned contract markdown file.
    pipeline_id : str
        Logical pipeline identifier.
    stage_id : str
        Stage identifier.
    sheet : Optional[str]
        Excel sheet name (ignored for non-Excel files).
    max_rows : int
        Maximum rows to include from tabular files.
    **kwargs
        Forwarded to RunRequest (provider, model, api_key, log, etc.).

    Returns
    -------
    RunResponse
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
        raise ConfigurationFailure(
            f"Unsupported file type '{suffix}'. "
            "Supported: .csv, .xlsx, .xls, .txt, .md"
        )

    return run(RunRequest(
        pipeline_id   = pipeline_id,
        stage_id      = stage_id,
        contract_path = contract_path,
        input_data    = text,
        max_rows      = max_rows,
        **kwargs,
    ))


# ---------------------------------------------------------------------------
# Private — execution pipeline
# ---------------------------------------------------------------------------

def _check_idempotency(request: RunRequest) -> Optional[RunResponse]:
    """Return a cached RunResponse if an idempotency match is found.

    Raises LockConflict if mode is 'fail_if_exists' and a match exists.
    Returns None when no match or when rerun_always is set.
    """
    from rey_lib.llm.api import (  # noqa: PLC0415
        IDEMPOTENCY_FAIL_IF_EXISTS,
        IDEMPOTENCY_REUSE_SUCCESS,
    )
    from rey_lib.llm.exceptions import LockConflict  # noqa: PLC0415

    if not request.idempotency_key or not request.log:
        return None

    records = load_all_records(request.log)
    for record in reversed(records):
        if record.idempotency_key != request.idempotency_key:
            continue

        if request.idempotency_mode == IDEMPOTENCY_FAIL_IF_EXISTS:
            raise LockConflict(
                f"Idempotency key '{request.idempotency_key}' already exists "
                f"(run_id={record.run_id}, status={record.status})."
            )

        if request.idempotency_mode == IDEMPOTENCY_REUSE_SUCCESS:
            if record.status == STATUS_SUCCESS:
                _logger.info(
                    "runner: idempotency hit — reusing run_id=%s", record.run_id
                )
                return RunResponse(
                    run_id          = record.run_id,
                    status          = record.status,
                    parsed_response = record.parsed_response,
                    errors          = [],
                    record          = record,
                )

        # rerun_always — fall through to execute
        break

    return None


def _load_contract(contract_path: Path) -> Contract:
    """Load and return the versioned contract, raising ConfigurationFailure on error."""
    try:
        return load_contract(contract_path)
    except (ConfigError, OSError) as exc:
        raise ConfigurationFailure(
            f"Failed to load contract '{contract_path}': {exc}"
        ) from exc


def _resolve_provider_config(request: RunRequest) -> _ProviderConfig:
    """Construct the provider instance from the fully-populated RunRequest.

    All three fields — provider, model, api_key — must be supplied by the
    caller.  The library does not read environment variables or apply defaults.
    If the provider name is pre-registered in the registry, that instance is
    used directly and api_key is ignored.
    """
    from rey_lib.llm.providers import registry as _reg  # noqa: PLC0415

    if not request.provider:
        raise ConfigurationFailure(
            "RunRequest.provider is required. Set it to the provider name "
            "(e.g. 'anthropic', 'openai', 'ollama')."
        )
    if not request.model:
        raise ConfigurationFailure(
            "RunRequest.model is required. Set it to the model identifier."
        )

    # Pre-registered providers carry their own credentials.
    try:
        provider = _reg.get(request.provider)
        return _ProviderConfig(name=request.provider, model=request.model, provider=provider)
    except ConfigurationFailure:
        pass

    provider = resolve_provider(request.provider, api_key=request.api_key)
    return _ProviderConfig(name=request.provider, model=request.model, provider=provider)


def _prepare_input(request: RunRequest) -> tuple[str, str]:
    """Format input_data as text and return (text, sha256_hash)."""
    if isinstance(request.input_data, list):
        return document_loader.from_query_result(
            request.input_data, max_rows=request.max_rows
        )
    return document_loader.from_string(request.input_data)


def _load_schema(request: RunRequest) -> Optional[dict[str, Any]]:
    """Return the output schema from the request or a sidecar file."""
    if request.output_schema:
        return request.output_schema
    return _load_sidecar_schema(Path(request.contract_path))


def _build_messages(input_text: str, system_prompt: str) -> list[Message]:
    """Construct the message array sent to the provider."""
    user_content = (
        input_text
        + "\n\nRespond with a single valid JSON object. "
        "Return only the JSON — no explanation or markdown wrapper."
    )
    return [
        Message(role="system", content=system_prompt),
        Message(role="user",   content=user_content),
    ]


def _check_capabilities(provider: BaseProvider, messages: list[Message]) -> None:
    """Raise ConfigurationFailure if the provider cannot handle the message array."""
    caps       = provider.capabilities
    has_system = any(m.role == "system" for m in messages)

    if has_system and not caps.supports_system_messages:
        raise ConfigurationFailure(
            f"{type(provider).__name__} does not support system messages. "
            "The contract body is always sent as a system prompt."
        )


def _execute_with_retry(
    provider:   BaseProvider,
    messages:   list[Message],
    model:      str,
    max_tokens: int,
    schema:     Optional[dict[str, Any]],
    policy:     RetryPolicy,
    raw_output: bool = False,
) -> _ExecuteResult:
    """Run the provider call up to policy.max_attempts times.

    SchemaMismatch is never retried.  ParseFailure and ProviderFailure are
    retried only if they appear in policy.retry_on.

    Returns an _ExecuteResult with status=STATUS_SUCCESS or STATUS_FAILED.
    """
    raw        = ""
    tokens_in  = 0
    tokens_out = 0
    last_errors: list[str] = []

    for attempt in range(policy.max_attempts):
        raw, tokens_in, tokens_out, exc = _single_provider_call(
            provider, messages, model, max_tokens
        )

        if exc is not None:
            last_errors = [str(exc)]
            if not isinstance(exc, policy.retry_on):
                return _ExecuteResult(
                    parsed=None, raw=raw, tokens_in=tokens_in,
                    tokens_out=tokens_out, retry_count=attempt,
                    validation_errors=last_errors, status=STATUS_FAILED,
                )
            if isinstance(exc, TimeoutFailure):
                _logger.warning(
                    "runner: attempt %d/%d timed out: %s",
                    attempt + 1, policy.max_attempts, exc,
                )
            else:
                _logger.warning(
                    "runner: attempt %d/%d failed (provider): %s",
                    attempt + 1, policy.max_attempts, exc,
                )
            continue

        if raw_output:
            return _ExecuteResult(
                parsed=None, raw=raw, tokens_in=tokens_in,
                tokens_out=tokens_out, retry_count=attempt,
                validation_errors=[], status=STATUS_SUCCESS,
            )

        parsed, parse_exc = _attempt_parse(raw)
        if parse_exc is not None:
            last_errors = [str(parse_exc)]
            if ParseFailure not in policy.retry_on:
                return _ExecuteResult(
                    parsed=None, raw=raw, tokens_in=tokens_in,
                    tokens_out=tokens_out, retry_count=attempt,
                    validation_errors=last_errors, status=STATUS_FAILED,
                )
            _logger.warning(
                "runner: attempt %d/%d failed (parse): %s",
                attempt + 1, policy.max_attempts, parse_exc,
            )
            continue

        # Schema validation: immediate failure, never retried.
        if schema:
            schema_errors = _validate_schema(parsed, schema)  # type: ignore[arg-type]
            if schema_errors:
                _logger.error(
                    "runner: schema validation failed — not retrying: %s",
                    "; ".join(schema_errors[:3]),
                )
                return _ExecuteResult(
                    parsed=None, raw=raw, tokens_in=tokens_in,
                    tokens_out=tokens_out, retry_count=attempt,
                    validation_errors=schema_errors, status=STATUS_FAILED,
                )

        return _ExecuteResult(
            parsed=parsed, raw=raw, tokens_in=tokens_in,
            tokens_out=tokens_out, retry_count=attempt,
            validation_errors=[], status=STATUS_SUCCESS,
        )

    last_error = last_errors[0] if last_errors else "unknown"
    if "timeout" in last_error.lower() or "timed out" in last_error.lower():
        _logger.error(
            "runner: too many provider timeouts (%d attempts). Last error: %s",
            policy.max_attempts, last_error,
        )
    else:
        _logger.error(
            "runner: all %d attempts failed. Last error: %s",
            policy.max_attempts, last_error,
        )
    return _ExecuteResult(
        parsed=None, raw=raw, tokens_in=tokens_in,
        tokens_out=tokens_out, retry_count=policy.max_attempts - 1,
        validation_errors=last_errors, status=STATUS_FAILED,
    )


def _single_provider_call(
    provider:   BaseProvider,
    messages:   list[Message],
    model:      str,
    max_tokens: int,
) -> tuple[str, int, int, Optional[ProviderFailure]]:
    """Make one provider call. Returns (raw, tokens_in, tokens_out, exc_or_None)."""
    try:
        resp = provider.run(messages=messages, model=model, max_tokens=max_tokens, temperature=0.0)
        return resp.content, resp.tokens_in, resp.tokens_out, None
    except ProviderFailure as exc:
        return str(exc), 0, 0, exc


def _attempt_parse(raw: str) -> tuple[Optional[dict[str, Any]], Optional[ParseFailure]]:
    """Parse JSON from provider response. Returns (parsed, exc_or_None)."""
    try:
        return json.loads(_strip_code_fences(raw)), None
    except (json.JSONDecodeError, ValueError) as exc:
        return None, ParseFailure(f"JSON parse error: {exc}")


def _validate_schema(
    parsed: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """Validate parsed dict against a JSON Schema. Returns list of error messages."""
    try:
        import jsonschema  # noqa: PLC0415
        errors = list(jsonschema.Draft7Validator(schema).iter_errors(parsed))
        return [e.message for e in errors]
    except ImportError:
        _logger.warning(
            "runner: jsonschema not installed — skipping schema validation. "
            "Run: pip install jsonschema"
        )
        return []


def _build_record(
    request:              RunRequest,
    contract:             Contract,
    provider_cfg:         _ProviderConfig,
    started_at:           str,
    t0:                   float,
    input_hash:           str,
    schema_hash:          str,
    prompt_hash:          str,
    rendered_prompt_hash: str,
    result:               _ExecuteResult,
    schema_version:       str,
    policy:               RetryPolicy,
    final_status:         str                  = "",
    artifact_uris:        Optional[list[str]]  = None,
    run_id:               str                  = "",
) -> ExecutionRecord:
    """Assemble an ExecutionRecord from all collected execution metadata.

    final_status overrides result.status when set (used for pending_approval).
    run_id is pre-generated by the caller so artifact URIs can embed it.
    """
    ended_at   = datetime.now(timezone.utc).isoformat()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return ExecutionRecord(
        run_id               = run_id or str(uuid.uuid4()),
        pipeline_id          = request.pipeline_id,
        stage_id             = request.stage_id,
        contract_name        = contract.name,
        contract_version     = contract.version,
        contract_hash        = contract.hash,
        schema_version       = schema_version,
        schema_hash          = schema_hash,
        provider             = provider_cfg.name,
        model                = provider_cfg.model,
        provider_endpoint    = "",
        input_hash           = input_hash,
        prompt_hash          = prompt_hash,
        rendered_prompt_hash = rendered_prompt_hash,
        started_at           = started_at,
        ended_at             = ended_at,
        elapsed_ms           = elapsed_ms,
        tokens_in            = result.tokens_in,
        tokens_out           = result.tokens_out,
        cost                 = 0.0,
        status               = final_status or result.status,
        raw_response         = result.raw,
        parsed_response      = result.parsed,
        validation_errors    = result.validation_errors,
        retry_count          = result.retry_count,
        retry_policy         = repr(policy),
        idempotency_key      = request.idempotency_key or "",
        classification       = request.classification,
        artifact_uris        = artifact_uris or [],
        approved_by          = "",
        approved_at          = "",
    )


# ---------------------------------------------------------------------------
# Private — utilities
# ---------------------------------------------------------------------------

def _load_sidecar_schema(contract_path: Path) -> Optional[dict[str, Any]]:
    """Load a JSON Schema file named <contract_stem>.schema.json if it exists."""
    schema_path = contract_path.with_name(contract_path.stem + ".schema.json")
    if schema_path.exists():
        _logger.debug("runner: loaded sidecar schema from %s", schema_path)
        return json.loads(schema_path.read_text(encoding="utf-8"))
    return None


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped its JSON response."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:]
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()


def _hash_text(text: str) -> str:
    """Return the SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_messages(messages: list[Message]) -> str:
    """Return the SHA-256 hex digest of the serialised message array."""
    serialised = json.dumps(
        [{"role": m.role, "content": m.content} for m in messages],
        sort_keys=True,
    )
    return _hash_text(serialised)


def _hash_schema(schema: Optional[dict[str, Any]]) -> str:
    """Return the SHA-256 hex digest of a JSON Schema dict, or empty string."""
    if not schema:
        return ""
    return _hash_text(json.dumps(schema, sort_keys=True))


def _warn_token_budget(text: str, model: str) -> None:
    """Log a warning when estimated input token count approaches the context limit."""
    estimated = len(text) // _CHARS_PER_TOKEN
    limit      = next(
        (v for k, v in _CONTEXT_LIMITS.items() if model.startswith(k)),
        None,
    )
    if limit and estimated > limit * _WARN_THRESHOLD:
        _logger.warning(
            "runner: input is ~%s estimated tokens (%.0f%% of %s limit: %s). "
            "Consider pre-aggregating before evaluation.",
            f"{estimated:,}",
            (estimated / limit) * 100,
            model,
            f"{limit:,}",
        )


if __name__ == "__main__":
    from rey_lib.llm.cli import main  # noqa: PLC0415
    main()
