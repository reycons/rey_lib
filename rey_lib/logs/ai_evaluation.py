"""Recording what an AI execution produced, as run evidence.

The ownership this settles, stated once:

    rey_lib.ai      emits canonical evidence -- AIEvidence, results, events.
                    It persists nothing.
    rey_lib.logs    owns recording that evidence.
    RunLog          is the persistence mechanism, and the configured
                    destination -- database, JSONL, or both -- is its decision.

The old subsystem appended JSONL files directly from inside the LLM runner,
which made the execution owner also a persistence owner and pinned the evidence
to one storage medium. Ownership here is about the logging domain, not the
medium: these go through ``RunLog.append`` like every other run record, so they
follow the run's configured destination without this module knowing what it is.

``LLM_EVALUATION_PAYLOAD`` and ``LLM_EVALUATION_RUN`` remain the canonical record
types -- both are already declared in the run-log vocabulary
(``run_log.py``) and read by the Console's LLM Evaluation tree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["record_evaluation_payload", "record_evaluation_run"]

#: The canonical record types for AI evaluation evidence.
EVALUATION_PAYLOAD = "LLM_EVALUATION_PAYLOAD"
EVALUATION_RUN = "LLM_EVALUATION_RUN"


def record_evaluation_payload(
    run_log: Any,
    *,
    payload_id: str,
    payload: Any,
    created_at: str = "",
) -> int | None:
    """Record the payload one evaluation was run against.

    Written once per payload. A caller reusing a saved payload passes its
    existing ``payload_id`` and does not record it again, which is what keeps
    the log append-only rather than duplicating a payload per run.

    Args:
        run_log: The run's ``RunLog``. ``None`` records nothing, so a caller
            with no run log is not forced to invent one.
        payload_id: The stable identity of this payload.
        payload: What was sent, as data.
        created_at: When, ISO-8601. Defaults to now.

    Returns:
        The minted ``control.run_log.run_log_id``, or ``None`` when nothing was
        recorded -- including when ``run_log`` is absent.
    """
    if run_log is None:
        return None
    return run_log.append(
        EVALUATION_PAYLOAD,
        payload_id=str(payload_id),
        created_at=created_at or _now(),
        payload=payload,
    )


def record_evaluation_run(
    run_log: Any,
    *,
    llm_run_id: str,
    payload_id: str,
    status: str,
    started_at: str,
    completed_at: str = "",
    profile_id: str = "",
    model: str = "",
    provider: str = "",
    contract: str = "",
    contract_version: str = "",
    result: Any = None,
    error: Any = None,
) -> int | None:
    """Record one evaluation run against a payload.

    ``llm_run_id`` reuses the execution's own run id and links to the payload,
    so an evaluation is traceable to exactly what it was given.
    """
    if run_log is None:
        return None
    return run_log.append(
        EVALUATION_RUN,
        llm_run_id=str(llm_run_id),
        payload_id=str(payload_id),
        started_at=started_at,
        completed_at=completed_at or _now(),
        status=status,
        execution_profile=profile_id,
        model=model,
        provider=provider,
        contract=contract,
        contract_version=contract_version,
        result=result,
        error=error or [],
    )


def _now() -> str:
    """The current instant, as records spell one."""
    return datetime.now(timezone.utc).isoformat()
