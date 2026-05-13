"""
Multi-stage LLM workflow pipeline.

A Pipeline is an ordered list of Stage definitions.  Each stage runs
via runner.run() and produces a RunResponse.

Approval semantics
------------------
When a stage has requires_approval=True and it succeeds, the pipeline:
1. Overwrites the stored record status to 'pending_approval'.
2. Returns a RunResponse with status='pending_approval' and the stage output.
3. Stops — subsequent stages are not run.

The human reviewer then calls records.approve()/reject() + store_record()
to persist the decision.  Calling resume() re-runs the pipeline, skipping
any stage whose latest record has status='approved', and stopping again if
a stage is still 'pending_approval'.

Replay
------
Call resume() to replay a pipeline.  Approved stages are skipped and their
stored parsed_response is forwarded as input to the next stage.  Any stage
that is still pending_approval causes an early return with that status.

Hooks
-----
Pass a PipelineHooks instance to receive callbacks at stage lifecycle points::

    hooks = PipelineHooks(
        pre_stage  = lambda sid, data: ...,
        post_stage = lambda sid, resp: ...,
    )

Public API
----------
PipelineHooks
    Named callbacks fired at stage lifecycle points.
Stage
    Definition of one pipeline stage.
Pipeline
    Ordered collection of stages with a shared log.
Pipeline.run(initial_data, pipeline_id)
    Execute all stages in sequence.
Pipeline.resume(initial_data, pipeline_id)
    Re-execute, skipping stages whose latest record is 'approved'.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rey_lib.llm.api import RunRequest, RunResponse
from rey_lib.llm.artifacts import ArtifactStore
from rey_lib.llm.locking import PipelineLock
from rey_lib.llm.records import (
    STATUS_APPROVED,
    STATUS_PENDING_APPROVAL,
    load_latest_record,
)
from rey_lib.llm.redaction import RedactionFilter
from rey_lib.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from rey_lib.llm.runner import run
from rey_lib.logs.log_utils import get_logger

__all__ = ["PipelineHooks", "Stage", "Pipeline"]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class PipelineHooks:
    """Lifecycle callbacks for pipeline stage execution.

    All fields are optional — pass only the hooks you need.

    Attributes
    ----------
    pre_stage : Optional[Callable[[str, Any], None]]
        Called with (stage_id, input_data) before each stage runs.
    post_stage : Optional[Callable[[str, RunResponse], None]]
        Called with (stage_id, response) after each stage completes.
    on_approval_required : Optional[Callable[[str, RunResponse], None]]
        Called with (stage_id, response) when a stage pauses for approval.
    """

    pre_stage:            Optional[Callable[[str, Any], None]]          = None
    post_stage:           Optional[Callable[[str, RunResponse], None]]  = None
    on_approval_required: Optional[Callable[[str, RunResponse], None]]  = None


@dataclass
class Stage:
    """Definition of one pipeline stage.

    Attributes
    ----------
    stage_id : str
        Stage identifier stored in the ExecutionRecord.
    contract_path : Path
        Path to the versioned contract markdown file.
    requires_approval : bool
        When True, the stage result is stored as 'pending_approval' and the
        pipeline stops after this stage until a human approves the result.
    transform : Optional[Callable[[dict], Any]]
        Called with the previous stage's parsed_response.  Return value
        is passed as input_data to this stage.  When omitted, the full
        previous parsed_response is JSON-serialised and used as input.
    output_schema : Optional[dict]
        JSON Schema for output validation.  When omitted, the runner
        looks for a sidecar .schema.json file.
    schema_version : str
        Schema version tag stored in the execution record.
    max_tokens : int
        Maximum tokens the provider may generate.
    max_rows : int
        Maximum rows when input is a list of dicts.
    retry_policy : RetryPolicy
        Retry behaviour for this stage.
    classification : str
        Data classification tag stored in the execution record.
    """

    stage_id:          str
    contract_path:     Path
    requires_approval: bool                              = False
    transform:         Optional[Callable[[dict], Any]]  = None
    output_schema:     Optional[dict[str, Any]]         = None
    schema_version:    str                               = ""
    max_tokens:        int                               = 4000
    max_rows:          int                               = 200
    retry_policy:      RetryPolicy                       = field(
        default_factory=lambda: DEFAULT_RETRY_POLICY
    )
    classification:    str                               = ""


class Pipeline:
    """Execute an ordered sequence of LLM workflow stages.

    Parameters
    ----------
    stages : list[Stage]
        Ordered stage definitions.
    log : Path
        JSONL file where all ExecutionRecords are appended.
    provider : str
        Provider name for all stages.
    model : str
        Model identifier for all stages.
    api_key : str
        API key for all stages.
    hooks : Optional[PipelineHooks]
        Lifecycle callbacks.  None disables all hooks.
    """

    def __init__(
        self,
        stages:           list[Stage],
        log:              Path,
        provider:         str                        = "",
        model:            str                        = "",
        api_key:          str                        = "",
        hooks:            Optional[PipelineHooks]    = None,
        redaction_filter: Optional[RedactionFilter]  = None,
        artifact_store:   Optional[ArtifactStore]    = None,
        use_lock:         bool                       = True,
    ) -> None:
        """Initialise the pipeline."""
        if not stages:
            raise ValueError("Pipeline requires at least one stage.")
        self._stages           = stages
        self._log              = Path(log)
        self._provider         = provider
        self._model            = model
        self._api_key          = api_key
        self._hooks            = hooks or PipelineHooks()
        self._redaction_filter = redaction_filter
        self._artifact_store   = artifact_store
        self._use_lock         = use_lock

    def run(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
        pipeline_id:  str,
    ) -> list[RunResponse]:
        """Execute all stages in sequence.

        When a stage has requires_approval=True and succeeds, its record is
        stored as 'pending_approval' and the pipeline returns early.  Call
        resume() after the record has been approved to continue.

        Parameters
        ----------
        initial_data : str | list[dict]
            Starting input for the first stage.
        pipeline_id : str
            Logical pipeline identifier stored in every ExecutionRecord.

        Returns
        -------
        list[RunResponse]
            One response per executed stage.  May be shorter than the full
            stage list if the pipeline paused for approval.
        """
        return self._execute(
            stages        = self._stages,
            initial_data  = initial_data,
            pipeline_id   = pipeline_id,
            skip_approved = False,
        )

    def resume(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
        pipeline_id:  str,
    ) -> list[RunResponse]:
        """Re-execute the pipeline, skipping stages already approved in the log.

        Reads the JSONL log and skips any stage whose latest ExecutionRecord
        for this pipeline_id + stage_id has status='approved'.  Stops again
        if a stage is 'pending_approval' (still awaiting review).

        Parameters
        ----------
        initial_data : str | list[dict]
            Starting input (same value as the original run() call).
        pipeline_id : str
            Must match the pipeline_id used in the original run() call.

        Returns
        -------
        list[RunResponse]
            Results for each stage executed or replayed.
        """
        return self._execute(
            stages        = self._stages,
            initial_data  = initial_data,
            pipeline_id   = pipeline_id,
            skip_approved = True,
        )

    # ---------------------------------------------------------------------------
    # Private
    # ---------------------------------------------------------------------------

    def _execute(
        self,
        stages:        list[Stage],
        initial_data:  Union[str, list[dict[str, Any]]],
        pipeline_id:   str,
        skip_approved: bool,
    ) -> list[RunResponse]:
        """Shared execution loop for run() and resume().

        Acquires a PipelineLock for the duration of the loop when use_lock=True
        and the log path is set.  The lock prevents concurrent execution of the
        same pipeline_id on the same machine.
        """
        if self._use_lock:
            lock: Any = PipelineLock(self._log, pipeline_id)
        else:
            lock = contextlib.nullcontext()

        with lock:
            return self._execute_locked(stages, initial_data, pipeline_id, skip_approved)

    def _execute_locked(
        self,
        stages:        list[Stage],
        initial_data:  Union[str, list[dict[str, Any]]],
        pipeline_id:   str,
        skip_approved: bool,
    ) -> list[RunResponse]:
        """Inner execution loop — called inside the pipeline lock."""
        responses: list[RunResponse] = []
        data: Any = initial_data

        for i, stage in enumerate(stages):
            _logger.info(
                "pipeline: stage %d/%d — pipeline=%s stage=%s",
                i + 1, len(stages), pipeline_id, stage.stage_id,
            )

            if skip_approved:
                existing = load_latest_record(self._log, pipeline_id, stage.stage_id)
                if existing is not None:
                    if existing.status == STATUS_APPROVED:
                        _logger.info(
                            "pipeline.resume: skipping approved stage %s/%s",
                            pipeline_id, stage.stage_id,
                        )
                        skipped = RunResponse(
                            run_id          = existing.run_id,
                            status          = existing.status,
                            parsed_response = existing.parsed_response,
                            record          = existing,
                        )
                        responses.append(skipped)
                        data = _prepare_next_input(existing.parsed_response, stage.transform)
                        continue

                    if existing.status == STATUS_PENDING_APPROVAL:
                        _logger.info(
                            "pipeline.resume: stage %s/%s still pending approval — stopping",
                            pipeline_id, stage.stage_id,
                        )
                        responses.append(RunResponse(
                            run_id          = existing.run_id,
                            status          = STATUS_PENDING_APPROVAL,
                            parsed_response = existing.parsed_response,
                            record          = existing,
                        ))
                        return responses

            if self._hooks.pre_stage:
                self._hooks.pre_stage(stage.stage_id, data)

            # requires_approval is on RunRequest — the runner stores the record
            # as pending_approval directly.  The pipeline never patches the record.
            request = RunRequest(
                pipeline_id       = pipeline_id,
                stage_id          = stage.stage_id,
                contract_path     = stage.contract_path,
                input_data        = data,
                provider          = self._provider,
                model             = self._model,
                api_key           = self._api_key,
                output_schema     = stage.output_schema,
                schema_version    = stage.schema_version,
                max_tokens        = stage.max_tokens,
                max_rows          = stage.max_rows,
                retry_policy      = stage.retry_policy,
                classification    = stage.classification,
                requires_approval = stage.requires_approval,
                log               = self._log,
            )

            response = run(
                request,
                redaction_filter = self._redaction_filter,
                artifact_store   = self._artifact_store,
            )

            if response.status == STATUS_PENDING_APPROVAL:
                _logger.info(
                    "pipeline: stage %s/%s stored as pending_approval — halting",
                    pipeline_id, stage.stage_id,
                )
                if self._hooks.on_approval_required:
                    self._hooks.on_approval_required(stage.stage_id, response)
                responses.append(response)
                return responses

            if self._hooks.post_stage:
                self._hooks.post_stage(stage.stage_id, response)

            responses.append(response)

            if i + 1 < len(stages):
                data = _prepare_next_input(response.parsed_response, stages[i + 1].transform)

        _logger.info(
            "pipeline: completed %d stage(s), log → %s",
            len(responses), self._log,
        )
        return responses


def _prepare_next_input(
    parsed:    Optional[dict[str, Any]],
    transform: Optional[Callable[[dict], Any]],
) -> Any:
    """Format one stage's output as input for the next stage."""
    if transform is not None and parsed is not None:
        return transform(parsed)
    return json.dumps(parsed or {}, indent=2, default=str)
