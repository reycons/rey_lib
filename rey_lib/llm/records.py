"""
Execution and approval records for the LLM orchestration framework.

Every stage execution produces one immutable ExecutionRecord.  Every
approval or rejection produces one immutable ApprovalRecord stored
separately from the execution record it references.

Status taxonomy
---------------
pending          Execution has not started.
success          Execution completed and output passed validation.
failed           Execution failed (provider error, parse error, schema mismatch).
pending_approval Execution succeeded but requires human approval before the
                 pipeline may continue.
approved         A human reviewer approved the result.
rejected         A human reviewer rejected the result.
cancelled        Execution was explicitly cancelled before completion.
timeout          Execution exceeded the configured time limit.

Public API
----------
ExecutionRecord
    Immutable record of one stage execution — matches the design contract schema.
ApprovalRecord
    Immutable, separately persisted record of one approval or rejection decision.
approve(record, reviewer, comments)
    Return (updated ExecutionRecord, ApprovalRecord) for an approval decision.
reject(record, reviewer, comments)
    Return (updated ExecutionRecord, ApprovalRecord) for a rejection decision.
store_record(record, path)
    Append an ExecutionRecord to a JSONL file.
store_approval(approval, path)
    Append an ApprovalRecord to a JSONL file.
load_all_records(path)
    Load all ExecutionRecords from a JSONL file.
load_all_approvals(path)
    Load all ApprovalRecords from a JSONL file.
load_latest_record(path, pipeline_id, stage_id)
    Return the most recent ExecutionRecord for a pipeline/stage pair.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    # Status constants
    "STATUS_PENDING",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "STATUS_PENDING_APPROVAL",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "STATUS_CANCELLED",
    "STATUS_TIMEOUT",
    # Record types
    "ExecutionRecord",
    "ApprovalRecord",
    # Record operations
    "approve",
    "reject",
    # Persistence
    "store_record",
    "store_approval",
    "load_all_records",
    "load_all_approvals",
    "load_latest_record",
]

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_PENDING          = "pending"
STATUS_SUCCESS          = "success"
STATUS_FAILED           = "failed"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED         = "approved"
STATUS_REJECTED         = "rejected"
STATUS_CANCELLED        = "cancelled"
STATUS_TIMEOUT          = "timeout"

# ---------------------------------------------------------------------------
# Dataclass types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionRecord:
    """Immutable record of one LLM stage execution.

    Field names and semantics match the design contract schema exactly.
    All fields with defaults are backward-compatible additions — existing
    JSONL records missing those keys are loaded with the default values.

    Attributes
    ----------
    run_id : str
        UUID for this execution run.
    pipeline_id : str
        Logical pipeline identifier.
    stage_id : str
        Stage identifier within the pipeline.
    contract_name : str
        Name field from the contract frontmatter.
    contract_version : str
        Version field from the contract frontmatter.
    contract_hash : str
        SHA-256 of the full contract file content.
    schema_version : str
        Version of the output schema (empty if no schema).
    schema_hash : str
        SHA-256 of the output schema JSON (empty if no schema).
    provider : str
        Provider name (e.g. 'anthropic', 'openai', 'ollama').
    model : str
        Model identifier used for this execution.
    provider_endpoint : str
        Provider endpoint URL (empty for hosted providers).
    input_hash : str
        SHA-256 of the serialised input sent to the LLM.
    prompt_hash : str
        SHA-256 of the contract body (system prompt).
    rendered_prompt_hash : str
        SHA-256 of the full rendered message array sent to the provider.
    started_at : str
        ISO 8601 UTC timestamp when execution began.
    ended_at : str
        ISO 8601 UTC timestamp when execution completed.
    elapsed_ms : int
        Wall-clock execution time in milliseconds.
    tokens_in : int
        Input tokens consumed (0 if provider does not report).
    tokens_out : int
        Output tokens generated (0 if provider does not report).
    cost : float
        Estimated cost in USD (0.0 if not calculated).
    status : str
        One of the STATUS_* constants.
    raw_response : str
        Raw text returned by the provider.
    parsed_response : Optional[dict[str, Any]]
        Validated structured output.  None on failure.
    validation_errors : list[str]
        Schema or parse error messages.  Empty on success.
    retry_count : int
        Number of retries before success or final failure (0 = first try).
    retry_policy : str
        Human-readable description of the retry policy applied.
    idempotency_key : str
        Idempotency key supplied by the caller (empty if none).
    classification : str
        Data classification tag (e.g. 'internal', 'confidential').
    artifact_uris : list[str]
        URIs of associated artifacts stored outside this record.
    approved_by : str
        Reviewer identity if approved or rejected.
    approved_at : str
        ISO 8601 UTC timestamp of the approval/rejection decision.
    """

    # --- identity ---
    run_id:               str
    pipeline_id:          str
    stage_id:             str

    # --- contract ---
    contract_name:        str
    contract_version:     str
    contract_hash:        str
    schema_version:       str = ""
    schema_hash:          str = ""

    # --- provider ---
    provider:             str = ""
    model:                str = ""
    provider_endpoint:    str = ""

    # --- hashes ---
    input_hash:           str = ""
    prompt_hash:          str = ""
    rendered_prompt_hash: str = ""

    # --- timing ---
    started_at:           str = ""
    ended_at:             str = ""
    elapsed_ms:           int = 0

    # --- telemetry ---
    tokens_in:            int   = 0
    tokens_out:           int   = 0
    cost:                 float = 0.0

    # --- result ---
    status:               str                        = STATUS_PENDING
    raw_response:         str                        = ""
    parsed_response:      Optional[dict[str, Any]]  = None
    validation_errors:    list[str]                  = field(default_factory=list)

    # --- retry ---
    retry_count:          int = 0
    retry_policy:         str = ""

    # --- governance ---
    idempotency_key:      str         = ""
    classification:       str         = ""
    artifact_uris:        list[str]   = field(default_factory=list)

    # --- approval ---
    approved_by:          str = ""
    approved_at:          str = ""


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable record of one human approval or rejection decision.

    Stored in a separate JSONL file from execution records so the approval
    audit trail is independently queryable.

    Attributes
    ----------
    approval_id : str
        UUID for this approval record.
    run_id : str
        UUID of the associated ExecutionRecord.
    stage_id : str
        Stage identifier that was reviewed.
    decision : str
        'approved' or 'rejected'.
    reviewer : str
        Identity of the reviewer (name, email, or system ID).
    reviewed_at : str
        ISO 8601 UTC timestamp of the decision.
    comments : str
        Optional reviewer comments.
    previous_status : str
        Status of the execution record before this decision.
    new_status : str
        Status of the execution record after this decision.
    """

    approval_id:     str
    run_id:          str
    stage_id:        str
    decision:        str
    reviewer:        str
    reviewed_at:     str
    comments:        str = ""
    previous_status: str = ""
    new_status:      str = ""


# Field name sets for forward-compatible loading.
_RECORD_FIELDS   = frozenset(f.name for f in dataclasses.fields(ExecutionRecord))
_APPROVAL_FIELDS = frozenset(f.name for f in dataclasses.fields(ApprovalRecord))


# ---------------------------------------------------------------------------
# Record operations
# ---------------------------------------------------------------------------

def approve(
    record:   ExecutionRecord,
    reviewer: str,
    comments: str = "",
) -> tuple[ExecutionRecord, ApprovalRecord]:
    """Record a human approval decision.

    Returns both the updated ExecutionRecord and the new ApprovalRecord.
    The caller is responsible for persisting both via store_record() and
    store_approval().

    Parameters
    ----------
    record : ExecutionRecord
        The record being approved.
    reviewer : str
        Identity of the approver (name, email, or system ID).
    comments : str
        Optional approval notes.

    Returns
    -------
    tuple[ExecutionRecord, ApprovalRecord]
        Updated execution record and new approval record.
    """
    now      = datetime.now(timezone.utc).isoformat()
    approval = ApprovalRecord(
        approval_id     = str(uuid.uuid4()),
        run_id          = record.run_id,
        stage_id        = record.stage_id,
        decision        = "approved",
        reviewer        = reviewer,
        reviewed_at     = now,
        comments        = comments,
        previous_status = record.status,
        new_status      = STATUS_APPROVED,
    )
    updated = dataclasses.replace(
        record,
        status      = STATUS_APPROVED,
        approved_by = reviewer,
        approved_at = now,
    )
    return updated, approval


def reject(
    record:   ExecutionRecord,
    reviewer: str,
    comments: str = "",
) -> tuple[ExecutionRecord, ApprovalRecord]:
    """Record a human rejection decision.

    Returns both the updated ExecutionRecord and the new ApprovalRecord.
    The caller is responsible for persisting both via store_record() and
    store_approval().

    Parameters
    ----------
    record : ExecutionRecord
        The record being rejected.
    reviewer : str
        Identity of the reviewer.
    comments : str
        Optional rejection reason.

    Returns
    -------
    tuple[ExecutionRecord, ApprovalRecord]
        Updated execution record and new approval record.
    """
    now      = datetime.now(timezone.utc).isoformat()
    approval = ApprovalRecord(
        approval_id     = str(uuid.uuid4()),
        run_id          = record.run_id,
        stage_id        = record.stage_id,
        decision        = "rejected",
        reviewer        = reviewer,
        reviewed_at     = now,
        comments        = comments,
        previous_status = record.status,
        new_status      = STATUS_REJECTED,
    )
    updated = dataclasses.replace(
        record,
        status      = STATUS_REJECTED,
        approved_by = reviewer,
        approved_at = now,
    )
    return updated, approval


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def store_record(record: ExecutionRecord, path: Path) -> None:
    """Append an ExecutionRecord to a JSONL file.

    Creates the file and parent directories if they do not exist.

    Parameters
    ----------
    record : ExecutionRecord
    path : Path
        Target JSONL file path.
    """
    _append_jsonl(asdict(record), path)


def store_approval(approval: ApprovalRecord, path: Path) -> None:
    """Append an ApprovalRecord to a JSONL file.

    Creates the file and parent directories if they do not exist.

    Parameters
    ----------
    approval : ApprovalRecord
    path : Path
        Target JSONL file path.  Convention: use log_path.with_suffix('')
        + '.approvals.jsonl' to keep approvals alongside records.
    """
    _append_jsonl(asdict(approval), path)


def load_all_records(path: Path) -> list[ExecutionRecord]:
    """Read all ExecutionRecords from a JSONL file.

    Unknown keys are silently ignored for forward compatibility.
    Returns an empty list if the file does not exist.

    Parameters
    ----------
    path : Path

    Returns
    -------
    list[ExecutionRecord]
        Records in file order (oldest first).
    """
    return [
        ExecutionRecord(**{k: v for k, v in row.items() if k in _RECORD_FIELDS})
        for row in _read_jsonl(path)
    ]


def load_all_approvals(path: Path) -> list[ApprovalRecord]:
    """Read all ApprovalRecords from a JSONL file.

    Unknown keys are silently ignored for forward compatibility.
    Returns an empty list if the file does not exist.

    Parameters
    ----------
    path : Path

    Returns
    -------
    list[ApprovalRecord]
        Approval records in file order (oldest first).
    """
    return [
        ApprovalRecord(**{k: v for k, v in row.items() if k in _APPROVAL_FIELDS})
        for row in _read_jsonl(path)
    ]


def load_latest_record(
    path:        Path,
    pipeline_id: str,
    stage_id:    str,
) -> Optional[ExecutionRecord]:
    """Return the most recent ExecutionRecord for a pipeline/stage pair.

    Parameters
    ----------
    path : Path
    pipeline_id : str
    stage_id : str

    Returns
    -------
    Optional[ExecutionRecord]
        Most recent matching record, or None.
    """
    matches = [
        r for r in load_all_records(path)
        if r.pipeline_id == pipeline_id and r.stage_id == stage_id
    ]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _append_jsonl(data: dict[str, Any], path: Path) -> None:
    """Append one JSON object as a line to a JSONL file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all non-empty lines from a JSONL file as dicts."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
