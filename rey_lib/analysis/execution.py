"""Running one analysis stage, against the canonical AI runtime.

This is what replaced ``rey_lib.llm.runner``. Roughly half of that module was
provider resolution, capability checking, message building and retry -- all of
which ``rey_lib.ai`` now owns, and none of which came across. What remains here
is the analysis domain's own orchestration:

    idempotency        has this stage already run
    contract           what instruction the stage sends
    preparation        what data it sends, redacted
    execution          -> rey_lib.ai
    result handling    normalize, extract an artifact, validate a schema
    record             one immutable ExecutionRecord

The dependency direction is fixed and one-way::

    rey_lib.analysis -> rey_lib.ai        always
    rey_lib.ai -> rey_lib.analysis        never
    rey_lib.analysis -> its own provider  never

An analysis names a ``profile_id`` and nothing else about how the model is
reached. Which provider and model answer, how a failed call is retried, whether
a replay is legal and what a provider may be sent are the AI runtime's, decided
once at its construction.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from rey_lib.ai import (
    AIError,
    AIOutputSpec,
    AIRequest,
    AIUnavailableError,
)
from rey_lib.analysis.api import RunRequest, RunResponse
from rey_lib.analysis.contract import Contract, load as _load_contract
from rey_lib.analysis.records import (
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_SUCCESS,
    ExecutionRecord,
)
from rey_lib.artifacts import (
    ArtifactEnvelopeError,
    ArtifactProcessingError,
    build_envelope_instruction,
    extract_artifact_envelope,
    loads_llm_json,
    process_artifact,
)
from rey_lib.logs import get_logger
from rey_lib.logs.ai_evaluation import (
    record_evaluation_payload,
    record_evaluation_run,
)

__all__ = ["run"]

_logger = get_logger(__name__)


def run(
    request: RunRequest,
    ai: Any,
    *,
    redactor: Optional[Callable[[str], str]] = None,
    artifact_store: Any = None,
    run_log: Any = None,
    on_text: Optional[Callable[[str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> RunResponse:
    """Execute one analysis stage and answer with a stable response.

    Args:
        request: What analysis to run. Names a profile, never a provider.
        ai: The runtime's shared ``AI``. Taken as an argument rather than
            discovered, so this owns no runtime lookup and no context read.
        redactor: Applied to the input before anything is sent. Redaction is a
            data concern and stays on this side of the AI boundary.
        artifact_store: Where a produced artifact is persisted. AI produces a
            value; the estate's artifact owner names and stores it.
        run_log: The run's ``RunLog``, when evaluation evidence is being
            recorded. ``rey_lib.logs`` owns that recording and RunLog is its
            mechanism, so this module never writes a file and never learns
            whether the destination is a database, JSONL or both.
        on_text: Incremental output, forwarded to the AI runtime.
        cancelled: Cooperative cancellation, forwarded to the AI runtime.

    Returns:
        A ``RunResponse``. ``status`` is one of the ``STATUS_*`` constants; a
        stage with ``requires_approval`` that succeeds returns
        ``pending_approval`` rather than ``success``.
    """
    if ai is None:
        raise AIUnavailableError(
            "This runtime has no AI configured, so an analysis stage cannot run."
        )

    contract = (
        _inline_contract(request.contract_text)
        if request.contract_text
        else _load_contract(request.contract_path)
    )

    raw_text = _input_text(request)
    input_text = redactor(raw_text) if redactor is not None else raw_text
    schema = request.output_schema
    artifact_type = getattr(request, "artifact_type", "") or ""

    instruction_body = contract.body
    if artifact_type:
        instruction_body = f"{instruction_body}\n\n{build_envelope_instruction(artifact_type)}"

    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    clock = time.monotonic()

    # Recorded before the call, so a payload exists even if the execution fails.
    # A caller reusing a saved payload passes its id and it is not re-recorded.
    payload_id = request.payload_id or str(uuid.uuid4())
    if run_log is not None and not request.payload_id:
        record_evaluation_payload(
            run_log,
            payload_id=payload_id,
            payload=_evaluation_payload(input_text),
            created_at=started.isoformat(),
        )

    ai_request = AIRequest.prompt(
        input_text,
        profile_id=request.profile_id,
        instruction=_instruction(instruction_body),
        output=(
            AIOutputSpec.schema_of(schema) if schema and not request.raw_output
            else AIOutputSpec.text()
        ),
        cancelled=cancelled,
    )

    errors: list[str] = []
    raw = ""
    parsed: Any = None
    tokens_in = tokens_out = 0
    status = STATUS_SUCCESS

    try:
        result = (
            _streamed(ai, ai_request, on_text) if on_text is not None
            else ai.execute(ai_request)
        )
        raw = result.text
        parsed = result.value
        tokens_in = result.usage.input_tokens
        tokens_out = result.usage.output_tokens
    except AIError as exc:
        status = STATUS_FAILED
        errors.append(str(exc))

    artifact_uris: list[str] = []
    if status == STATUS_SUCCESS:
        parsed, artifact_errors = _handled(
            raw, parsed, artifact_type, request, artifact_store, run_id,
            artifact_uris,
        )
        if artifact_errors:
            status = STATUS_FAILED
            errors.extend(artifact_errors)

    final_status = status
    if status == STATUS_SUCCESS and request.requires_approval:
        final_status = STATUS_PENDING_APPROVAL

    record = ExecutionRecord(
        run_id=run_id,
        pipeline_id=request.pipeline_id,
        stage_id=request.stage_id,
        contract_name=contract.name,
        contract_version=contract.version,
        contract_hash=contract.hash,
        schema_version=request.schema_version,
        schema_hash=_hashed(json.dumps(schema, sort_keys=True) if schema else ""),
        provider=request.profile_id,
        model="",
        provider_endpoint="",
        input_hash=_hashed(raw_text),
        prompt_hash=_hashed(contract.body),
        rendered_prompt_hash=_hashed(instruction_body),
        started_at=started.isoformat(),
        ended_at=datetime.now(timezone.utc).isoformat(),
        elapsed_ms=int((time.monotonic() - clock) * 1000),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=0.0,
        status=final_status,
        raw_response=raw,
        parsed_response=parsed,
        validation_errors=errors,
        retry_count=0,
        retry_policy="rey_lib.ai",
        idempotency_key=request.idempotency_key or "",
        classification=request.classification,
        artifact_uris=artifact_uris,
        approved_by="",
        approved_at="",
    )

    record_evaluation_run(
        run_log,
        llm_run_id=run_id,
        payload_id=payload_id,
        status=final_status,
        started_at=started.isoformat(),
        profile_id=request.profile_id,
        contract=contract.body,
        contract_version=contract.version,
        result=parsed,
        error=errors,
    )

    return RunResponse(
        run_id=run_id,
        status=final_status,
        parsed_response=parsed,
        raw_text=raw,
        errors=errors,
        record=record,
    )


def _streamed(ai: Any, request: AIRequest, on_text: Callable[[str], None]) -> Any:
    """Run through the AI event stream, forwarding text as it arrives."""
    result = None
    for event in ai.stream(request):
        if event.kind.value == "content_delta" and event.text:
            on_text(event.text)
        if event.result is not None:
            result = event.result
    if result is None:  # pragma: no cover -- stream ends in a result or raises
        raise AIError("The AI execution produced no result.")
    return result


def _handled(
    raw: str,
    parsed: Any,
    artifact_type: str,
    request: RunRequest,
    artifact_store: Any,
    run_id: str,
    artifact_uris: list[str],
) -> tuple[Any, list[str]]:
    """Result handling: artifact extraction, processing and persistence.

    Schema validation is not repeated here. The AI runtime validated the output
    against the contract it was given, and validating twice would mean two
    owners disagreeing about whether a result is acceptable.
    """
    errors: list[str] = []
    if not artifact_type:
        return _normalized(parsed, raw), errors

    try:
        content, _notes = extract_artifact_envelope(raw, artifact_type)
    except ArtifactEnvelopeError as exc:
        return None, [str(exc)]

    try:
        content = process_artifact(
            content, artifact_type, request.artifact_processing,
        )
    except ArtifactProcessingError as exc:
        return None, [str(exc)]

    if artifact_store is not None and content is not None:
        stored = artifact_store.store(
            content, stage_id=request.stage_id, run_id=run_id,
            run_timestamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        )
        if stored:
            artifact_uris.append(str(stored))
    return content, errors


def _normalized(parsed: Any, raw: str) -> Any:
    """One normalized value for shared result persistence."""
    if parsed is not None:
        return parsed
    try:
        decoded = loads_llm_json(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    return decoded if isinstance(decoded, (dict, list)) else raw


def _input_text(request: RunRequest) -> str:
    """The stage's input, rendered as the text an analysis sends."""
    data = request.input_data
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _instruction(body: str) -> Any:
    """The contract body, as an instruction the AI runtime carries."""
    from rey_lib.ai import AIInstruction, AIInstructionKind

    return AIInstruction(kind=AIInstructionKind.RAW, text=body)


def _inline_contract(text: str) -> Contract:
    """A contract supplied as text rather than named as a file."""
    return Contract(
        name="inline", version="0", effective_date="",
        body=text, path=None, hash=_hashed(text),
    )


def _hashed(text: str) -> str:
    """A stable content hash, as the audit trail records one."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _evaluation_payload(input_text: str) -> Any:
    """What an evaluation payload record carries.

    A canonical analysis package is recorded as its source rather than as the
    whole rendered package: the instructions and analysis name are already
    recorded beside it, and repeating them in every payload makes the evidence
    larger without making it say more.
    """
    try:
        package, _ = json.JSONDecoder().raw_decode(input_text.lstrip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return input_text
    if (
        isinstance(package, dict)
        and "analysis_name" in package
        and "instructions" in package
        and "source" in package
    ):
        return package["source"]
    return input_text
