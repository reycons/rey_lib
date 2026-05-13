"""
Multi-stage LLM workflow pipeline.

A Pipeline is an ordered list of Stage definitions.  Each stage evaluates
data against a contract and produces an EvaluationResult.  Stages can
require human approval before the next stage runs, enforcing review gates
that the contract system guarantees in writing.

The output of one stage feeds the next via an optional ``transform``
callable.  When no transform is provided the full result dict from the
previous stage is serialised as text and used as the next stage's input.

All results from every stage are appended to a single JSONL log so the
full execution history is preserved in one place.

Public API
----------
Stage
    Dataclass defining one pipeline stage.
Pipeline
    Ordered collection of stages with a shared log.
Pipeline.run(initial_data)
    Execute all stages in sequence and return all EvaluationResults.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rey_lib.errors.error_utils import ConfigError
from rey_lib.llm import result as result_module
from rey_lib.llm.result import EvaluationResult
from rey_lib.llm.runner import run

__all__ = ["Stage", "Pipeline"]

_logger = logging.getLogger(__name__)


@dataclass
class Stage:
    """Definition of one pipeline stage.

    Attributes
    ----------
    use_case : str
        Logical use case name (stored in the EvaluationResult envelope).
    stage : str
        Stage name (e.g. 'criteria_extraction', 'sql_generation').
    contract_path : Path
        Path to the versioned contract markdown file.
    requires_approval : bool
        When True, the pipeline checks that the previous stage result has
        review_status == 'approved' before executing this stage.  Raises
        PipelineGateError if the check fails.
    transform : Optional[Callable[[dict], Any]]
        Called with the previous stage's result dict.  The return value is
        passed as ``data`` to runner.run() for this stage.  When omitted,
        the full previous result dict is JSON-serialised and used as input.
    output_schema : Optional[dict]
        JSON Schema for validating this stage's LLM output.  When omitted,
        the runner looks for a sidecar .schema.json file.
    max_tokens : int
        Maximum LLM response tokens for this stage.
    max_rows : int
        Maximum rows when the stage input is a list of dicts.
    """

    use_case:          str
    stage:             str
    contract_path:     Path
    requires_approval: bool                             = False
    transform:         Optional[Callable[[dict], Any]] = None
    output_schema:     Optional[dict[str, Any]]        = None
    max_tokens:        int                              = 4000
    max_rows:          int                              = 200


class PipelineGateError(Exception):
    """Raised when a stage requires approval but the previous result is not approved."""


class Pipeline:
    """Execute an ordered sequence of LLM workflow stages.

    Parameters
    ----------
    stages : list[Stage]
        Ordered stage definitions.
    log : Path
        JSONL file where all stage results are appended.
    provider : Optional[str]
        LLM provider override for all stages.
    model : Optional[str]
        Model override for all stages.
    api_key : Optional[str]
        API key override for all stages.
    """

    def __init__(
        self,
        stages:   list[Stage],
        log:      Path,
        provider: Optional[str] = None,
        model:    Optional[str] = None,
        api_key:  Optional[str] = None,
    ) -> None:
        """Initialise the pipeline."""
        if not stages:
            raise ConfigError("Pipeline requires at least one stage.")
        self._stages   = stages
        self._log      = Path(log)
        self._provider = provider
        self._model    = model
        self._api_key  = api_key

    def run(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
    ) -> list[EvaluationResult]:
        """Execute all stages in sequence.

        The first stage receives ``initial_data``.  Each subsequent stage
        receives either the transformed output of the previous stage (if
        ``transform`` is set) or the full previous result dict as JSON text.

        Parameters
        ----------
        initial_data : str | list[dict]
            Starting input — typically a SQL result set or plain text.

        Returns
        -------
        list[EvaluationResult]
            One result per stage in execution order.

        Raises
        ------
        PipelineGateError
            If a stage has requires_approval=True and the preceding result
            has not been approved.
        """
        results: list[EvaluationResult] = []
        data: Any = initial_data

        for i, stage in enumerate(self._stages):
            _logger.info(
                "pipeline: running stage %d/%d — %s/%s",
                i + 1, len(self._stages), stage.use_case, stage.stage,
            )

            if stage.requires_approval and results:
                prev = results[-1]
                if prev.review_status != "approved":
                    raise PipelineGateError(
                        f"Stage '{stage.stage}' requires approval of "
                        f"'{prev.stage}' (evaluation_id={prev.evaluation_id}, "
                        f"status='{prev.review_status}'). "
                        "Approve the result with result_module.approve() and "
                        "store it before re-running the pipeline."
                    )

            evaluation = run(
                data          = data,
                contract_path = stage.contract_path,
                use_case      = stage.use_case,
                stage         = stage.stage,
                max_tokens    = stage.max_tokens,
                max_rows      = stage.max_rows,
                provider      = self._provider,
                model         = self._model,
                api_key       = self._api_key,
                output_schema = stage.output_schema,
                log           = self._log,
            )

            results.append(evaluation)

            if i + 1 < len(self._stages):
                next_stage = self._stages[i + 1]
                data = _prepare_next_input(evaluation, next_stage.transform)

        _logger.info(
            "pipeline: completed %d stage(s), log → %s",
            len(results), self._log,
        )
        return results

    def resume(
        self,
        initial_data: Union[str, list[dict[str, Any]]],
    ) -> list[EvaluationResult]:
        """Re-run the pipeline, skipping stages that already have an approved result.

        Reads the JSONL log and skips any stage whose latest result for the
        matching use_case+stage has review_status='approved'.  Useful after a
        human has reviewed and approved intermediate results.

        Parameters
        ----------
        initial_data : str | list[dict]
            Starting input (same as run()).

        Returns
        -------
        list[EvaluationResult]
            Results for each stage — loaded from log for approved stages,
            freshly evaluated for pending/rejected stages.
        """
        results: list[EvaluationResult] = []
        data: Any = initial_data

        for i, stage in enumerate(self._stages):
            existing = result_module.load_latest(
                self._log, stage.use_case, stage.stage
            )

            if existing and existing.review_status == "approved":
                _logger.info(
                    "pipeline.resume: skipping approved stage %d/%d — %s/%s",
                    i + 1, len(self._stages), stage.use_case, stage.stage,
                )
                results.append(existing)
                data = _prepare_next_input(existing, stage.transform)
                continue

            if stage.requires_approval and results:
                prev = results[-1]
                if prev.review_status != "approved":
                    raise PipelineGateError(
                        f"Stage '{stage.stage}' requires approval of "
                        f"'{prev.stage}' (status='{prev.review_status}')."
                    )

            evaluation = run(
                data          = data,
                contract_path = stage.contract_path,
                use_case      = stage.use_case,
                stage         = stage.stage,
                max_tokens    = stage.max_tokens,
                max_rows      = stage.max_rows,
                provider      = self._provider,
                model         = self._model,
                api_key       = self._api_key,
                output_schema = stage.output_schema,
                log           = self._log,
            )
            results.append(evaluation)

            if i + 1 < len(self._stages):
                next_stage = self._stages[i + 1]
                data = _prepare_next_input(evaluation, next_stage.transform)

        return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _prepare_next_input(
    evaluation: EvaluationResult,
    transform:  Optional[Callable[[dict], Any]],
) -> Any:
    """Extract or format the output of one stage as input for the next."""
    if transform is not None:
        return transform(evaluation.result)
    return json.dumps(evaluation.result, indent=2, default=str)
