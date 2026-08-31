"""Build and append a configured LLM package to a completed run log, and run the
configured log analysis over that package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rey_lib.logs.evidence_projection import read_run_log_sections

__all__ = [
    "create_llm_package",
    "load_contract_references",
    "run_configured_log_analysis",
    "run_configured_record_analysis",
    "run_uncontracted_record_analysis",
    "run_workbench_input_stream",
]


def create_llm_package(
    run_log: Any,
    *,
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

    path = Path(run_log.path()).expanduser().resolve()
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
        and record.get("source") == source_record
        for record in records
    ):
        return package


    run_log.append(package_record_type, record_group="results", **package)
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
    """Return the log-analysis LLM_PACKAGE: the situation an analysis reads.

    This is the legacy provider wire package for the log-analysis path, not the
    canonical LLM package (rey_lib/analysis/package.py). The same object is
    written as the durable LLM_PACKAGE record and serialized as the provider
    prompt. The canonical package is adopted separately in paths whose fields
    exist without reconstruction (rey_analyzer).

    **The contract is no longer embedded here.** It arrives as the instruction
    the task resolves, through the canonical AI settings object, so that every
    task answers the same way about what governs it. Carrying it here as well
    would send it twice and let this path disagree with the setting a reader can
    see.

    That supersedes SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence
    reconciliation (c), which preserved this object unchanged as a legacy wire
    package. The invariant that replaces it: no consumer supplies or suppresses
    AI settings independently. Safe for the contracts themselves -- they describe
    their input by kind, never by the field it arrives in.

    The contract is still *parsed* to resolve the references it declares. What
    changed is where it is sent, not who reads it.
    """
    package: dict[str, Any] = {
        "analysis_name": analysis_name,
        "source_record_type": source_record_type,
    }
    declared = instructions.get("references") if isinstance(instructions, dict) else None
    references = load_contract_references(ctx, declared)
    if references:
        package["references"] = references
    package["source"] = source
    return package


def _execute_analysis_package(
    ctx: Any,
    ai: Any,
    execution_profile: str,
    artifact_type: str,
    package: dict[str, Any],
    max_input_characters: int = 0,
    payload_id: str | None = None,
    task: str = "",
) -> Any:
    """Send one package to an execution profile and return the parsed artifact.

    The single execution path for both configured analyses and No Contract runs:
    profile resolution, the envelope instruction, the provider call, envelope
    extraction, and parsing. Callers own record selection, failure recording, and
    output writing.

    Parameters
    ----------
    ctx : Any
        Resolved configuration, carrying ``llm_profiles``. Configuration only --
        the AI is handed in separately, because a context that resolves an
        installation's settings is not the same thing as that installation's
        running AI.
    ai : Any
        The runtime's one AI, built by bootstrap for the installation currently
        executing. ``None`` when this runtime configures none, which the caller's
        failure boundary records.
    execution_profile : str
        Name of the ``llm_profiles`` entry to run the package against. For a No
        Contract run this is the Workbench-selected profile. Empty when ``task``
        is given, because the runtime's settings then own the choice.
    task : str
        What the AI is being asked to do. Given instead of a profile, the
        runtime's task-aware settings decide which one answers, so this caller
        stops carrying execution policy it never owned.
    artifact_type : str
        Artifact envelope type. ``analysis.artifact_type`` for a configured
        analysis, ``""`` for a No Contract run.
    package : dict[str, Any]
        The complete, self-contained LLM input.
    max_input_characters : int
        Optional prompt size limit. ``0`` disables the check.
    structured : bool
        Whether the caller needs a parsed JSON payload rather than text. Asking
        tells the provider to answer in JSON and puts the runtime's output
        reader in the path, which is what normalizes a fenced or prose-wrapped
        answer. A caller that says nothing still receives text.
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.config.env_reference import resolve_env_reference
    from rey_lib.artifacts import (
        build_envelope_instruction, extract_artifact_envelope, loads_llm_json,
    )
    from rey_lib.ai.errors import AIConfigurationError

    prompt = json.dumps(package) + build_envelope_instruction(artifact_type)
    if max_input_characters and len(prompt) > max_input_characters:
        raise ValueError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    # A named profile is still checked here, because a caller that names one is
    # asserting it exists. A task names none: the settings answer, and the AI
    # refuses a selection it does not offer, so repeating the check would be a
    # second authority on the same question.
    if not task:
        profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
        if profile is None:
            raise AIConfigurationError(
                f"llm_execution_profile not found: {execution_profile}"
            )
    raw = _ask(ai, prompt, execution_profile, payload_id=payload_id, task=task)
    content, _ = extract_artifact_envelope(raw, artifact_type)
    # The shared policy, not the standard library's: loads_llm_json accepts
    # literal control characters inside strings, protects invalid Markdown
    # escapes and completes a truncated document. Parsing extracted artifact
    # content with json.loads bypassed all three and failed on a control
    # character the shared loader is written to accept.
    return loads_llm_json(content)


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
    AIConfigurationError
        The analysis or its execution profile is not configured, or the
        configured contract cannot be read.
    ValueError
        The supplied record is not a JSON object, or exceeds the size limit.
    AIProviderError, ArtifactEnvelopeError
        Raised by the shared execution path; the caller owns presentation.
    """
    from rey_lib.ai.errors import AIConfigurationError

    result: dict[str, Any] = {"result": None, "action": None, "skipped": []}

    if not isinstance(record, dict):
        raise ValueError("Record analysis requires a JSON object record")

    analyses = getattr(ctx, "log_analysis", None)
    analysis = analyses.get(analysis_name) if analyses is not None else None
    if analysis is None:
        raise AIConfigurationError(f"log_analysis configuration not found: {analysis_name}")
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
        # This entry point takes a ctx by contract, so the runtime's AI is read
        # once here at the boundary rather than looked up further down.
        getattr(ctx, "shared_ai", None),
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
    execution_profile: str = "",
    max_input_characters: int = 0,
    task: str = "",
    structured: bool = False,
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
    from rey_lib.config.env_reference import resolve_env_reference
    from rey_lib.ai.errors import AIConfigurationError

    result: dict[str, Any] = {"result": None, "action": None, "skipped": []}

    if not isinstance(record, dict):
        raise ValueError("Record analysis requires a JSON object record")
    # A caller naming a profile is asserting it exists. A caller naming a task
    # names none: its settings answer, and the AI refuses a selection it does
    # not offer, so checking here would be a second authority on one question.
    if not task:
        profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
        if profile is None:
            raise AIConfigurationError(
                f"llm_execution_profile not found: {execution_profile}"
            )

    # Raw send: the supplied package is serialized and sent exactly as-is. No
    # envelope instruction is appended and the raw response is returned unparsed
    # (direct_ask with no output_format sends the prompt exactly as supplied).
    prompt = json.dumps(record)
    if max_input_characters and len(prompt) > max_input_characters:
        raise ValueError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    result["result"] = _ask(
        # A ctx-taking entry point, so the runtime's AI is read once here at the
        # boundary rather than looked up inside the execution path.
        getattr(ctx, "shared_ai", None), prompt, execution_profile,
        payload_id=str(record["payload_id"]) if record.get("payload_id") else None,
        task=task,
        structured=structured,
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
    ``ctx.llm_profiles``, its ``api_key`` read from the environment as this
    request is built, and
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

    from rey_lib.analysis.api import RunRequest
    from rey_lib.analysis.execution import run as _run
    from rey_lib.artifacts import build_envelope_instruction

    common: dict[str, Any] = {
        "pipeline_id": "ai_workbench",
        "stage_id": "run",
        "profile_id": str(profile_name or ""),
        "payload_id": payload_id or None,
    }

    if instruction_mode == "contract":
        analyses = getattr(ctx, "log_analysis", None)
        entry = (
            analyses.get(instruction_value)
            if analyses is not None and hasattr(analyses, "get") else None
        )
        if entry is None or not str(getattr(entry, "contract", "") or ""):
            raise AIConfigurationError(
                f"No contract configured for '{instruction_value}'."
            )
        # Match configured AI Analysis exactly: read the configured YAML through
        # _analysis_instructions(), place that parsed contract in the established
        # analysis package, include its configured references, and send the whole
        # package through the inline/direct execution behavior. These analysis
        # contracts intentionally use Rey's `contract: {name, ...}` document
        # convention; they are not low-level rey_lib.analysis Contract files
        # and must never be passed to contract.load().
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

    from rey_lib.ai.errors import AIUnavailableError  # noqa: PLC0415

    ai = getattr(ctx, "shared_ai", None)
    if ai is None:
        raise AIUnavailableError(
            "This runtime has no AI configured, so the workbench cannot run."
        )
    return _run(request, ai, on_text=on_chunk, cancelled=cancelled)


def run_configured_log_analysis(
    run_log: Any,
    *,
    ai: Any,
    analysis_name: str,
    package_record_type: str,
    task: str,
) -> dict[str, Any]:
    """Run the configured LLM analysis over the existing package record.

    Sends the complete ``package_record_type`` record unchanged to the runtime's
    AI, extracts and validates the configured artifact from the standard rey_lib
    envelope, and writes the parsed structured result through the configured
    writer. The embedded contract is never reloaded — only existing rey_lib
    functions are composed.

    ``task`` names what this analysis is for, and it is the only selection this
    stage makes. The AI resolves request override -> task override -> default
    from its own settings, so an operator changing that task's settings changes
    what this stage runs on, and no profile or instruction is named here.

    ``ai`` is the runtime's one AI, belonging to the installation currently
    executing the run. It is handed in rather than discovered: the context below
    is rebuilt from the installation this run recorded and resolves the analysis
    configuration only, which is a different question from which runtime is
    executing. ``None`` means this runtime configures no AI, and the failure
    boundary records it.
    """
    import json

    from rey_lib.config.config_utils import build_ctx_from_path
    from rey_lib.errors.error_utils import build_safe_error_payload
    from rey_lib.files import write_file
    from rey_lib.ai.errors import (
        AIConfigurationError, AIProviderError, AIUnavailableError,
    )
    from rey_lib.artifacts import ArtifactEnvelopeError
    from rey_lib.logs.record_enrichment import log_run_record

    result: dict[str, Any] = {"result": None, "action": None, "skipped": [], "failures": []}

    path = Path(run_log.path()).expanduser().resolve()
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


    # Safe record identity for the failure record, resolved without dereferencing a
    # possibly malformed output block. When the configured output type cannot be read,
    # the failure is still recorded (never silent) rather than repaired or inferred.
    #
    # The type is this stage's, never the configured one. output.record_type names
    # the successful analysis payload -- LLM_INTERPRETATION here -- and an exception
    # is a different kind of record. Typing a failure as a success left a reader
    # unable to tell them apart by record type, which is what four stored
    # LLM_INTERPRETATION rows carrying an error_message already are.
    output = getattr(analysis, "output", None)
    failure_record_type = "LLM_ANALYSIS_FAILURE"
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
            # Configuration and runtime, separately. ctx was rebuilt from the
            # installation this run recorded and resolves the analysis entry; the
            # AI belongs to the runtime executing the run and is handed in.
            ctx,
            ai,
            # No profile. This chain always names a task, and the AI resolves
            # request override -> task override -> default from its own settings.
            # Naming a profile here would be a second answer to that question.
            "",
            str(getattr(analysis, "artifact_type", "")),
            package,
            task=task,
        )
    except (
        AIProviderError, ArtifactEnvelopeError, AIConfigurationError,
        # A runtime with no AI is one more way this stage cannot run, not a
        # reason to fail a run that already finished. It is a sibling of
        # AIConfigurationError rather than a subclass, so it matched nothing
        # here and skipped the record below along with fail_on_error.
        AIUnavailableError, json.JSONDecodeError,
        AttributeError, KeyError, TypeError,
    ) as exc:
        # Canonical failure record for any failure in this stage — LLM failure or
        # malformed configuration. Shaped by error_utils so the full sanitized scope
        # (type, message, exception, traceback) is captured, keyed by the configured
        # analysis name and stamped run metadata. fail_on_error then decides whether to
        # re-raise or return nonfatally.
        run_log.append(failure_record_type, record_group=failure_record_group,
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
        run_log.append(record_type, record_group=record_group, **parsed_result)
        result["action"] = "written_stdout"

    result["result"] = parsed_result
    return result


def _ask(
    ai: Any, prompt: str, execution_profile: str, *,
    payload_id: Any = None, task: str = "", structured: bool = False,
) -> Any:
    """Send one prompt through the runtime's AI and answer with its text.

    The AI is handed in, never discovered. It is the runtime's one AI, built by
    bootstrap, and it owns which provider and model answer, how a credential is
    resolved, how a failed call is retried and whether a replay is legal. This
    reads no provider configuration and resolves no credential.

    ``None`` is an explicit value meaning this runtime configures no AI. It is an
    ordinary capability state, and the caller's failure boundary records it.

    Evaluation evidence is recorded by ``rey_lib.logs`` through the run's
    ``RunLog``, which is why no log path appears here.
    """
    from rey_lib.ai import AIRequest  # noqa: PLC0415
    from rey_lib.ai.errors import AIUnavailableError  # noqa: PLC0415

    if ai is None:
        raise AIUnavailableError(
            "This runtime has no AI configured, so an analysis cannot run."
        )
    from rey_lib.ai.requests import AIOutputSpec  # noqa: PLC0415

    # A caller asking for JSON says so, and the runtime does two things with it:
    # the provider is told to answer in JSON, and OutputParser reads what comes
    # back -- including a model that wrapped its answer in prose or a fence,
    # which it already retries at the outermost object or array. Asking here is
    # what puts that boundary in the path; a caller that says nothing still gets
    # text, which is what the log-interpretation path returns.
    result = ai.execute(
        AIRequest.prompt(
            str(prompt),
            task=str(task or ""),
            profile_id=str(execution_profile or ""),
            **({"output": AIOutputSpec.json()} if structured else {}),
        ),
    )
    return result.value if structured else result.text
