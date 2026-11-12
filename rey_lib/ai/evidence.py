"""What AI records of an execution it was asked for under a caller's parent (#1305).

An application that asks for a task through ApplicationAI supplies its own place
-- its run, the record a new record is parented to, its nesting and correlation
(``RunLog.parent_context``) -- and the task carries it as its ``origin_id``, a
string fact the task itself never interprets. This module is what interprets it,
for exactly one purpose: AI records what it was sent and what it answered under
that place, and under nothing else.

The package is the one the finalization stage used to write, in its structure
and field names: ``analysis_name``, ``source_record_type``, ``instructions`` (the
task's contract), ``references`` (what the contract declares, loaded) and
``source`` (the record the application sent). What changed is who creates and
records it -- the AI, at the run -- not what it means.
"""

from __future__ import annotations

import json

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Sequence

from rey_lib.ai.errors import AIRequestError
from rey_lib.config.config_loader import parse_yaml
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "FAILURE_RECORD",
    "INTERPRETATION_RECORD",
    "INTERPRETATION_TASK",
    "PACKAGE_RECORD",
    "RESULT_RECORD",
    "failure_of",
    "origin_of",
    "package_of",
    "result_of",
    "trace_of",
]

#: The record types AI writes, as the run log already declares them (#1305):
#: the package it was sent; the completed interpretation of a run log; the
#: completed result of any other task; a failed or withdrawn execution.
PACKAGE_RECORD = "LLM_PACKAGE"
INTERPRETATION_RECORD = "LLM_INTERPRETATION"
RESULT_RECORD = "LLM_ANALYSIS_RESULT"
FAILURE_RECORD = "LLM_ANALYSIS_FAILURE"

#: The governed task whose completed answer is an interpretation.
INTERPRETATION_TASK = "log_interpretation"

#: What a caller's origin must name for AI to record under it.
_ORIGIN_REQUIRED = ("run_id", "parent_run_log_id")


def origin_of(task: Any) -> dict[str, Any] | None:
    """The caller's place a task carries as its ``origin_id``, read once, or None.

    None when the task carries no origin: nothing is recorded, and the task runs
    as any other. An origin that is present but is not the execution context it
    must be -- not a mapping, or naming no run or no parent record -- is refused
    before anything runs: evidence is never recorded under a substituted parent.

    Raises:
        AIRequestError: When the origin is malformed or incomplete.
    """
    text = str(getattr(task, "origin_id", "") or "").strip()
    if not text:
        return None
    key = getattr(task, "task_key", "") or "(default)"
    try:
        given = json.loads(text)
    except ValueError as malformed:
        raise AIRequestError(
            f"AI task '{key}' carries an origin that is not an execution context: {text[:80]!r}."
        ) from malformed
    if not isinstance(given, Mapping):
        raise AIRequestError(
            f"AI task '{key}' carries an origin that is not an execution context: {text[:80]!r}."
        )
    missing = [name for name in _ORIGIN_REQUIRED if given.get(name) in (None, "")]
    if missing:
        raise AIRequestError(
            f"AI task '{key}' carries an origin naming no {', '.join(missing)}; "
            "its evidence is recorded under no other run."
        )
    try:
        run_id = int(given["run_id"])
        parent_run_log_id = int(given["parent_run_log_id"])
        nest_level = int(given.get("nest_level") or 0)
    except (TypeError, ValueError) as invalid:
        raise AIRequestError(
            f"AI task '{key}' carries an origin whose run, parent record or nesting is not "
            f"a number: {text[:80]!r}."
        ) from invalid
    return {
        "run_id": run_id,
        "parent_run_log_id": parent_run_log_id,
        "nest_level": nest_level,
        "correlation_id": str(given.get("correlation_id") or ""),
        "app": str(given.get("app") or ""),
    }


def package_of(task: Any, source: Mapping[str, Any] | None, source_record_type: str,
               runtime: Any, *, task_run_id: str = "") -> dict[str, Any]:
    """The LLM package AI is sent, in the structure the finalization stage wrote.

    ``analysis_name`` is the task's instruction (``log_interpreter`` for the log
    interpretation); ``source_record_type`` what the application named its record;
    ``instructions`` the task's contract as the task holds it, parsed as the
    config loader parses governed YAML (a body that is not a mapping is carried as
    its text); ``references`` what that contract declares, loaded through the one
    existing loader, where it declares any; ``source`` the record the application
    sent. Nothing else. ``runtime`` is what resolves a declared reference's path
    (the loader reads its ``paths``); None loads none.
    """
    from rey_lib.logs.llm_package import load_contract_references

    instructions = _contract_of(task)
    package: dict[str, Any] = {
        "analysis_name": str(getattr(task, "instruction_key", "") or ""),
        "source_record_type": str(source_record_type or ""),
        "instructions": instructions,
    }
    declared = instructions.get("references")
    references = (load_contract_references(runtime, declared)
                  if runtime is not None and declared else None)
    if references:
        package["references"] = references
    # THE SHARED INSTRUCTIONS THE MODEL WAS GIVEN (#1337, #1344): each one's identity,
    # version and the body exactly as sent, in the order it was sent -- what the model
    # received, not a pointer to a contract that may change. None given, no key.
    shared = [
        {"instruction_key": str(one.get("instruction_key") or ""),
         "contract_key": str(one.get("contract_key") or ""),
         "contract_version": str(one.get("contract_version") or ""),
         "body": str(one.get("body") or "")}
        for one in (getattr(task, "instructions", ()) or ())
    ]
    if shared:
        package["shared_instructions"] = shared
    package["source"] = dict(source) if source is not None else None
    _correlated(package, task, task_run_id)
    return package


def _correlated(record: dict[str, Any], task: Any, task_run_id: str = "") -> dict[str, Any]:
    """The task run this record belongs to, where the task has one (#1344).

    Filed under the caller's parent or the hosting application's own run alike, the
    record joins its task run, trace and conversation by this id; no run, no key.
    ``task_run_id`` is the run as named before execution began (as ``trace_of``
    takes it): the task's own ``run_id`` is cleared while the engine runs.
    """
    named = str(task_run_id or getattr(task, "run_id", "") or "")
    if named:
        record["task_run_id"] = named
    return record


def result_of(task: Any, result: Any, *, task_run_id: str = "") -> dict[str, Any]:
    """What came back, as the completed execution states it."""
    execution = result.execution
    output = result.output
    finish = getattr(execution, "finish_reason", "")
    return _correlated({
        "execution_id": str(getattr(execution, "execution_id", "") or ""),
        "analysis_name": str(getattr(task, "instruction_key", "") or ""),
        "finish_reason": str(getattr(finish, "value", finish) or ""),
        "media_type": str(getattr(output, "media_type", "") or ""),
        "output": result.value if result.value is not None else result.text,
        # What the model answered, exactly as received, and every try before it.
        "raw_output": str(getattr(output, "raw", "") or ""),
        "attempts": [_attempt(one) for one in getattr(execution, "attempts", ())],
        "usage": {"input_tokens": int(result.usage.input_tokens),
                  "output_tokens": int(result.usage.output_tokens)},
        "tool_calls": [_plain(call) for call in result.tool_calls],
        "tool_results": [_plain(answer) for answer in result.tool_results],
        "prompt_digest": str(getattr(result.evidence, "prompt_digest", "") or ""),
        "started_at": str(getattr(execution, "started_at", "") or ""),
        "ended_at": str(getattr(execution, "ended_at", "") or ""),
    }, task, task_run_id)


def failure_of(task: Any, status: str, error: str,
               attempts: Sequence[Any] = (), *, task_run_id: str = "") -> dict[str, Any]:
    """Why the execution ended without an answer: failed, or cancelled.

    With the tries it made (``AIExecutionInfo.attempts``), the model's last answer
    exactly as received is ``raw_output``, and every try is kept beside it.
    """
    tries = list(attempts)
    return _correlated({
        "analysis_name": str(getattr(task, "instruction_key", "") or ""),
        "status": str(status),
        "error": str(error),
        **({"raw_output": str(getattr(tries[-1], "raw_output", "") or ""),
            "attempts": [_attempt(one) for one in tries]} if tries else {}),
    }, task, task_run_id)


def _attempt(attempt: Any) -> dict[str, Any]:
    """One try as the record keeps it: its number, how it ended, what was answered."""
    return {"number": int(getattr(attempt, "number", 0)),
            "failed": bool(getattr(attempt, "failed", False)),
            "error": str(getattr(attempt, "error", "") or ""),
            "raw_output": str(getattr(attempt, "raw_output", "") or "")}


def _contract_of(task: Any) -> dict[str, Any]:
    """The task's contract as the task holds it: its key and version, and its body
    parsed as the config loader parses governed YAML."""
    body = str(getattr(task, "contract_body", "") or "")
    contract: dict[str, Any] = {
        "contract_key": str(getattr(task, "contract_key", "") or ""),
        "contract_version": str(getattr(task, "contract_version", "") or ""),
    }
    parsed: Any = None
    if body.strip():
        try:
            parsed = parse_yaml(body)
        except ConfigError:
            parsed = None
    if isinstance(parsed, Mapping):
        contract.update({str(name): value for name, value in parsed.items()})
    elif body:
        contract["body"] = body
    return contract


def _plain(value: Any) -> Any:
    """A tool call or result as plain data."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def trace_of(task: Any, event: Any, *, task_run_id: str = "") -> dict[str, Any] | None:
    """A canonical tool/lifecycle event for the existing typed run log.

    Content deltas are deliberately not records. Calls, returned evidence and
    terminal outcomes survive failed executions, correlated to the actual task
    run even when the hosting process is the only available logging parent.
    """
    from rey_lib.ai.streaming import AIEventKind
    kinds = {AIEventKind.EXECUTION_STARTED, AIEventKind.TOOL_CALL_REQUESTED,
             AIEventKind.TOOL_RESULT_ACCEPTED, AIEventKind.EXECUTION_COMPLETED,
             AIEventKind.EXECUTION_FAILED, AIEventKind.EXECUTION_CANCELLED}
    if event.kind not in kinds:
        return None
    projected = event.projection()
    projected.pop("result", None)  # the ordinary result record owns the final answer
    return {"task_id": task.id, "task_key": task.task_key,
            "task_run_id": task_run_id or task.run_id, "model_identifier": task.model_identifier,
            "max_turns": task.max_turns, "event": projected}
