"""
Public API models for the LLM orchestration framework.

External programs integrate through these stable types.  Internal execution
objects (ExecutionRecord, provider instances, contract internals) must not
be imported directly by application code.

Public API
----------
RunRequest
    Fully describes one stage execution request.
RunResponse
    Stable response envelope returned to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from rey_lib.llm.records import ExecutionRecord
from rey_lib.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy

__all__ = ["RunRequest", "RunResponse"]

# Idempotency behaviour when a matching key is found.
IDEMPOTENCY_REUSE_SUCCESS = "reuse_success"
IDEMPOTENCY_RERUN_ALWAYS  = "rerun_always"
IDEMPOTENCY_FAIL_IF_EXISTS = "fail_if_exists"


@dataclass(frozen=True)
class RunRequest:
    """Fully describes one LLM stage execution request.

    All provider, model, and policy configuration lives here.  The runner
    reads nothing from global state or environment variables unless a field
    is left empty.

    Attributes
    ----------
    pipeline_id : str
        Logical pipeline identifier.
    stage_id : str
        Stage identifier within the pipeline.
    contract_path : Path
        Path to the versioned contract markdown file.
    input_data : str | list[dict]
        Input data.  A string is sent as-is; a list of dicts is formatted
        as a markdown table.
    provider : str
        Provider name ('anthropic', 'openai', 'ollama', 'mock', or any
        registered custom name).  Falls back to LLM_PROVIDER env var if
        empty.
    model : str
        Model identifier.  Falls back to LLM_MODEL env var if empty.
    api_key : str
        API key for the provider.  Falls back to the provider's standard
        env var if empty.  Not required for 'ollama' or 'mock'.
    output_schema : Optional[dict]
        JSON Schema dict for output validation.  When omitted, the runner
        looks for a sidecar .schema.json file alongside the contract.
    schema_version : str
        Caller-supplied schema version tag stored in the execution record.
    log : Optional[Path]
        JSONL file where the ExecutionRecord is appended after each run.
    idempotency_key : Optional[str]
        When set, the runner checks the log for an existing record with this
        key before executing.  Behaviour is controlled by idempotency_mode.
    idempotency_mode : str
        One of: 'reuse_success', 'rerun_always', 'fail_if_exists'.
    classification : str
        Data classification tag stored in the execution record.
    max_tokens : int
        Maximum tokens the provider may generate.
    max_rows : int
        Maximum rows included when input_data is a list of dicts.
    requires_approval : bool
        When True, the runner stores this stage's record as 'pending_approval'
        on success rather than 'success'.  The pipeline halts at this point.
        Use records.approve() + pipeline.resume() to continue.
    retry_policy : RetryPolicy
        Retry behaviour for this stage.
    """

    pipeline_id:       str
    stage_id:          str
    contract_path:     Path
    input_data:        Union[str, list[dict[str, Any]]]

    provider:          str            = ""
    model:             str            = ""
    api_key:           str            = ""

    output_schema:     Optional[dict[str, Any]] = None
    schema_version:    str                      = ""

    log:               Optional[Path] = None
    idempotency_key:   Optional[str]  = None
    idempotency_mode:  str            = IDEMPOTENCY_REUSE_SUCCESS

    classification:    str            = ""
    max_tokens:        int            = 4000
    max_rows:          int            = 200

    # When True the runner stores the record as 'pending_approval' on success
    # instead of 'success'.  The pipeline halts at this stage until the record
    # is approved via records.approve() and a resume() call is made.
    requires_approval: bool           = False

    # When True the runner skips JSON parsing and returns the raw LLM text.
    # Use for contracts that output YAML, SQL, or other non-JSON formats.
    raw_output:        bool           = False

    retry_policy:      RetryPolicy    = field(default_factory=lambda: DEFAULT_RETRY_POLICY)


@dataclass(frozen=True)
class RunResponse:
    """Stable response envelope returned to all callers.

    Application code should depend on this type, not on ExecutionRecord
    or any internal execution object.

    Attributes
    ----------
    run_id : str
        UUID of the associated ExecutionRecord.
    status : str
        One of the STATUS_* constants from records.py.
    parsed_response : Optional[dict]
        Validated structured output.  None when status is not 'success'.
    errors : list[str]
        Validation or provider error messages.  Empty on success.
    record : Optional[ExecutionRecord]
        Full execution record for callers that need detailed audit data.
        None for idempotency-reused responses that did not load the full
        record from disk.
    """

    run_id:          str
    status:          str
    parsed_response: Optional[dict[str, Any]]    = None
    raw_text:        Optional[str]               = None
    errors:          list[str]                   = field(default_factory=list)
    record:          Optional[ExecutionRecord]   = None
