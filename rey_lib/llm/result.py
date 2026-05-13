"""
EvaluationResult — the common envelope for all LLM workflow outputs.

Every stage of every use case produces one EvaluationResult. The envelope
carries the audit fields (contract version, model, hashes, review status,
token usage, retry count) and the use-case-specific payload in ``result``.

Results are persisted as line-delimited JSON (one JSON object per line)
to a configured file path. This keeps the storage format simple, portable,
and appendable without a database dependency.

Public API
----------
EvaluationResult
    Frozen dataclass representing one stage evaluation.
new(...)
    Construct a new EvaluationResult with a generated ID and timestamp.
approve(result, reviewer)
    Return a copy with review_status='approved'.
reject(result, reviewer, reason)
    Return a copy with review_status='rejected'.
store(result, path)
    Append a result to a JSONL file.
load_all(path)
    Read all results from a JSONL file.
load_latest(path, use_case, stage)
    Return the most recent result for a given use_case + stage.
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
    "EvaluationResult",
    "new",
    "approve",
    "reject",
    "store",
    "load_all",
    "load_latest",
]

# Field names present in EvaluationResult — used for forward-compatible loading.
_KNOWN_FIELDS: frozenset[str] = frozenset()  # populated after class definition


@dataclass(frozen=True)
class EvaluationResult:
    """
    Common envelope for every LLM workflow stage output.

    Attributes
    ----------
    evaluation_id : str
        UUID for this specific evaluation run.
    use_case : str
        Name of the use case (e.g. 'trade_pattern_analysis').
    stage : str
        Stage name within the use case (e.g. 'analysis', 'criteria_extraction').
    contract_name : str
        Name field from the contract frontmatter.
    contract_version : str
        Version field from the contract frontmatter.
    contract_hash : str
        SHA-256 of the full contract file content.
    model : str
        LLM model identifier used for this evaluation.
    evaluated_at : str
        ISO 8601 UTC timestamp.
    input_hash : str
        SHA-256 of the serialised input sent to the LLM.
    result : dict[str, Any]
        Use-case-specific structured payload.
    provider : str
        Provider name (e.g. 'anthropic', 'openai').
    tokens_in : int
        Input tokens consumed.
    tokens_out : int
        Output tokens generated.
    retry_count : int
        Number of retry attempts before a successful response (0 = first try).
    prompt_hash : str
        SHA-256 of the rendered prompt sent to the provider.
    raw_response : str
        Raw text response from the provider for audit/replay.
    validation_errors : list[str]
        Schema or validation error messages from the final attempt.
    review_status : str
        'pending' | 'approved' | 'rejected'
    reviewed_by : Optional[str]
        Identity of the reviewer (set externally after review).
    reviewed_at : Optional[str]
        ISO 8601 UTC timestamp of review (set externally after review).
    """

    # --- identity & contract ---
    evaluation_id:    str
    use_case:         str
    stage:            str
    contract_name:    str
    contract_version: str
    contract_hash:    str

    # --- execution ---
    model:            str
    evaluated_at:     str
    input_hash:       str
    result:           dict[str, Any]

    # --- provider telemetry (defaulted for backward compat) ---
    provider:          str           = ""
    tokens_in:         int           = 0
    tokens_out:        int           = 0
    retry_count:       int           = 0
    prompt_hash:       str           = ""
    raw_response:      str           = ""
    validation_errors: list[str]     = field(default_factory=list)

    # --- review ---
    review_status:     str           = "pending"
    reviewed_by:       Optional[str] = None
    reviewed_at:       Optional[str] = None


# Build the known-fields set after the class is defined.
_KNOWN_FIELDS = frozenset(f.name for f in dataclasses.fields(EvaluationResult))


def new(
    use_case:          str,
    stage:             str,
    contract_name:     str,
    contract_version:  str,
    contract_hash:     str,
    model:             str,
    input_hash:        str,
    result:            dict[str, Any],
    provider:          str       = "",
    tokens_in:         int       = 0,
    tokens_out:        int       = 0,
    retry_count:       int       = 0,
    prompt_hash:       str       = "",
    raw_response:      str       = "",
    validation_errors: list[str] | None = None,
) -> EvaluationResult:
    """
    Construct a new EvaluationResult with a generated ID and current timestamp.

    Parameters
    ----------
    use_case : str
        Name of the use case.
    stage : str
        Stage name within the use case.
    contract_name : str
        Contract name from frontmatter.
    contract_version : str
        Contract version from frontmatter.
    contract_hash : str
        SHA-256 of contract file content.
    model : str
        LLM model identifier.
    input_hash : str
        SHA-256 of the serialised input.
    result : dict[str, Any]
        Structured LLM output payload.
    provider : str
        Provider name.
    tokens_in : int
        Input tokens consumed.
    tokens_out : int
        Output tokens generated.
    retry_count : int
        Retry attempts before success.
    prompt_hash : str
        SHA-256 of the rendered prompt.
    raw_response : str
        Raw provider response text.
    validation_errors : list[str] | None
        Validation error messages.  None is normalised to [].

    Returns
    -------
    EvaluationResult
    """
    return EvaluationResult(
        evaluation_id     = str(uuid.uuid4()),
        use_case          = use_case,
        stage             = stage,
        contract_name     = contract_name,
        contract_version  = contract_version,
        contract_hash     = contract_hash,
        model             = model,
        evaluated_at      = datetime.now(timezone.utc).isoformat(),
        input_hash        = input_hash,
        result            = result,
        provider          = provider,
        tokens_in         = tokens_in,
        tokens_out        = tokens_out,
        retry_count       = retry_count,
        prompt_hash       = prompt_hash,
        raw_response      = raw_response,
        validation_errors = validation_errors or [],
        review_status     = "pending",
    )


def approve(
    result:   EvaluationResult,
    reviewer: str,
) -> EvaluationResult:
    """Return a new EvaluationResult with review_status set to 'approved'.

    The original result is unchanged.  The returned result should be
    persisted with store() so the approval is recorded in the audit log.
    The same evaluation_id is preserved so the approval can be traced back
    to the original evaluation.

    Parameters
    ----------
    result : EvaluationResult
        The evaluation to approve.
    reviewer : str
        Identity of the approver (name, email, or system ID).

    Returns
    -------
    EvaluationResult
        Copy with review_status='approved', reviewed_by, and reviewed_at set.
    """
    return dataclasses.replace(
        result,
        review_status = "approved",
        reviewed_by   = reviewer,
        reviewed_at   = datetime.now(timezone.utc).isoformat(),
    )


def reject(
    result:   EvaluationResult,
    reviewer: str,
    reason:   str = "",
) -> EvaluationResult:
    """Return a new EvaluationResult with review_status set to 'rejected'.

    The original result is unchanged.  The returned result should be
    persisted with store() so the rejection is recorded in the audit log.

    Parameters
    ----------
    result : EvaluationResult
        The evaluation to reject.
    reviewer : str
        Identity of the reviewer.
    reason : str
        Optional rejection reason, stored in the result payload under
        'review_rejection_reason'.

    Returns
    -------
    EvaluationResult
        Copy with review_status='rejected', reviewed_by, reviewed_at, and
        optional reason merged into the result payload.
    """
    payload = dict(result.result)
    if reason:
        payload["review_rejection_reason"] = reason
    return dataclasses.replace(
        result,
        review_status = "rejected",
        reviewed_by   = reviewer,
        reviewed_at   = datetime.now(timezone.utc).isoformat(),
        result        = payload,
    )


def store(result: EvaluationResult, path: Path) -> None:
    """
    Append a result to a line-delimited JSON file.

    Creates the file and its parent directories if they do not exist.

    Parameters
    ----------
    result : EvaluationResult
        The result to persist.
    path : Path
        Target JSONL file path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(result), default=str) + "\n")


def load_all(path: Path) -> list[EvaluationResult]:
    """
    Read all results from a JSONL file.

    Unknown fields in stored records are silently ignored so that older
    records can be loaded after schema additions.

    Returns an empty list if the file does not exist.

    Parameters
    ----------
    path : Path
        JSONL file path.

    Returns
    -------
    list[EvaluationResult]
        All stored results in file order (oldest first).
    """
    path = Path(path)
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        data    = json.loads(line)
        # Drop keys not in the current schema to stay forward-compatible.
        filtered = {k: v for k, v in data.items() if k in _KNOWN_FIELDS}
        results.append(EvaluationResult(**filtered))
    return results


def load_latest(
    path:     Path,
    use_case: str,
    stage:    str,
) -> Optional[EvaluationResult]:
    """
    Return the most recent result for a given use_case + stage combination.

    Parameters
    ----------
    path : Path
        JSONL file path.
    use_case : str
        Use case name to filter by.
    stage : str
        Stage name to filter by.

    Returns
    -------
    Optional[EvaluationResult]
        Most recent matching result, or None if none found.
    """
    matches = [
        r for r in load_all(path)
        if r.use_case == use_case and r.stage == stage
    ]
    return matches[-1] if matches else None
