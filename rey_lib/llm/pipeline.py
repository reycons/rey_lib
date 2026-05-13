"""
Multi-stage LLM workflow pipeline.

A Pipeline is an ordered list of Stage definitions.  Each stage runs
via runner.run() and produces a RunResponse.  Stages that require approval
pause the pipeline by returning a RunResponse with status='pending_approval'
rather than raising an exception — the caller checks the status and arranges
for human review before calling resume().

Approval workflow
-----------------
1. Stage sets requires_approval=True.
2. pipeline.run() executes the stage successfully.
3. Return RunResponse with status='pending_approval' — pipeline stops here.
4. Human calls records.approve(record, reviewer) → (updated_record, approval).
5. Human calls records.store_record(updated, log) and records.store_approval(approval, path).
6. pipeline.resume() reads the log, sees status='approved', continues.

Public API
----------
Stage
    Definition of one pipeline stage.
Pipeline
    Ordered collection of stages with a shared log.
Pipeline.run(initial_data, pipeline_id)
    Execute all stages in sequence, returning all RunResponses.
Pipeline.resume(initial_data, pipeline_id)
    Re-execute, skipping stages whose latest record is 'approved'.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rey_lib.llm.api import RunRequest, RunResponse
from rey_lib.llm.records import (
    STATUS_APPROVED,
    STATUS_PENDING_APPROVAL,
    load_latest_record,
)
from rey_lib.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from rey_lib.llm.runner import run

__all__ = ["Stage", "Pipeline"]

_logger = logging.getLogger(__name__)


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
        When True, the pipeline pauses after this stage with
        status='pending_approval' until a human approves the result.
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
    schema_version:    str                              = ""
    max_tokens:        int                              = 4000
    max_rows:          int                              = 200
    retry_policy:      RetryPolicy                      = field(
        default_factory=lambda: DEFAULT_RETRY_POLICY
    )
    classification:    str                              = ""


class Pipeline:
    """Execute an ordered sequence of LLM workflow stages.

    Parameters
    ----------
    stages : list[Stage]
        Ordered stage definitions.
    log : Path
        JSONL file where all ExecutionRecords are appended.
    provider : str
        Provider override for all stages.
    model : str
        Model override for all stages.
    api_key : str
        API key override for all stages.
    """

    def __init__(
        self,
        stages:   list[Stage],
        log:      Path,
        provider: str = "",
        model:    str = "",
        api_key:  str = "",
    ) -> None:
        """Initialise the pipeline."""
        if not stages:
            raise ValueError("Pipeline requires at least one stage.")
        self._stages   = stages
        self._log      = Path(log)
        self._provider = provider
        self._model    = model
        self._api_key  = api_key

    def run(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
        pipeline_id:  str,
    ) -> list[RunResponse]:
        """Execute all stages in sequence.

        Stops and returns early if a stage sets requires_approval=True after
        succeeding — the last RunResponse in the list will have
        status='pending_approval'.  Call resume() after approval to continue.

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
            stages       = self._stages,
            initial_data = initial_data,
            pipeline_id  = pipeline_id,
            skip_approved = False,
        )

    def resume(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
        pipeline_id:  str,
    ) -> list[RunResponse]:
        """Re-execute the pipeline, skipping stages already approved in the log.

        Reads the JSONL log and skips any stage whose latest ExecutionRecord
        for this pipeline_id + stage_id has status='approved'.

        Parameters
        ----------
        initial_data : str | list[dict]
            Starting input (same as run()).
        pipeline_id : str
            Must match the pipeline_id used in the original run() call.

        Returns
        -------
        list[RunResponse]
            Results for each stage.
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
        """Shared execution loop for run() and resume()."""
        responses: list[RunResponse] = []
        data: Any = initial_data

        for i, stage in enumerate(stages):
            _logger.info(
                "pipeline: stage %d/%d — pipeline=%s stage=%s",
                i + 1, len(stages), pipeline_id, stage.stage_id,
            )

            # Skip approved stages when resuming.
            if skip_approved:
                existing = load_latest_record(self._log, pipeline_id, stage.stage_id)
                if existing and existing.status == STATUS_APPROVED:
                    _logger.info(
                        "pipeline.resume: skipping approved stage %s/%s",
                        pipeline_id, stage.stage_id,
                    )
                    responses.append(RunResponse(
                        run_id          = existing.run_id,
                        status          = existing.status,
                        parsed_response = existing.parsed_response,
                        record          = existing,
                    ))
                    data = _prepare_next_input(existing.parsed_response, stage.transform)
                    continue

            # Check approval gate before running this stage.
            if stage.requires_approval and responses:
                prev = responses[-1]
                if prev.status != STATUS_APPROVED:
                    _logger.info(
                        "pipeline: stage %s requires approval of '%s' — pausing",
                        stage.stage_id,
                        stages[i - 1].stage_id,
                    )
                    responses.append(RunResponse(
                        run_id = "",
                        status = STATUS_PENDING_APPROVAL,
                        errors = [
                            f"Stage '{stage.stage_id}' requires approval of "
                            f"'{stages[i - 1].stage_id}' "
                            f"(run_id={prev.run_id}, status='{prev.status}')."
                        ],
                    ))
                    return responses

            request = RunRequest(
                pipeline_id   = pipeline_id,
                stage_id      = stage.stage_id,
                contract_path = stage.contract_path,
                input_data    = data,
                provider      = self._provider,
                model         = self._model,
                api_key       = self._api_key,
                output_schema = stage.output_schema,
                schema_version = stage.schema_version,
                max_tokens    = stage.max_tokens,
                max_rows      = stage.max_rows,
                retry_policy  = stage.retry_policy,
                classification = stage.classification,
                log           = self._log,
            )

            response = run(request)
            responses.append(response)

            # Prepare input for the next stage if there is one.
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
