"""Build and append a configured LLM package to a completed run log, and run the
configured log analysis over that package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rey_lib.logs.evidence_projection import _run_log_identity, read_run_log_sections

__all__ = [
    "create_llm_package",
    "load_contract_references",
    "run_configured_log_analysis",
    "run_configured_record_analysis",
    "run_uncontracted_record_analysis",
    "run_workbench_input_stream",
]


def create_llm_package(
    log_path: str | Path,
    analysis_name: str,
    source_record_type: str,
    package_record_type: str,
) -> dict[str, Any]:
    """Append a configured analysis contract and a source record as a package record.

    Pairs the parsed analysis contract (``instructions``) with the newest
    ``source_record_type`` record (generic ``source`` field) and appends it as
    ``package_record_type``. The same function serves every analysis stage.
    """
    # Imports stay local because config/files import the public logs facade.
    from rey_lib.config.config_utils import build_ctx_from_path
    from rey_lib.logs.record_enrichment import log_run_record

    path = Path(log_path).expanduser().resolve()
    run = read_run_log_sections(path)
    records = run["records"]

    config_record = next((
        record for record in records
        if str(record.get("record_type") or "").upper() == "CONFIG_FILE_REFERENCE"
        and record.get("load_order") == 0
        and str(record.get("configuration_layer") or record.get("config_type") or "").lower()
        == "installation"
    ), None)
    if config_record is None:
        raise ValueError(
            "Execution log has no load-order-zero installation CONFIG_FILE_REFERENCE"
        )

    ctx = build_ctx_from_path(Path(config_record["path"]), full_installation=True)
    analyses = getattr(ctx, "log_analysis", None)
    analysis = analyses.get(analysis_name) if analyses is not None else None
    if analysis is None:
        raise ValueError(f"log_analysis configuration not found: {analysis_name}")

    instructions = _analysis_instructions(analysis)

    source_record = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == source_record_type.upper()
    ), None)
    if source_record is None:
        raise ValueError(
            f"Execution log does not contain source record: {source_record_type}"
        )

    package = _build_analysis_package(
        ctx, analysis_name, source_record_type, instructions, source_record
    )
    if any(
        str(record.get("record_type") or "").upper() == package_record_type.upper()
        and record.get("analysis_name") == analysis_name
        and record.get("source_record_type") == source_record_type
        and record.get("instructions") == instructions
        and record.get("source") == source_record
        for record in records
    ):
        return package

    identity = _run_log_identity(path, records, run["sections"])
    ctx.run_log_path = str(path)
    ctx.run_id = identity["run_id"]
    ctx.run_timestamp = identity["run_timestamp"]
    if identity["app"]:
        ctx.owner_app_name = identity["app"]
    if identity["pipeline"]:
        ctx.pipeline_name = identity["pipeline"]
    if identity["workflow"]:
        ctx.workflow_name = identity["workflow"]

    log_run_record(ctx, package_record_type, record_group="results", **package)
    return package


def _analysis_instructions(analysis: Any) -> Any:
    """Return the parsed contract configured for one analysis.

    The contract is configuration, so it is read through the same file and YAML
    helpers as the rest of the configuration tree.
    """
    from rey_lib.config.config_utils import parse_yaml
    from rey_lib.files import read_text_file

    contract_path = Path(str(analysis.contract))
    if not contract_path.is_file():
        raise FileNotFoundError(
            f"Configured log_analysis contract not found: {contract_path}"
        )
    return parse_yaml(read_text_file(contract_path))


def _resolve_reference_path(ctx: Any, raw_path: str) -> Path:
    """Resolve a contract-declared reference path through the existing resolver.

    Reuses ctx.paths (the installation PathResolver): each ``{name}`` token is
    replaced with its resolved path. No new resolver or path system is created.
    """
    import re

    resolver = getattr(ctx, "paths", None)

    def _sub(match: "re.Match[str]") -> str:
        if resolver is not None:
            try:
                return str(resolver.resolve(match.group(1)))
            except Exception:
                return match.group(0)
        return match.group(0)

    return Path(re.sub(r"\{([^}]+)\}", _sub, str(raw_path))).expanduser()


def load_contract_references(ctx: Any, declared: Any) -> list[dict[str, Any]] | None:
    """Resolve and load the reference documents a contract declares.

    ``declared`` is a contract's ``references`` list (or None/empty). Reuses the
    existing path-token resolver (ctx.paths) and the approved text loader
    (rey_lib.files.read_text_file), so every LLM package-construction path attaches
    the same reference contents. Returns None when nothing is declared. A required
    reference that cannot be resolved or loaded raises before the LLM request; a
    non-required one is omitted with a warning.
    """
    from rey_lib.files import read_text_file
    from rey_lib.logs.log_utils import get_logger

    if not declared:
        return None

    loaded: list[dict[str, Any]] = []
    for ref in declared:
        if not isinstance(ref, dict):
            continue
        name = str(ref.get("name") or "")
        role = str(ref.get("role") or "")
        raw_path = str(ref.get("path") or "")
        required = bool(ref.get("required", True))
        try:
            content = read_text_file(_resolve_reference_path(ctx, raw_path))
        except Exception as exc:
            if required:
                raise ValueError(
                    f"Required contract reference '{name}' could not be loaded "
                    f"from '{raw_path}': {exc}"
                ) from exc
            get_logger(__name__).warning(
                "Skipping optional contract reference '%s' (%s): %s", name, raw_path, exc
            )
            continue
        entry: dict[str, Any] = {"name": name}
        if role:
            entry["role"] = role
        entry["content"] = content
        loaded.append(entry)
    return loaded


def _build_analysis_package(
    ctx: Any,
    analysis_name: str,
    source_record_type: str,
    instructions: Any,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Return the log-analysis LLM_PACKAGE, pairing a contract with a source record.

    This is the legacy provider wire package for the log-analysis path, not the
    canonical LLM package (rey_lib/llm/package.py). The same object is written as
    the durable LLM_PACKAGE record and serialized as the provider prompt, and every
    configured contract reads this shape, so its structure and fields are an
    established wire contract preserved unchanged
    (SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence, reconciliation c).
    The canonical package is adopted separately in paths whose fields exist without
    reconstruction (rey_analyzer).
    """
    package: dict[str, Any] = {
        "analysis_name": analysis_name,
        "source_record_type": source_record_type,
        "instructions": instructions,
    }
    declared = instructions.get("references") if isinstance(instructions, dict) else None
    references = load_contract_references(ctx, declared)
    if references:
        package["references"] = references
    package["source"] = source
    return package


def _execute_analysis_package(
    ctx: Any,
    execution_profile: str,
    artifact_type: str,
    package: dict[str, Any],
    max_input_characters: int = 0,
    payload_id: str | None = None,
) -> Any:
    """Send one package to a named execution profile and return the parsed artifact.

    The single execution path for both configured analyses and No Contract runs:
    profile resolution, the envelope instruction, the provider call, envelope
    extraction, and parsing. Callers own record selection, failure recording, and
    output writing.

    Parameters
    ----------
    ctx : Any
        A resolved context carrying ``llm_profiles``.
    execution_profile : str
        Name of the ``llm_profiles`` entry to run the package against. For a
        configured analysis this is ``analysis.llm_execution_profile``; for a No
        Contract run it is the Workbench-selected profile.
    artifact_type : str
        Artifact envelope type. ``analysis.artifact_type`` for a configured
        analysis, ``""`` for a No Contract run.
    package : dict[str, Any]
        The complete, self-contained LLM input.
    max_input_characters : int
        Optional prompt size limit. ``0`` disables the check.
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.llm.envelope import build_envelope_instruction, extract_artifact_envelope
    from rey_lib.llm.exceptions import ConfigurationFailure
    from rey_lib.llm.llm_utils import direct_ask

    prompt = json.dumps(package) + build_envelope_instruction(artifact_type)
    if max_input_characters and len(prompt) > max_input_characters:
        raise ValueError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
    if profile is None:
        raise ConfigurationFailure(
            f"llm_execution_profile not found: {execution_profile}"
        )
    _eval = getattr(ctx, "llm_evaluation", None)
    _payload_log = getattr(_eval, "payload_log_path", None) if _eval else None
    _run_log = getattr(_eval, "run_log_path", None) if _eval else None
    raw = direct_ask(
        prompt,
        model=profile.model,
        provider=profile.provider,
        api_key=getattr(profile, "api_key", ""),
        eval_payload_log_path=Path(_payload_log) if _payload_log else None,
        eval_run_log_path=Path(_run_log) if _run_log else None,
        payload_id=payload_id,
    )
    content, _ = extract_artifact_envelope(raw, artifact_type)
    return json.loads(content)


def run_configured_record_analysis(
    ctx: Any,
    record: dict[str, Any],
    analysis_name: str,
    source_record_type: str = "",
    max_input_characters: int = 0,
) -> dict[str, Any]:
    """Run a configured analysis over one supplied record and return the result.

    The on-demand counterpart to ``run_configured_log_analysis``. That function
    owns the finalization lifecycle: it reads a run log, selects the newest
    record of a type, and writes the result back as a new record. This one is
    given the exact record to analyse and returns the parsed result to its
    caller — no log is read, and nothing is written to any log or file. It runs
    the same configured analysis, contract, and execution profile, so a caller
    gains no analysis behavior of its own.

    The record is packaged as supplied. Callers pass records that the run-log
    projection has already masked; a caller sourcing records from anywhere else
    is responsible for masking them first.

    Parameters
    ----------
    ctx : Any
        A resolved installation context carrying ``log_analysis`` and
        ``llm_profiles``.
    record : dict[str, Any]
        The already-parsed record to analyse, exactly as selected by the caller.
    analysis_name : str
        The configured ``log_analysis`` entry to run (for example
        ``log_interpreter``).
    source_record_type : str
        Optional declared type of the supplied record, recorded in the package
        for the contract's benefit. Defaults to the record's own
        ``record_type`` when present.
    max_input_characters : int
        Optional serialized-package size limit. ``0`` disables the check.

    Returns
    -------
    dict[str, Any]
        ``{"result": parsed_result_or_None, "action": ..., "skipped": [...]}``
        where action is ``"analysed"``, ``"skipped"``, or ``"failed"``.

    Raises
    ------
    ConfigurationFailure
        The analysis or its execution profile is not configured, or the
        configured contract cannot be read.
    ValueError
        The supplied record is not a JSON object, or exceeds the size limit.
    ProviderFailure, ParseFailure
        Raised by the shared execution path; the caller owns presentation.
    """
    from rey_lib.llm.exceptions import ConfigurationFailure

    result: dict[str, Any] = {"result": None, "action": None, "skipped": []}

    if not isinstance(record, dict):
        raise ValueError("Record analysis requires a JSON object record")

    analyses = getattr(ctx, "log_analysis", None)
    analysis = analyses.get(analysis_name) if analyses is not None else None
    if analysis is None:
        raise ConfigurationFailure(f"log_analysis configuration not found: {analysis_name}")
    if not getattr(analysis, "enabled", False):
        result["skipped"].append("disabled")
        result["action"] = "skipped"
        return result

    package = _build_analysis_package(
        ctx,
        analysis_name,
        source_record_type or str(record.get("record_type") or ""),
        _analysis_instructions(analysis),
        record,
    )
    result["result"] = _execute_analysis_package(
        ctx,
        str(analysis.llm_execution_profile),
        str(getattr(analysis, "artifact_type", "")),
        package,
        max_input_characters,
        payload_id=str(record["payload_id"]) if record.get("payload_id") else None,
    )
    result["action"] = "analysed"
    return result


def run_uncontracted_record_analysis(
    ctx: Any,
    record: dict[str, Any],
    execution_profile: str,
    max_input_characters: int = 0,
) -> dict[str, Any]:
    """Run one already-complete package through the LLM with NO contract added.

    The No Contract counterpart to ``run_configured_record_analysis``. No contract
    is resolved or inserted and no package is assembled: the supplied ``record`` is
    itself the complete package, serialized and sent raw through ``direct_ask``
    (no envelope instruction appended, raw response returned) using the
    Workbench-selected ``execution_profile``. Only existing rey_lib functions are
    composed; nothing is written to any log or file.

    Parameters
    ----------
    ctx : Any
        A resolved context carrying ``llm_profiles``.
    record : dict[str, Any]
        The already-complete package, passed through exactly as supplied.
    execution_profile : str
        Name of the ``llm_profiles`` entry to run against.
    max_input_characters : int
        Optional serialized-package size limit. ``0`` disables the check.

    Returns
    -------
    dict[str, Any]
        ``{"result": raw_response_text_or_None, "action": ..., "skipped": [...]}``.
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.llm.exceptions import ConfigurationFailure
    from rey_lib.llm.llm_utils import direct_ask

    result: dict[str, Any] = {"result": None, "action": None, "skipped": []}

    if not isinstance(record, dict):
        raise ValueError("Record analysis requires a JSON object record")
    profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
    if profile is None:
        raise ConfigurationFailure(f"llm_execution_profile not found: {execution_profile}")

    # Raw send: the supplied package is serialized and sent exactly as-is. No
    # envelope instruction is appended and the raw response is returned unparsed
    # (direct_ask with no output_format sends the prompt exactly as supplied).
    prompt = json.dumps(record)
    if max_input_characters and len(prompt) > max_input_characters:
        raise ValueError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    _eval = getattr(ctx, "llm_evaluation", None)
    _payload_log = getattr(_eval, "payload_log_path", None) if _eval else None
    _run_log = getattr(_eval, "run_log_path", None) if _eval else None
    result["result"] = direct_ask(
        prompt,
        model=profile.model,
        provider=profile.provider,
        api_key=getattr(profile, "api_key", ""),
        eval_payload_log_path=Path(_payload_log) if _payload_log else None,
        eval_run_log_path=Path(_run_log) if _run_log else None,
        payload_id=str(record["payload_id"]) if record.get("payload_id") else None,
    )
    result["action"] = "analysed"
    return result


def run_workbench_input_stream(
    ctx: Any,
    profile_name: str,
    instruction_mode: str,
    instruction_value: str,
    input_text: str,
    payload_id: str | None = None,
    on_chunk: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Run one AI Workbench request through the configured LLM execution owner.

    The Workbench supplies only the selected execution profile, the instruction
    mode, an instruction value, and the operator's input text. Provider and
    credential resolution stay internal: the profile is resolved from
    ``ctx.llm_profiles`` (its ``api_key`` already resolved by config) and
    execution goes through ``runner.run``, so recording and evaluation logging are
    unchanged. When ``on_chunk`` is supplied and the provider supports streaming,
    each response delta is delivered to it as it arrives.

    Parameters
    ----------
    ctx : Any
        A resolved context carrying ``llm_profiles`` (and ``log_analysis`` for
        the contract mode).
    profile_name : str
        Name of the selected ``llm_profiles`` entry.
    instruction_mode : str
        ``'contract'``, ``'none'``, or ``'text_prompt'``.
    instruction_value : str
        For ``'contract'`` the configured-contract analysis name; for
        ``'text_prompt'`` the free-form instructions; ignored for ``'none'``.
    input_text : str
        The operator's left-pane input, sent unchanged.
    payload_id : Optional[str]
        Existing evaluation-payload identity to preserve for a rerun.
    on_chunk : Optional[Callable[[str], None]]
        Optional incremental-output callback forwarded to the provider.
    cancelled : Optional[Callable[[], bool]]
        Optional cooperative cancellation check forwarded to the LLM runner.

    Returns
    -------
    RunResponse
        The runner response (its ``raw_text`` / ``parsed_response`` hold the
        complete result for providers that do not stream).
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.llm.api import RunRequest
    from rey_lib.llm.envelope import build_envelope_instruction
    from rey_lib.llm.exceptions import ConfigurationFailure
    from rey_lib.llm.runner import run as _run

    profile = find_in_ctx(ctx, "llm_profiles", profile_name)
    if profile is None:
        raise ConfigurationFailure(f"llm_execution_profile not found: {profile_name}")

    _eval = getattr(ctx, "llm_evaluation", None)
    payload_log = getattr(_eval, "payload_log_path", None) if _eval else None
    run_log = getattr(_eval, "run_log_path", None) if _eval else None

    common: dict[str, Any] = {
        "pipeline_id": "ai_workbench",
        "stage_id": "run",
        "provider": str(getattr(profile, "provider", "") or ""),
        "model": str(getattr(profile, "model", "") or ""),
        "api_key": str(getattr(profile, "api_key", "") or ""),
        "eval_payload_log_path": Path(payload_log) if payload_log else None,
        "eval_run_log_path": Path(run_log) if run_log else None,
        "payload_id": payload_id or None,
    }

    if instruction_mode == "contract":
        analyses = getattr(ctx, "log_analysis", None)
        entry = (
            analyses.get(instruction_value)
            if analyses is not None and hasattr(analyses, "get") else None
        )
        if entry is None or not str(getattr(entry, "contract", "") or ""):
            raise ConfigurationFailure(
                f"No contract configured for '{instruction_value}'."
            )
        # Match configured AI Analysis exactly: read the configured YAML through
        # _analysis_instructions(), place that parsed contract in the established
        # analysis package, include its configured references, and send the whole
        # package through the inline/direct execution behavior. These analysis
        # contracts intentionally use Rey's `contract: {name, ...}` document
        # convention; they are not low-level rey_lib.llm Contract files and must
        # never be passed to runner.load_contract().
        try:
            source: Any = json.loads(input_text)
        except (TypeError, json.JSONDecodeError):
            source = input_text
        source_record_type = (
            str(source.get("record_type") or "") if isinstance(source, dict) else ""
        )
        package = _build_analysis_package(
            ctx,
            instruction_value,
            source_record_type,
            _analysis_instructions(entry),
            source,
        )
        prompt = json.dumps(package) + build_envelope_instruction(
            str(getattr(entry, "artifact_type", "") or "")
        )
        request = RunRequest(
            contract_path=Path("<ai_workbench>"),
            contract_text=prompt,
            input_data=prompt,
            raw_output=True,
            **common,
        )
    elif instruction_mode == "text_prompt":
        # The free-form instructions become the contract body; the input is the
        # user turn and is sent unchanged.
        request = RunRequest(
            contract_path=Path("<ai_workbench>"),
            contract_text=str(instruction_value or ""),
            input_data=input_text,
            raw_output=True,
            **common,
        )
    else:
        # None: send the input exactly as entered, with no separate instructions.
        request = RunRequest(
            contract_path=Path("<ai_workbench>"),
            contract_text=input_text,
            input_data=input_text,
            raw_output=True,
            **common,
        )

    return _run(request, on_chunk=on_chunk, cancelled=cancelled)


def run_configured_log_analysis(
    log_path: str | Path,
    analysis_name: str,
    package_record_type: str,
) -> dict[str, Any]:
    """Run the configured LLM analysis over the existing package record.

    Sends the complete ``package_record_type`` record unchanged to the configured LLM
    through ``direct_ask``, extracts and validates the configured artifact from the
    standard rey_lib envelope, and writes the parsed structured result through the
    configured writer. The embedded contract is never reloaded — only existing
    rey_lib functions are composed.
    """
    import json

    from rey_lib.config.config_utils import build_ctx_from_path
    from rey_lib.errors.error_utils import build_safe_error_payload
    from rey_lib.files import write_file
    from rey_lib.llm.exceptions import ConfigurationFailure, ParseFailure, ProviderFailure
    from rey_lib.logs.record_enrichment import log_run_record

    result: dict[str, Any] = {"result": None, "action": None, "skipped": [], "failures": []}

    path = Path(log_path).expanduser().resolve()
    run = read_run_log_sections(path)
    records = run["records"]

    # Newest existing package record — the complete, self-contained LLM input.
    package = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == package_record_type.upper()
    ), None)
    if package is None:
        raise ValueError(
            f"Execution log does not contain package record: {package_record_type}"
        )

    config_record = next((
        record for record in records
        if str(record.get("record_type") or "").upper() == "CONFIG_FILE_REFERENCE"
        and record.get("load_order") == 0
        and str(record.get("configuration_layer") or record.get("config_type") or "").lower()
        == "installation"
    ), None)
    if config_record is None:
        raise ValueError(
            "Execution log has no load-order-zero installation CONFIG_FILE_REFERENCE"
        )

    ctx = build_ctx_from_path(Path(config_record["path"]), full_installation=True)
    analyses = getattr(ctx, "log_analysis", None)
    analysis = analyses.get(analysis_name) if analyses is not None else None
    if analysis is None:
        raise ValueError(f"log_analysis configuration not found: {analysis_name}")

    if not getattr(analysis, "enabled", False):
        result["skipped"].append("disabled")
        return result

    # Stamp run identity (run metadata, not configuration) before the failure boundary
    # so any failure below — including malformed configuration — is recorded against
    # this run.
    identity = _run_log_identity(path, records, run["sections"])
    ctx.run_log_path = str(path)
    ctx.run_id = identity["run_id"]
    ctx.run_timestamp = identity["run_timestamp"]
    if identity["app"]:
        ctx.owner_app_name = identity["app"]
    if identity["pipeline"]:
        ctx.pipeline_name = identity["pipeline"]
    if identity["workflow"]:
        ctx.workflow_name = identity["workflow"]

    # Safe record identity for the failure record, resolved without dereferencing a
    # possibly malformed output block. When the configured output type cannot be read,
    # the failure is still recorded (never silent) rather than repaired or inferred.
    output = getattr(analysis, "output", None)
    failure_record_type = str(getattr(output, "record_type", "") or "LLM_ANALYSIS_FAILURE")
    failure_record_group = str(getattr(output, "record_group", "") or "results")

    # Every configuration access and validation for this stage lives inside the failure
    # boundary: reading record_type, record_group, destination, format,
    # the idempotency probe, profile resolution, execution, and parsing.
    try:
        record_type = str(output.record_type)
        record_group = str(output.record_group)
        destination = str(getattr(output, "destination", "stdout")).lower()
        output_format = str(getattr(output, "format", ""))
        output_path = getattr(output, "path", None)

        # Idempotency: a prior configured result must not be duplicated on re-run.
        if destination == "file":
            if output_path is not None and Path(str(output_path)).expanduser().exists():
                result["action"] = "existing"
                return result
        elif any(
            str(record.get("record_type") or "").upper() == record_type.upper()
            for record in records
        ):
            result["action"] = "existing"
            return result

        parsed_result = _execute_analysis_package(
            ctx,
            str(analysis.llm_execution_profile),
            str(getattr(analysis, "artifact_type", "")),
            package,
        )
    except (
        ProviderFailure, ParseFailure, ConfigurationFailure, json.JSONDecodeError,
        AttributeError, KeyError, TypeError,
    ) as exc:
        # Canonical failure record for any failure in this stage — LLM failure or
        # malformed configuration. Shaped by error_utils so the full sanitized scope
        # (type, message, exception, traceback) is captured, keyed by the configured
        # analysis name and stamped run metadata. fail_on_error then decides whether to
        # re-raise or return nonfatally.
        log_run_record(
            ctx, failure_record_type, record_group=failure_record_group,
            analysis_name=analysis_name, **build_safe_error_payload(exc),
        )
        result["failures"].append(str(exc))
        result["action"] = "failed"
        if getattr(analysis, "fail_on_error", False):
            raise
        return result

    if destination == "file":
        write_file(Path(str(output_path)), parsed_result, file_type=output_format)
        result["action"] = "written_file"
    else:
        log_run_record(ctx, record_type, record_group=record_group, **parsed_result)
        result["action"] = "written_stdout"

    result["result"] = parsed_result
    return result
