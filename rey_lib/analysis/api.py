"""
Public API models for the contract-driven analysis domain.

External programs integrate through these stable types. Internal objects
(ExecutionRecord, contract internals) must not be imported directly by
application code.

**Provider vocabulary is deliberately absent.** A request names a
``profile_id`` and nothing else about how the model is reached: which provider
and model answer, how a failed call is retried, and what a provider may be sent
are all owned by ``rey_lib.ai``. This domain says what analysis to run; it does
not say who runs it.

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

from rey_lib.analysis.records import ExecutionRecord

__all__ = ["RunRequest", "RunResponse"]

# Idempotency behaviour when a matching key is found.
IDEMPOTENCY_REUSE_SUCCESS = "reuse_success"
IDEMPOTENCY_RERUN_ALWAYS  = "rerun_always"
IDEMPOTENCY_FAIL_IF_EXISTS = "fail_if_exists"


@dataclass(frozen=True)
class RunRequest:
    """Fully describes one LLM stage execution request.

    Analysis configuration only. Provider, model, credential and retry policy
    are the AI runtime's, selected by ``profile_id``. Nothing here is read from
    global state or the environment.

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
    profile_id : str
        Provider name ('anthropic', 'openai', 'ollama', 'mock', or any
        registered custom name).  Falls back to LLM_PROVIDER env var if
        empty.
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
        Maximum tokens the model may generate.
    max_rows : int
        Maximum rows included when input_data is a list of dicts.
    requires_approval : bool
        When True, the runner stores this stage's record as 'pending_approval'
        on success rather than 'success'.  The pipeline halts at this point.
        Use records.approve() + pipeline.resume() to continue.
    """

    pipeline_id:       str
    stage_id:          str
    contract_path:     Path
    input_data:        Union[str, list[dict[str, Any]]]

    profile_id:        str            = ""

    # Provider-specific options sourced from the LLM profile (e.g. Ollama
    # endpoint, timeout_seconds, and capability flags). Empty for providers
    # that take no extra options.

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

    # When set (e.g. "sql"), the runner asks the model for a standard JSON
    # artifact envelope, strips accidental fencing, extracts the `content`
    # field, validates it for the artifact type, and returns only the clean
    # content. Provider/model independent — applies to any configured provider.
    artifact_type:     str            = ""

    # Artifact-processing routing config (artifact_type -> {enabled, engine,
    # config_path, fail_on_error}) from linting/artifact_processing.yaml. When
    # present, the runner post-processes the extracted artifact (e.g. SQL
    # formatting via rey_lib.artifacts) before returning the final content.
    artifact_processing: dict[str, Any] = field(default_factory=dict)


    # Append-only LLM evaluation logs (SGC_Rey_LLM_Evaluation_Append_Only_Log).
    # Callers pass the already-resolved llm_evaluation.payload_log_path and
    # run_log_path from ctx; evaluation logging is additive and only active when
    # a path is set. payload_id is the stable reusable-payload identity: when
    # supplied the payload is reused (no new payload record), otherwise the runner
    # generates one for the newly captured payload.
    # Names an already-recorded evaluation payload, so a reused payload is
    # not recorded twice. Where that record goes is rey_lib.logs' decision.
    payload_id:            Optional[str]  = None

    # In-memory contract text for the deprecated direct_ask() compatibility
    # adapter. When set, the runner uses this as the contract instead of loading
    # contract_path — no file is read and nothing is persisted or registered.
    contract_text:         Optional[str]  = None


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
