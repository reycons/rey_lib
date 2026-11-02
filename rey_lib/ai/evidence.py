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
from typing import Any, Mapping

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
               runtime: Any) -> dict[str, Any]:
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
    package["source"] = dict(source) if source is not None else None
    return package


def result_of(task: Any, result: Any) -> dict[str, Any]:
    """What came back, as the completed execution states it."""
    execution = result.execution
    output = result.output
    finish = getattr(execution, "finish_reason", "")
    return {
        "execution_id": str(getattr(execution, "execution_id", "") or ""),
        "analysis_name": str(getattr(task, "instruction_key", "") or ""),
        "finish_reason": str(getattr(finish, "value", finish) or ""),
        "media_type": str(getattr(output, "media_type", "") or ""),
        "output": result.value if result.value is not None else result.text,
        "usage": {"input_tokens": int(result.usage.input_tokens),
                  "output_tokens": int(result.usage.output_tokens)},
        "tool_calls": [_plain(call) for call in result.tool_calls],
        "tool_results": [_plain(answer) for answer in result.tool_results],
        "prompt_digest": str(getattr(result.evidence, "prompt_digest", "") or ""),
        "started_at": str(getattr(execution, "started_at", "") or ""),
        "ended_at": str(getattr(execution, "ended_at", "") or ""),
    }


def failure_of(task: Any, status: str, error: str) -> dict[str, Any]:
    """Why the execution ended without an answer: failed, or cancelled."""
    return {
        "analysis_name": str(getattr(task, "instruction_key", "") or ""),
        "status": str(status),
        "error": str(error),
    }


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
