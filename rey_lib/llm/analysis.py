"""
First-class analysis domain types and the Analyzer entry point.

The Analyzer is the primary public interface for contract-driven data
analysis.  Pipeline, Stage, RunRequest, and runner are internal machinery
that Analyzer composes — callers never need to import them directly.

Domain model
------------
AnalysisContractSpec
    Parsed domain-specific fields from the analysis contract frontmatter.
    Declares what data the contract expects, how to prepare it, and what
    output schema to validate against.
AnalysisContract
    A fully loaded contract — both base fields (name, version, hash, body)
    and domain spec in one object.
AnalysisResult
    The result of one analysis run: structured data, preparation metadata,
    status, and a full audit record.
Analyzer
    Orchestrates the full lifecycle: load → extract → prepare → run LLM →
    validate → return AnalysisResult.

Analysis contract frontmatter
------------------------------
Beyond the three required base fields (name, version, effective_date),
analysis contracts support the following optional domain fields::

    source_type: sql | csv | excel | text | any
    allowed_columns:
      - col_a
      - col_b
    required_filters:
      - column: status
        operator: "=="
        value: "active"
    max_rows: 500
    sampling:
      method: random      # head | tail | random
      seed: 42
    redaction:
      - column: customer_id
        mask: "[CUSTOMER]"
    output_schema:
      type: object
      properties:
        total: {type: number}

All fields default to permissive values when omitted (all columns allowed,
no filters, head sampling, 200 rows, no redaction).

Public API
----------
AnalysisContractSpec
AnalysisContract
AnalysisResult
Analyzer
load_analysis_contract(path)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from rey_lib.llm.api import RunRequest, RunResponse
from rey_lib.llm.artifacts import ArtifactStore
from rey_lib.llm.contract import Contract, load as _load_contract
from rey_lib.llm.datasource import DataSource
from rey_lib.llm.exceptions import ConfigurationFailure
from rey_lib.llm.preparation import PreparedInput, prepare
from rey_lib.llm.records import ExecutionRecord
from rey_lib.llm.redaction import RedactionFilter
from rey_lib.llm.runner import run
from rey_lib.logs.log_utils import get_logger

__all__ = [
    "AnalysisContractSpec",
    "AnalysisContract",
    "AnalysisResult",
    "Analyzer",
    "load_analysis_contract",
]

_logger = get_logger(__name__)

# Permitted source type values.
_VALID_SOURCE_TYPES = frozenset({"sql", "csv", "excel", "text", "any"})

# Permitted sampling methods.
_VALID_SAMPLING_METHODS = frozenset({"head", "tail", "random"})


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalysisContractSpec:
    """Domain-specific fields parsed from an analysis contract's frontmatter.

    Attributes
    ----------
    source_type : str
        Expected data origin: ``sql``, ``csv``, ``excel``, ``text``, or ``any``.
    allowed_columns : list[str]
        Columns the LLM is permitted to see.  Empty list = all columns allowed.
    required_filters : list[dict[str, Any]]
        Row-level predicates applied before sampling.  Each dict requires
        ``column``, ``operator``, and ``value`` keys.
    max_rows : int
        Maximum rows sent to the LLM after sampling.
    sampling_method : str
        Row selection strategy: ``head``, ``tail``, or ``random``.
    sampling_seed : Optional[int]
        Random seed for ``random`` sampling.  None = non-deterministic.
    redaction : list[dict[str, str]]
        Column-level masking rules.  Each dict has ``column`` and ``mask``.
    output_schema : Optional[dict[str, Any]]
        JSON Schema dict for LLM output validation.  None = no validation
        (runner falls back to a sidecar ``.schema.json`` if present).
    """

    source_type:      str                      = "any"
    allowed_columns:  list[str]                = field(default_factory=list)
    required_filters: list[dict[str, Any]]     = field(default_factory=list)
    max_rows:         int                      = 200
    sampling_method:  str                      = "head"
    sampling_seed:    Optional[int]            = None
    redaction:        list[dict[str, str]]     = field(default_factory=list)
    output_schema:    Optional[dict[str, Any]] = None
    output_format:    str                      = "json"
    artifact_type:    str                      = ""


@dataclass(frozen=True)
class AnalysisContract:
    """A fully loaded analysis contract.

    Combines the base ``Contract`` (name, version, hash, path, body) with
    the parsed domain ``AnalysisContractSpec``.

    Attributes
    ----------
    base : Contract
        Loaded base contract with frontmatter and body.
    spec : AnalysisContractSpec
        Parsed domain-specific preparation and validation rules.
    """

    base: Contract
    spec: AnalysisContractSpec

    @property
    def name(self) -> str:
        """Contract name from frontmatter."""
        return self.base.name

    @property
    def version(self) -> str:
        """Contract version from frontmatter."""
        return self.base.version

    @property
    def path(self) -> Path:
        """Absolute path the contract was loaded from."""
        return self.base.path

    @property
    def hash(self) -> str:
        """SHA-256 of the full contract file content."""
        return self.base.hash


@dataclass(frozen=True)
class AnalysisResult:
    """Result of one Analyzer.analyze() call.

    Attributes
    ----------
    run_id : str
        UUID of the underlying ExecutionRecord.
    status : str
        One of the STATUS_* constants from records.py.
    data : Optional[dict[str, Any]]
        Validated structured output from the LLM.  None when status is not
        ``success`` or ``pending_approval``.
    prepared : PreparedInput
        Full preparation metadata — what columns, rows, and text the LLM saw.
    errors : list[str]
        Validation or provider error messages.  Empty on success.
    record : Optional[ExecutionRecord]
        Full execution record for callers that need audit detail.
    """

    run_id:    str
    status:    str
    data:      Optional[dict[str, Any]]
    raw_text:  Optional[str]
    prepared:  PreparedInput
    errors:    list[str]
    record:    Optional[ExecutionRecord]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_analysis_contract(path: Path) -> AnalysisContract:
    """Parse and validate an analysis contract file.

    Reads the base contract fields (name, version, effective_date, body,
    hash) then extracts domain-specific preparation rules from
    ``raw_frontmatter``.  All domain fields are optional — contracts that
    omit them behave like plain runner contracts with permissive defaults.

    Parameters
    ----------
    path : Path
        Path to the contract markdown file.

    Returns
    -------
    AnalysisContract
        Loaded contract with base fields and domain spec.

    Raises
    ------
    ConfigurationFailure
        If the base contract is invalid or domain fields have illegal values.
    """
    from rey_lib.errors.error_utils import ConfigError  # noqa: PLC0415

    try:
        base = _load_contract(Path(path))
    except ConfigError as exc:
        raise ConfigurationFailure(str(exc)) from exc

    fm = base.raw_frontmatter

    source_type = str(fm.get("source_type", "any")).lower()
    if source_type not in _VALID_SOURCE_TYPES:
        raise ConfigurationFailure(
            f"Contract '{base.name}': source_type '{source_type}' is not valid. "
            f"Choose one of: {sorted(_VALID_SOURCE_TYPES)}"
        )

    sampling_cfg    = fm.get("sampling") or {}
    sampling_method = str(sampling_cfg.get("method", "head")).lower()
    if sampling_method not in _VALID_SAMPLING_METHODS:
        raise ConfigurationFailure(
            f"Contract '{base.name}': sampling.method '{sampling_method}' is not valid. "
            f"Choose one of: {sorted(_VALID_SAMPLING_METHODS)}"
        )

    raw_seed    = sampling_cfg.get("seed")
    sampling_seed: Optional[int] = int(raw_seed) if raw_seed is not None else None

    output_cfg = fm.get("output") or {}

    spec = AnalysisContractSpec(
        source_type      = source_type,
        allowed_columns  = list(fm.get("allowed_columns") or []),
        required_filters = list(fm.get("required_filters") or []),
        max_rows         = int(fm.get("max_rows", 200)),
        sampling_method  = sampling_method,
        sampling_seed    = sampling_seed,
        redaction        = list(fm.get("redaction") or []),
        output_schema    = fm.get("output_schema"),
        output_format    = str(
            output_cfg.get("format", fm.get("output_format", "json"))
        ).lower(),
        artifact_type    = str(
            output_cfg.get("artifact_type", fm.get("artifact_type", ""))
        ).lower(),
    )

    return AnalysisContract(base=base, spec=spec)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    """Orchestrate contract-driven data analysis.

    The Analyzer is the main entry point for application code.  It owns the
    full lifecycle: load the contract once, then call analyze() with any
    DataSource.

    Parameters
    ----------
    contract_path : Path
        Path to the analysis contract markdown file.
    provider : str
        Provider name (e.g. 'anthropic', 'openai', 'ollama').
    model : str
        Model identifier (e.g. 'claude-sonnet-4-6').
    api_key : str
        API key for the provider.  Not required for 'ollama'.
    log : Optional[Path]
        JSONL file where execution records are appended.
    artifact_store : Optional[ArtifactStore]
        When provided, the parsed response is written after each successful run.
    redaction_filter : Optional[RedactionFilter]
        Applied to the rendered prompt text before it reaches the provider.
        Complements per-column redaction rules in the contract spec.
    max_extract : int
        Hard upper bound on rows extracted from the DataSource.  The
        preparation pipeline samples down to ``contract.spec.max_rows``.
    requires_approval : bool
        When True, every successful run is stored as ``pending_approval``
        and the caller must approve before the result is considered final.
    """

    def __init__(
        self,
        contract_path:    Path,
        provider:         str                          = "",
        model:            str                          = "",
        api_key:          str                          = "",
        temperature:      float                        = 0.0,
        provider_options: Optional[dict[str, Any]]     = None,
        log:              Optional[Path]               = None,
        artifact_store:   Optional[ArtifactStore]     = None,
        redaction_filter: Optional[RedactionFilter]    = None,
        max_extract:      int                          = 10_000,
        requires_approval: bool                        = False,
        artifact_processing: Optional[dict[str, Any]]  = None,
        eval_payload_log_path: Optional[Path]          = None,
        eval_run_log_path:     Optional[Path]          = None,
        payload_id:            Optional[str]           = None,
    ) -> None:
        """Load the contract and store provider configuration."""
        self._contract         = load_analysis_contract(contract_path)
        self._provider         = provider
        self._model            = model
        self._api_key          = api_key
        self._temperature      = temperature
        self._provider_options = provider_options or {}
        self._log              = Path(log) if log else None
        self._artifact_store   = artifact_store
        self._redaction_filter = redaction_filter
        self._max_extract      = max_extract
        self._requires_approval = requires_approval
        self._artifact_processing = artifact_processing or {}
        # Resolved llm_evaluation.payload_log_path / run_log_path, supplied by the
        # ctx-holding caller exactly as the run-log ``log`` path already is
        # (SGC_Rey_LLM_Evaluation_Append_Only_Log). Evaluation logging is additive.
        self._eval_payload_log_path = Path(eval_payload_log_path) if eval_payload_log_path else None
        self._eval_run_log_path     = Path(eval_run_log_path) if eval_run_log_path else None
        self._payload_id            = payload_id

        _logger.info(
            "analyzer: loaded contract '%s' v%s from %s",
            self._contract.name,
            self._contract.version,
            self._contract.path,
        )

    @property
    def contract(self) -> AnalysisContract:
        """The loaded analysis contract."""
        return self._contract

    def analyze(
        self,
        source:      DataSource,
        analysis_id: str,
    ) -> AnalysisResult:
        """Run the full analysis lifecycle for one data source.

        1. Extract raw data from ``source``.
        2. Apply preparation pipeline (filter, sample, redact, render).
        3. Call the LLM provider with the rendered prompt.
        4. Validate output against the contract's output_schema.
        5. Return an AnalysisResult with data, metadata, and audit record.

        Parameters
        ----------
        source : DataSource
            The data origin to analyse.
        analysis_id : str
            Identifier for this analysis run — stored in every audit record.
            Use a meaningful value such as a batch ID or timestamp slug.

        Returns
        -------
        AnalysisResult
            Structured output, preparation metadata, status, and audit record.
        """
        spec = self._contract.spec

        _logger.info(
            "analyzer: extracting data for analysis_id=%s source_type=%s",
            analysis_id,
            spec.source_type,
        )
        raw = source.extract(max_extract=self._max_extract)

        _logger.info(
            "analyzer: preparing %d rows from '%s'",
            raw.row_count,
            raw.source_ref,
        )
        prepared = prepare(
            raw,
            allowed_columns  = spec.allowed_columns,
            required_filters = spec.required_filters,
            max_rows         = spec.max_rows,
            sampling_method  = spec.sampling_method,
            sampling_seed    = spec.sampling_seed,
            redaction_rules  = spec.redaction,
        )

        _logger.info(
            "analyzer: prepared %d rows (extracted=%d, filtered=%d) — calling LLM",
            prepared.profile.rows_sampled,
            prepared.profile.rows_extracted,
            prepared.profile.rows_after_filter,
        )

        request = RunRequest(
            pipeline_id       = analysis_id,
            stage_id          = self._contract.name,
            contract_path     = self._contract.path,
            input_data        = prepared.rendered_text,
            provider          = self._provider,
            model             = self._model,
            api_key           = self._api_key,
            temperature       = self._temperature,
            provider_options  = self._provider_options,
            output_schema     = spec.output_schema,
            log               = self._log,
            requires_approval = self._requires_approval,
            raw_output        = spec.output_format == "raw",
            artifact_type     = spec.artifact_type,
            artifact_processing = self._artifact_processing,
            eval_payload_log_path = self._eval_payload_log_path,
            eval_run_log_path     = self._eval_run_log_path,
            payload_id            = self._payload_id,
        )

        response: RunResponse = run(
            request,
            redaction_filter = self._redaction_filter,
            artifact_store   = self._artifact_store,
        )

        if response.status == "failed":
            _logger.warning(
                "analyzer: analysis_id=%s failed run_id=%s errors=%s",
                analysis_id,
                response.run_id,
                "; ".join(response.errors) if response.errors else "unknown",
            )
        else:
            _logger.info(
                "analyzer: analysis_id=%s status=%s run_id=%s",
                analysis_id,
                response.status,
                response.run_id,
            )

        return AnalysisResult(
            run_id   = response.run_id,
            status   = response.status,
            data     = response.parsed_response,
            raw_text = response.raw_text,
            prepared = prepared,
            errors   = response.errors,
            record   = response.record,
        )
